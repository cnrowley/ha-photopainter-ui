"""Services for PhotopainterArt."""

from __future__ import annotations

import logging

import aiohttp
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .art_generator import (
    DLA_SEQUENCE_LENGTH,
    MANDELBROT_COLOURS,
    GOBAN_BG_COLOURS,
    GOBAN_BOARD_COLOURS,
    GOBAN_WHITE_STONE_COLOURS,
    GOBAN_BLACK_STONE_COLOURS,
    GOBAN_GRID_THICKNESS,
    GOBAN_HIGHLIGHT_MODES,
    DLAParams,
    MandelbrotParams,
    GobanParams,
    generate_dla,
    generate_mandelbrot,
    generate_goban,
)
from .const import (
    ART_TYPE_DLA,
    ART_TYPE_MANDELBROT,
    ART_TYPE_GOBAN,
    ART_TYPES,
    DOMAIN,
    GOBAN_SOURCES,
    IMAGE_SOURCES,
    MANDELBROT_MODES,
    SOURCE_CAMERA,
    SOURCE_GENERATIVE,
    SOURCE_URL,
    SERVICE_DISPLAY_IMAGE,
    SERVICE_GENERATE_ART,
    SERVICE_ROTATE,
)
from .coordinator import PhotopainterArtCoordinator
from .generative_art import _mandelbrot_state_path, _mandelbrot_manager
from . import sgf_library

_LOGGER = logging.getLogger(__name__)

SERVICE_DISPLAY_IMAGE_SCHEMA = vol.Schema(
    {vol.Required("entity_id"): cv.entity_id}
)

SERVICE_GENERATE_ART_SCHEMA = vol.Schema(
    {
        # ── Primary source picker (mirrors the "Image source" select) ──────────
        vol.Optional("image_source", default=SOURCE_GENERATIVE): vol.In(IMAGE_SOURCES),

        # ── Camera / URL sources ─────────────────────────────────────────────
        vol.Optional("camera_entity_id", default=""): cv.string,
        vol.Optional("image_url", default=""): cv.string,

        # ── Generative: which generator ─────────────────────────────────────
        vol.Optional("art_type", default=ART_TYPE_DLA): vol.In(ART_TYPES),

        # ── DLA ───────────────────────────────────────────────────────────────
        vol.Optional("dla_frame", default=1): vol.All(
            int, vol.Range(min=1, max=DLA_SEQUENCE_LENGTH)
        ),

        # ── Mandelbrot ────────────────────────────────────────────────────────
        vol.Optional("mb_fg",   default="white"): vol.In(MANDELBROT_COLOURS),
        vol.Optional("mb_bg",   default="black"): vol.In(MANDELBROT_COLOURS),
        vol.Optional("mb_mode", default="single"): vol.In(MANDELBROT_MODES),

        # ── Goban ─────────────────────────────────────────────────────────────
        vol.Optional("goban_source",     default="library"): vol.In(GOBAN_SOURCES),
        vol.Optional("goban_library_id", default=""): str,
        vol.Optional("goban_url",        default=""): str,
        vol.Optional("goban_sgf_text",   default=""): str,
        vol.Optional("goban_move",       default=0): vol.All(int, vol.Range(min=0, max=700)),
        vol.Optional("goban_bg",             default="white"):  vol.In(GOBAN_BG_COLOURS),
        vol.Optional("goban_board",          default="yellow"): vol.In(GOBAN_BOARD_COLOURS),
        vol.Optional("goban_white_color",    default="green"):  vol.In(GOBAN_WHITE_STONE_COLOURS),
        vol.Optional("goban_black_color",    default="black"):  vol.In(GOBAN_BLACK_STONE_COLOURS),
        vol.Optional("goban_grid_thickness", default=1):        vol.In(GOBAN_GRID_THICKNESS),
        vol.Optional("goban_highlight",      default="ring"):   vol.In(GOBAN_HIGHLIGHT_MODES),
    }
)


async def async_register_services(hass: HomeAssistant, coordinator: PhotopainterArtCoordinator) -> None:
    """Register services for the integration."""

    async def handle_rotate(call: ServiceCall) -> None:
        """Trigger the device's own internal image rotation."""
        try:
            async with coordinator.session.post(
                f"{coordinator.host}/api/rotate",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status == 200:
                    _LOGGER.info("Successfully triggered image rotation")
                    await coordinator.async_request_refresh()
                else:
                    _LOGGER.error("Failed to trigger rotation: HTTP %s", response.status)
        except aiohttp.ClientError as err:
            _LOGGER.error("Failed to trigger rotation: %s", err)

    async def handle_display_image(call: ServiceCall) -> None:
        """Push an image from a camera/image entity directly (legacy path)."""
        entity_id = call.data["entity_id"]
        state = hass.states.get(entity_id)
        if state is None:
            _LOGGER.error("Entity %s not found", entity_id)
            return
        if state.domain == "camera":
            from homeassistant.components.camera import async_get_image
            try:
                image = await async_get_image(hass, entity_id)
                success = await coordinator.async_display_image(image.content)
                if success:
                    _LOGGER.info("Successfully displayed image from %s", entity_id)
                else:
                    _LOGGER.error("Failed to display image from %s", entity_id)
            except Exception as err:
                _LOGGER.error("Error getting image from %s: %s", entity_id, err)
        else:
            _LOGGER.error("Entity %s is not a camera", entity_id)

    async def handle_generate_art(call: ServiceCall) -> None:
        """Unified entry point: produce the next image from whichever source
        is specified (generative / camera / url) and push it to the device.

        Mirrors the logic in generative_art.GenerateArtButton so automations
        can drive exactly what the "Generate & display" button does.
        """
        data   = call.data
        source = data.get("image_source", SOURCE_GENERATIVE)

        try:
            if source == SOURCE_CAMERA:
                image_bytes = await _service_fetch_camera_image(hass, data)
            elif source == SOURCE_URL:
                image_bytes = await _service_fetch_url_image(hass, data)
            else:
                image_bytes = await _service_generate_art_image(hass, data)

            if image_bytes is None:
                return  # error already logged by the helper

            _LOGGER.info(
                "Produced image via source=%s (%d bytes), sending to device",
                source, len(image_bytes),
            )
            success = await coordinator.async_display_image(image_bytes)
            if success:
                _LOGGER.info("Image displayed successfully via service call")
                await coordinator.async_request_refresh()
            else:
                _LOGGER.error("Failed to display image on device")

        except RuntimeError as err:
            _LOGGER.error("Image generation failed: %s", err)
        except Exception as err:
            _LOGGER.error("Unexpected error producing image: %s", err, exc_info=True)

    async def _service_fetch_camera_image(hass: HomeAssistant, data: dict) -> bytes | None:
        entity_id = data.get("camera_entity_id", "")
        if not entity_id:
            _LOGGER.error("image_source is 'camera' but camera_entity_id is empty")
            return None
        state = hass.states.get(entity_id)
        if state is None:
            _LOGGER.error("Entity %s not found", entity_id)
            return None
        if state.domain == "camera":
            from homeassistant.components.camera import async_get_image
            try:
                image = await async_get_image(hass, entity_id)
                return image.content
            except Exception as err:
                _LOGGER.error("Error getting image from %s: %s", entity_id, err)
                return None
        if state.domain == "image":
            entity_picture = state.attributes.get("entity_picture")
            if not entity_picture:
                _LOGGER.error("Image entity %s has no picture", entity_id)
                return None
            base_url = hass.config.external_url or hass.config.internal_url or "http://localhost:8123"
            full_url = entity_picture if entity_picture.startswith("http") else f"{base_url}{entity_picture}"
            return await _service_fetch_url_bytes(hass, full_url)
        _LOGGER.error("Entity %s is not a camera or image entity", entity_id)
        return None

    async def _service_fetch_url_image(hass: HomeAssistant, data: dict) -> bytes | None:
        url = data.get("image_url", "").strip()
        if not url:
            _LOGGER.error("image_source is 'url' but image_url is empty")
            return None
        return await _service_fetch_url_bytes(hass, url)

    async def _service_fetch_url_bytes(hass: HomeAssistant, url: str) -> bytes | None:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(hass)
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status != 200:
                    _LOGGER.error("Failed to fetch image from %s: HTTP %s", url, response.status)
                    return None
                return await response.read()
        except aiohttp.ClientError as err:
            _LOGGER.error("Failed to fetch image from %s: %s", url, err)
            return None

    async def _service_generate_art_image(hass: HomeAssistant, data: dict) -> bytes | None:
        art_type = data.get("art_type", ART_TYPE_DLA)
        _LOGGER.info("Service call: generating %s artwork", art_type)

        if art_type == ART_TYPE_DLA:
            frame = data.get("dla_frame", 1)
            _LOGGER.info("DLA service: rendering frame %d", frame)
            return await generate_dla(DLAParams(frame=frame))

        if art_type == ART_TYPE_MANDELBROT:
            mode   = data.get("mb_mode", "single")
            is_seq = (mode == "zoom_sequence")
            entry_id = next(iter(hass.data.get(DOMAIN, {})), None)
            state_path = (
                _mandelbrot_state_path(hass, entry_id)
                if (is_seq and entry_id)
                else ""
            )
            params = MandelbrotParams(
                fg         = data.get("mb_fg",   "white"),
                bg         = data.get("mb_bg",   "black"),
                single     = not is_seq,
                frames     = 1,
                state_path = state_path,
            )
            image_bytes = await generate_mandelbrot(params)
            if is_seq and entry_id:
                _mandelbrot_manager(entry_id).advance()
            return image_bytes

        if art_type == ART_TYPE_GOBAN:
            source = data.get("goban_source", "library")
            lib_id = data.get("goban_library_id", "")
            if source == "library" and not lib_id:
                options = sgf_library.library_options()
                lib_id = options[0] if options else ""

            return await generate_goban(GobanParams(
                sgf_source     = source,
                sgf_text       = data.get("goban_sgf_text", ""),
                library_id     = lib_id,
                sgf_url        = data.get("goban_url", ""),
                move           = data.get("goban_move", 0),
                bg             = data.get("goban_bg", "white"),
                board          = data.get("goban_board", "yellow"),
                white_color    = data.get("goban_white_color", "green"),
                black_color    = data.get("goban_black_color", "black"),
                grid_thickness = data.get("goban_grid_thickness", 1),
                highlight      = data.get("goban_highlight", "ring"),
            ))

        _LOGGER.error("Unknown art type: %s", art_type)
        return None

    hass.services.async_register(DOMAIN, SERVICE_ROTATE,        handle_rotate)
    hass.services.async_register(DOMAIN, SERVICE_DISPLAY_IMAGE, handle_display_image,
                                 schema=SERVICE_DISPLAY_IMAGE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_GENERATE_ART,  handle_generate_art,
                                 schema=SERVICE_GENERATE_ART_SCHEMA)
