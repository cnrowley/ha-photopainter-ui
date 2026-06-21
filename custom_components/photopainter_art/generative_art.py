"""Image-source and generative-art entities for PhotopainterArt.

The UI is built around a single primary picker:

Selects
-------
* Image source                – generative / camera / url   (PRIMARY PICKER)

When source = "generative", a second picker chooses which generator to use:

* Generative art type         – dla / mandelbrot / goban

── DLA ──────────────────────────────────────────────────────────────────────
(no user-tunable parameters – the CLI is fully stateful)

Sensors
  * DLA next frame           read-only; 1 – 120
Buttons
  * DLA reset sequence       restarts from frame 1 on next Generate

── Mandelbrot ────────────────────────────────────────────────────────────────
Selects
  * Foreground colour        black / white / green / blue / red / yellow / orange
  * Background colour        same list
  * Mode                     single (one frame) / zoom_sequence (advancing zoom)
Sensors
  * Mandelbrot next frame    read-only; only meaningful in zoom_sequence mode
Buttons
  * Mandelbrot reset zoom    deletes the state file so zoom restarts from scratch

── Goban ─────────────────────────────────────────────────────────────────────
Selects
  * SGF source                library / url / inline
  * Library game               one of the bundled public-domain SGF files
  * Background colour          white / black
  * Board colour                yellow / white
  * White stone colour          white / green / blue / red
  * Black stone colour          black / red
  * Grid thickness              1 / 2
  * Last-move highlight         dot / ring / none
Numbers
  * Move number                0 – 700  (0 = final position)
Texts
  * SGF download URL            used when source = url
  * SGF paste text              used when source = inline

── Camera / image source (source = "camera") ──────────────────────────────────
Selects
  * Camera/image entity         which HA camera or image entity to push

── URL source (source = "url") ────────────────────────────────────────────────
Texts
  * Image URL                   a direct image URL to fetch and push

── Common ────────────────────────────────────────────────────────────────────
Button
  * Generate & Display       fire-and-forget; reads the primary source select
                             and dispatches to generative / camera / url;
                             ``generating`` attr True while running

All mutable parameter values are stored in
    hass.data["{DOMAIN}_art_params"][entry_id]
so they survive coordinator refreshes without touching the device config.

The Mandelbrot zoom state JSON file is stored at:
    <hass_config_dir>/generative_art/<entry_id>/mandelbrot_state.json
and persists across HA restarts, enabling a long-running zoom sequence.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import aiohttp
from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .art_generator import (
    DLA_SEQUENCE_LENGTH,
    MANDELBROT_COLOURS,
    GOBAN_BG_COLOURS,
    GOBAN_BOARD_COLOURS,
    GOBAN_WHITE_STONE_COLOURS,
    GOBAN_BLACK_STONE_COLOURS,
    GOBAN_HIGHLIGHT_MODES,
    DLAParams,
    MandelbrotParams,
    GobanParams,
    generate_dla,
    generate_mandelbrot,
    generate_goban,
)
from .const import (
    DOMAIN,
    SOURCE_GENERATIVE,
    SOURCE_CAMERA,
    SOURCE_URL,
    IMAGE_SOURCES,
)
from .coordinator import PhotopainterArtCoordinator

_LOGGER = logging.getLogger(__name__)

# ── Art type constants ─────────────────────────────────────────────────────────
ART_TYPE_DLA        = "dla"
ART_TYPE_MANDELBROT = "mandelbrot"
ART_TYPE_GOBAN      = "goban"
ART_TYPES           = [ART_TYPE_DLA, ART_TYPE_MANDELBROT, ART_TYPE_GOBAN]

MANDELBROT_MODES    = ["single", "zoom_sequence"]

# hass.data key for art parameter state
_ART_STATE_KEY = f"{DOMAIN}_art_params"


# ── hass.data helpers ──────────────────────────────────────────────────────────

def _art_state(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    hass.data.setdefault(_ART_STATE_KEY, {})
    hass.data[_ART_STATE_KEY].setdefault(entry_id, {})
    return hass.data[_ART_STATE_KEY][entry_id]


def _mandelbrot_state_path(hass: HomeAssistant, entry_id: str) -> str:
    """Filesystem path for the persistent Mandelbrot zoom state JSON."""
    return os.path.join(
        hass.config.config_dir,
        "generative_art",
        entry_id,
        "mandelbrot_state.json",
    )


# ── DLA sequence manager ───────────────────────────────────────────────────────

class DLASequenceManager:
    """Tracks the current frame position in the 1 … DLA_SEQUENCE_LENGTH cycle."""

    def __init__(self) -> None:
        self._frame: int = 1

    @property
    def current_frame(self) -> int:
        return self._frame

    def next_frame(self) -> int:
        """Return the frame to render now and advance the counter."""
        frame = self._frame
        self._frame = (frame % DLA_SEQUENCE_LENGTH) + 1
        return frame

    def reset(self) -> None:
        self._frame = 1


_DLA_MANAGERS: dict[str, DLASequenceManager] = {}

def _dla_manager(entry_id: str) -> DLASequenceManager:
    if entry_id not in _DLA_MANAGERS:
        _DLA_MANAGERS[entry_id] = DLASequenceManager()
    return _DLA_MANAGERS[entry_id]


# ── Mandelbrot zoom frame counter ──────────────────────────────────────────────

class MandelbrotSequenceManager:
    """Tracks how many zoom steps have been generated for display purposes."""

    def __init__(self) -> None:
        self._step: int = 0   # increments each time zoom_sequence is used

    @property
    def current_step(self) -> int:
        return self._step

    def advance(self) -> None:
        self._step += 1

    def reset(self) -> None:
        self._step = 0


_MANDELBROT_MANAGERS: dict[str, MandelbrotSequenceManager] = {}

def _mandelbrot_manager(entry_id: str) -> MandelbrotSequenceManager:
    if entry_id not in _MANDELBROT_MANAGERS:
        _MANDELBROT_MANAGERS[entry_id] = MandelbrotSequenceManager()
    return _MANDELBROT_MANAGERS[entry_id]


# ── Platform setup ─────────────────────────────────────────────────────────────

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PhotopainterArtCoordinator = hass.data[DOMAIN][entry.entry_id]

    _dla_manager(entry.entry_id)
    _mandelbrot_manager(entry.entry_id)

    entities: list[Entity] = [
        # Primary picker — what kind of image source feeds the next display
        ImageSourceSelect(coordinator, entry, hass),

        # Camera / URL sources (used when Image source = camera / url)
        CameraEntitySelect(coordinator, entry, hass),
        ImageURLText(coordinator, entry, hass),

        # Generative art type sub-picker
        ArtTypeSelect(coordinator, entry, hass),

        # DLA
        DLAFrameSensor(coordinator, entry, hass),
        DLAResetButton(coordinator, entry, hass),

        # Mandelbrot
        MandelbrotFgSelect(coordinator, entry, hass),
        MandelbrotBgSelect(coordinator, entry, hass),
        MandelbrotModeSelect(coordinator, entry, hass),
        MandelbrotZoomStepSensor(coordinator, entry, hass),
        MandelbrotResetZoomButton(coordinator, entry, hass),

        # Goban
        GobanSourceSelect(coordinator, entry, hass),
        GobanLibrarySelect(coordinator, entry, hass),
        GobanURLText(coordinator, entry, hass),
        GobanSGFText(coordinator, entry, hass),
        GobanMoveNumber(coordinator, entry, hass),
        GobanBgSelect(coordinator, entry, hass),
        GobanBoardColourSelect(coordinator, entry, hass),
        GobanWhiteStoneColourSelect(coordinator, entry, hass),
        GobanBlackStoneColourSelect(coordinator, entry, hass),
        GobanGridThicknessSelect(coordinator, entry, hass),
        GobanHighlightSelect(coordinator, entry, hass),

        # Generate & Display
        GenerateArtButton(coordinator, entry, hass),
    ]

    async_add_entities(entities)


# ── Base mixin ─────────────────────────────────────────────────────────────────

class _ArtParamMixin:
    _param_key:    str = ""
    _default_value: Any = None
    _attr_has_entity_name = True
    _attr_available       = True

    def _state_dict(self) -> dict[str, Any]:
        hass  = getattr(self, "hass",   None) or getattr(self, "_hass")
        entry = getattr(self, "_entry")
        return _art_state(hass, entry.entry_id)

    def _get(self) -> Any:
        return self._state_dict().get(self._param_key, self._default_value)

    def _set(self, value: Any) -> None:
        self._state_dict()[self._param_key] = value


# ── Image source selector (PRIMARY PICKER) ──────────────────────────────────────

class ImageSourceSelect(_ArtParamMixin, CoordinatorEntity, SelectEntity):
    """Top-level picker: where does the next displayed image come from.

    This is the entity the UI is built around.  "generative" is listed
    first and is the default — pressing Generate & Display with this
    selected runs whichever generator is chosen in ArtTypeSelect.
    "camera" and "url" preserve the original upload / HA-image-serving
    behaviour from before generative art was added.
    """

    _param_key     = "image_source"
    _default_value = SOURCE_GENERATIVE
    _attr_options  = IMAGE_SOURCES
    _attr_icon     = "mdi:image-multiple-outline"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry = entry
        self.hass   = hass
        self._attr_unique_id   = f"{entry.entry_id}_image_source"
        self._attr_name        = "Image source"
        self._attr_device_info = coordinator.device_info

    @property
    def current_option(self) -> str:
        return self._get()

    async def async_select_option(self, option: str) -> None:
        self._set(option)
        self.async_write_ha_state()


class CameraEntitySelect(_ArtParamMixin, CoordinatorEntity, SelectEntity):
    """Pick a camera/image entity to push (used when source = camera).

    This mirrors the original "Media source" select that existed before
    generative art was introduced, but now lives under the unified source
    picker rather than being its own always-on feature.
    """

    _param_key     = "camera_entity_id"
    _default_value = "None"
    _attr_icon     = "mdi:camera"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry = entry
        self.hass   = hass
        self._attr_unique_id   = f"{entry.entry_id}_camera_entity"
        self._attr_name        = "Camera/image entity"
        self._attr_device_info = coordinator.device_info

    @property
    def options(self) -> list[str]:
        from homeassistant.helpers import entity_registry as er

        entity_reg = er.async_get(self.hass)
        camera_entities = [
            entity.entity_id
            for entity in entity_reg.entities.values()
            if entity.domain in ("camera", "image")
        ]
        for state in self.hass.states.async_all():
            if state.domain in ("camera", "image") and state.entity_id not in camera_entities:
                camera_entities.append(state.entity_id)
        camera_entities.sort()
        return ["None"] + camera_entities

    @property
    def current_option(self) -> str:
        return self._get()

    async def async_select_option(self, option: str) -> None:
        self._set(option)
        self.async_write_ha_state()


class ImageURLText(_ArtParamMixin, CoordinatorEntity, TextEntity):
    """Direct image URL to fetch and push (used when source = url)."""

    _param_key       = "image_url"
    _default_value   = ""
    _attr_native_min = 0
    _attr_native_max = 2048
    _attr_icon       = "mdi:link"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry = entry
        self.hass   = hass
        self._attr_unique_id   = f"{entry.entry_id}_source_image_url"
        self._attr_name        = "Image URL"
        self._attr_device_info = coordinator.device_info

    @property
    def native_value(self) -> str:
        return self._get()

    async def async_set_value(self, value: str) -> None:
        self._set(value.strip())
        self.async_write_ha_state()


# ── Art-type selector (sub-picker, only relevant when source = generative) ──────

class ArtTypeSelect(_ArtParamMixin, CoordinatorEntity, SelectEntity):
    _param_key     = "art_type"
    _default_value = ART_TYPE_DLA
    _attr_options  = ART_TYPES
    _attr_icon     = "mdi:palette"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry = entry
        self.hass   = hass
        self._attr_unique_id   = f"{entry.entry_id}_art_type"
        self._attr_name        = "Generative art type"
        self._attr_device_info = coordinator.device_info

    @property
    def current_option(self) -> str:
        return self._get()

    async def async_select_option(self, option: str) -> None:
        self._set(option)
        self.async_write_ha_state()


# ── DLA entities ───────────────────────────────────────────────────────────────

class DLAFrameSensor(_ArtParamMixin, CoordinatorEntity, SensorEntity):
    """Read-only sensor: next DLA frame index (1 – 120)."""

    _param_key        = ""
    _default_value    = 1
    _attr_icon        = "mdi:film-strip"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "frame"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry = entry
        self.hass   = hass
        self._attr_unique_id   = f"{entry.entry_id}_dla_frame"
        self._attr_name        = "DLA next frame"
        self._attr_device_info = coordinator.device_info

    @property
    def native_value(self) -> int:
        return _dla_manager(self._entry.entry_id).current_frame

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"sequence_length": DLA_SEQUENCE_LENGTH}


class DLAResetButton(_ArtParamMixin, CoordinatorEntity, ButtonEntity):
    """Reset the DLA sequence back to frame 1."""

    _param_key     = ""
    _default_value = None
    _attr_icon     = "mdi:restart"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry = entry
        self.hass   = hass
        self._attr_unique_id   = f"{entry.entry_id}_dla_reset"
        self._attr_name        = "DLA reset sequence"
        self._attr_device_info = coordinator.device_info

    async def async_press(self) -> None:
        _dla_manager(self._entry.entry_id).reset()
        _LOGGER.info("DLA sequence reset to frame 1")
        self.hass.async_create_task(self.coordinator.async_request_refresh())


# ── Mandelbrot entities ────────────────────────────────────────────────────────

class MandelbrotFgSelect(_ArtParamMixin, CoordinatorEntity, SelectEntity):
    """Mandelbrot foreground colour."""

    _param_key     = "mb_fg"
    _default_value = "white"
    _attr_options  = MANDELBROT_COLOURS
    _attr_icon     = "mdi:palette"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry = entry
        self.hass   = hass
        self._attr_unique_id   = f"{entry.entry_id}_mb_fg"
        self._attr_name        = "Mandelbrot foreground colour"
        self._attr_device_info = coordinator.device_info

    @property
    def current_option(self) -> str:
        return self._get()

    async def async_select_option(self, option: str) -> None:
        self._set(option)
        self.async_write_ha_state()


class MandelbrotBgSelect(_ArtParamMixin, CoordinatorEntity, SelectEntity):
    """Mandelbrot background colour."""

    _param_key     = "mb_bg"
    _default_value = "black"
    _attr_options  = MANDELBROT_COLOURS
    _attr_icon     = "mdi:palette-outline"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry = entry
        self.hass   = hass
        self._attr_unique_id   = f"{entry.entry_id}_mb_bg"
        self._attr_name        = "Mandelbrot background colour"
        self._attr_device_info = coordinator.device_info

    @property
    def current_option(self) -> str:
        return self._get()

    async def async_select_option(self, option: str) -> None:
        self._set(option)
        self.async_write_ha_state()


class MandelbrotModeSelect(_ArtParamMixin, CoordinatorEntity, SelectEntity):
    """Single frame vs. advancing zoom sequence."""

    _param_key     = "mb_mode"
    _default_value = "single"
    _attr_options  = MANDELBROT_MODES
    _attr_icon     = "mdi:magnify-expand"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry = entry
        self.hass   = hass
        self._attr_unique_id   = f"{entry.entry_id}_mb_mode"
        self._attr_name        = "Mandelbrot mode"
        self._attr_device_info = coordinator.device_info

    @property
    def current_option(self) -> str:
        return self._get()

    async def async_select_option(self, option: str) -> None:
        self._set(option)
        self.async_write_ha_state()


class MandelbrotZoomStepSensor(_ArtParamMixin, CoordinatorEntity, SensorEntity):
    """Read-only: how many zoom steps have been generated so far."""

    _param_key        = ""
    _default_value    = 0
    _attr_icon        = "mdi:magnify-plus-outline"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "step"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry = entry
        self.hass   = hass
        self._attr_unique_id   = f"{entry.entry_id}_mb_zoom_step"
        self._attr_name        = "Mandelbrot zoom step"
        self._attr_device_info = coordinator.device_info

    @property
    def native_value(self) -> int:
        return _mandelbrot_manager(self._entry.entry_id).current_step

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state_file = _mandelbrot_state_path(self.hass, self._entry.entry_id)
        return {
            "state_file_exists": os.path.isfile(state_file),
            "state_file_path":   state_file,
        }


class MandelbrotResetZoomButton(_ArtParamMixin, CoordinatorEntity, ButtonEntity):
    """Delete the Mandelbrot state file so the next Generate starts fresh."""

    _param_key     = ""
    _default_value = None
    _attr_icon     = "mdi:magnify-remove-outline"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry = entry
        self.hass   = hass
        self._attr_unique_id   = f"{entry.entry_id}_mb_reset_zoom"
        self._attr_name        = "Mandelbrot reset zoom"
        self._attr_device_info = coordinator.device_info

    async def async_press(self) -> None:
        state_file = _mandelbrot_state_path(self.hass, self._entry.entry_id)
        if os.path.isfile(state_file):
            await self.hass.async_add_executor_job(os.unlink, state_file)
            _LOGGER.info("Mandelbrot: deleted state file %s", state_file)
        _mandelbrot_manager(self._entry.entry_id).reset()
        _LOGGER.info("Mandelbrot zoom reset to step 0")
        self.hass.async_create_task(self.coordinator.async_request_refresh())


# ── Goban entities ─────────────────────────────────────────────────────────────
#
# goban.x renders a single SGF position to a BMP.  The SGF source is picked
# via GobanSourceSelect (inline / library / url); only the entities relevant
# to the chosen source need to be filled in.  Colour/style entities map
# directly onto goban.x flags.

class GobanSourceSelect(_ArtParamMixin, CoordinatorEntity, SelectEntity):
    """Where to get the SGF from: bundled library, a URL, or pasted text."""

    _param_key     = "goban_source"
    _default_value = "library"
    _attr_options  = ["library", "url", "inline"]
    _attr_icon     = "mdi:source-branch"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry = entry
        self.hass   = hass
        self._attr_unique_id   = f"{entry.entry_id}_goban_source"
        self._attr_name        = "Goban SGF source"
        self._attr_device_info = coordinator.device_info

    @property
    def current_option(self) -> str:
        return self._get()

    async def async_select_option(self, option: str) -> None:
        self._set(option)
        self.async_write_ha_state()


class GobanLibrarySelect(_ArtParamMixin, CoordinatorEntity, SelectEntity):
    """Pick a game from the bundled SGF library (used when source=library)."""

    _param_key = "goban_library_id"
    _attr_icon = "mdi:bookshelf"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry = entry
        self.hass   = hass
        self._attr_unique_id   = f"{entry.entry_id}_goban_library"
        self._attr_name        = "Goban library game"
        self._attr_device_info = coordinator.device_info

        from . import sgf_library

        options = sgf_library.library_options()
        self._attr_options  = options
        self._default_value = options[0] if options else ""

    @property
    def current_option(self) -> str:
        return self._get()

    async def async_select_option(self, option: str) -> None:
        self._set(option)
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        from . import sgf_library

        return {"display_name": sgf_library.display_name(self._get())}


class GobanURLText(_ArtParamMixin, CoordinatorEntity, TextEntity):
    """Direct download URL for an .sgf file (used when source=url).

    Works with any host that serves a raw SGF file at a stable URL, e.g.
    an OGS "download SGF" link or a file from a public SGF archive.
    """

    _param_key       = "goban_url"
    _default_value   = ""
    _attr_native_min = 0
    _attr_native_max = 2048
    _attr_icon       = "mdi:download-network"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry = entry
        self.hass   = hass
        self._attr_unique_id   = f"{entry.entry_id}_goban_url"
        self._attr_name        = "Goban SGF download URL"
        self._attr_device_info = coordinator.device_info

    @property
    def native_value(self) -> str:
        return self._get()

    async def async_set_value(self, value: str) -> None:
        self._set(value.strip())
        self.async_write_ha_state()


class GobanSGFText(_ArtParamMixin, CoordinatorEntity, TextEntity):
    """Paste raw SGF text directly (used when source=inline)."""

    _param_key       = "goban_sgf_text"
    _default_value   = ""
    _attr_native_min = 0
    _attr_native_max = 65535
    _attr_icon       = "mdi:file-document-edit"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry = entry
        self.hass   = hass
        self._attr_unique_id   = f"{entry.entry_id}_goban_sgf_text"
        self._attr_name        = "Goban SGF (paste text)"
        self._attr_device_info = coordinator.device_info

    @property
    def native_value(self) -> str:
        return self._get()

    async def async_set_value(self, value: str) -> None:
        self._set(value)
        self.async_write_ha_state()


class GobanMoveNumber(_ArtParamMixin, CoordinatorEntity, NumberEntity):
    """Move number to render (0 = final position in the SGF)."""

    _param_key             = "goban_move"
    _default_value         = 0
    _attr_native_min_value = 0
    _attr_native_max_value = 700
    _attr_native_step      = 1
    _attr_mode             = NumberMode.BOX
    _attr_icon             = "mdi:counter"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry = entry
        self.hass   = hass
        self._attr_unique_id   = f"{entry.entry_id}_goban_move"
        self._attr_name        = "Goban move number (0 = final)"
        self._attr_device_info = coordinator.device_info

    @property
    def native_value(self) -> float:
        return float(self._get())

    async def async_set_native_value(self, value: float) -> None:
        self._set(int(value))
        self.async_write_ha_state()


class GobanBgSelect(_ArtParamMixin, CoordinatorEntity, SelectEntity):
    """Board background colour (goban.x -bg)."""

    _param_key     = "goban_bg"
    _default_value = "white"
    _attr_options  = GOBAN_BG_COLOURS
    _attr_icon     = "mdi:square-outline"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry = entry
        self.hass   = hass
        self._attr_unique_id   = f"{entry.entry_id}_goban_bg"
        self._attr_name        = "Goban background colour"
        self._attr_device_info = coordinator.device_info

    @property
    def current_option(self) -> str:
        return self._get()

    async def async_select_option(self, option: str) -> None:
        self._set(option)
        self.async_write_ha_state()


class GobanBoardColourSelect(_ArtParamMixin, CoordinatorEntity, SelectEntity):
    """Board surface colour (goban.x -board)."""

    _param_key     = "goban_board"
    _default_value = "yellow"
    _attr_options  = GOBAN_BOARD_COLOURS
    _attr_icon     = "mdi:square"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry = entry
        self.hass   = hass
        self._attr_unique_id   = f"{entry.entry_id}_goban_board_colour"
        self._attr_name        = "Goban board colour"
        self._attr_device_info = coordinator.device_info

    @property
    def current_option(self) -> str:
        return self._get()

    async def async_select_option(self, option: str) -> None:
        self._set(option)
        self.async_write_ha_state()


class GobanWhiteStoneColourSelect(_ArtParamMixin, CoordinatorEntity, SelectEntity):
    """White stone colour (goban.x -white-color)."""

    _param_key     = "goban_white_color"
    _default_value = "green"
    _attr_options  = GOBAN_WHITE_STONE_COLOURS
    _attr_icon     = "mdi:circle-outline"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry = entry
        self.hass   = hass
        self._attr_unique_id   = f"{entry.entry_id}_goban_white_color"
        self._attr_name        = "Goban white stone colour"
        self._attr_device_info = coordinator.device_info

    @property
    def current_option(self) -> str:
        return self._get()

    async def async_select_option(self, option: str) -> None:
        self._set(option)
        self.async_write_ha_state()


class GobanBlackStoneColourSelect(_ArtParamMixin, CoordinatorEntity, SelectEntity):
    """Black stone colour (goban.x -black-color)."""

    _param_key     = "goban_black_color"
    _default_value = "black"
    _attr_options  = GOBAN_BLACK_STONE_COLOURS
    _attr_icon     = "mdi:circle"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry = entry
        self.hass   = hass
        self._attr_unique_id   = f"{entry.entry_id}_goban_black_color"
        self._attr_name        = "Goban black stone colour"
        self._attr_device_info = coordinator.device_info

    @property
    def current_option(self) -> str:
        return self._get()

    async def async_select_option(self, option: str) -> None:
        self._set(option)
        self.async_write_ha_state()


class GobanGridThicknessSelect(_ArtParamMixin, CoordinatorEntity, SelectEntity):
    """Grid line thickness (goban.x -grid-thickness, 1 or 2)."""

    _param_key     = "goban_grid_thickness"
    _default_value = "1"
    _attr_options  = ["1", "2"]
    _attr_icon     = "mdi:grid"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry = entry
        self.hass   = hass
        self._attr_unique_id   = f"{entry.entry_id}_goban_grid_thickness"
        self._attr_name        = "Goban grid thickness"
        self._attr_device_info = coordinator.device_info

    @property
    def current_option(self) -> str:
        return self._get()

    async def async_select_option(self, option: str) -> None:
        self._set(option)
        self.async_write_ha_state()


class GobanHighlightSelect(_ArtParamMixin, CoordinatorEntity, SelectEntity):
    """Last-move highlight style (goban.x -highlight)."""

    _param_key     = "goban_highlight"
    _default_value = "ring"
    _attr_options  = GOBAN_HIGHLIGHT_MODES
    _attr_icon     = "mdi:target"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry = entry
        self.hass   = hass
        self._attr_unique_id   = f"{entry.entry_id}_goban_highlight"
        self._attr_name        = "Goban last-move highlight"
        self._attr_device_info = coordinator.device_info

    @property
    def current_option(self) -> str:
        return self._get()

    async def async_select_option(self, option: str) -> None:
        self._set(option)
        self.async_write_ha_state()


# ── Generate & Display button ──────────────────────────────────────────────────

class GenerateArtButton(_ArtParamMixin, CoordinatorEntity, ButtonEntity):
    """Produce the next image — from a generator, a camera, or a URL — and
    push it to the PhotoPainter.

    Reads ``image_source`` first (the primary picker).  Only when that is
    "generative" does it look at ``art_type`` to decide which generator to
    run; for "camera" / "url" it fetches the configured image directly,
    exactly as the original upload-based behaviour worked before generative
    art was added.
    """

    _param_key     = ""
    _default_value = None
    _attr_icon     = "mdi:image-play"

    def __init__(self, coordinator, entry, hass):
        super().__init__(coordinator)
        self._entry      = entry
        self.hass        = hass
        self._generating = False
        self._attr_unique_id   = f"{entry.entry_id}_generate_art"
        self._attr_name        = "Generate & display"
        self._attr_device_info = coordinator.device_info

    @property
    def available(self) -> bool:
        return self.coordinator.available and not self._generating

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state  = self._state_dict()
        source = state.get("image_source", SOURCE_GENERATIVE)
        attrs: dict[str, Any] = {
            "image_source": source,
            "generating":   self._generating,
        }

        if source == SOURCE_CAMERA:
            attrs["camera_entity_id"] = state.get("camera_entity_id", "None")
            return attrs

        if source == SOURCE_URL:
            attrs["image_url"] = state.get("image_url", "")
            return attrs

        # source == SOURCE_GENERATIVE
        art_type = state.get("art_type", ART_TYPE_DLA)
        attrs["art_type"] = art_type

        if art_type == ART_TYPE_DLA:
            mgr = _dla_manager(self._entry.entry_id)
            attrs.update({
                "dla_next_frame":      mgr.current_frame,
                "dla_sequence_length": DLA_SEQUENCE_LENGTH,
            })

        elif art_type == ART_TYPE_MANDELBROT:
            mb_mgr = _mandelbrot_manager(self._entry.entry_id)
            attrs.update({
                "mb_fg":        state.get("mb_fg",   "white"),
                "mb_bg":        state.get("mb_bg",   "black"),
                "mb_mode":      state.get("mb_mode", "single"),
                "mb_zoom_step": mb_mgr.current_step,
            })

        elif art_type == ART_TYPE_GOBAN:
            attrs.update({
                "goban_source":         state.get("goban_source", "library"),
                "goban_library_id":     state.get("goban_library_id", ""),
                "goban_url":            state.get("goban_url", ""),
                "goban_move":           state.get("goban_move", 0),
                "goban_bg":             state.get("goban_bg", "white"),
                "goban_board":          state.get("goban_board", "yellow"),
                "goban_white_color":    state.get("goban_white_color", "green"),
                "goban_black_color":    state.get("goban_black_color", "black"),
                "goban_grid_thickness": state.get("goban_grid_thickness", "1"),
                "goban_highlight":      state.get("goban_highlight", "ring"),
            })

        return attrs

    async def async_press(self) -> None:
        if self._generating:
            _LOGGER.warning("Generation already in progress – ignoring press")
            return
        asyncio.create_task(self._generate_and_display())

    async def _generate_and_display(self) -> None:
        self._generating = True
        self.async_write_ha_state()

        state  = self._state_dict()
        source = state.get("image_source", SOURCE_GENERATIVE)

        try:
            if source == SOURCE_CAMERA:
                image_bytes = await self._fetch_camera_image(state)
            elif source == SOURCE_URL:
                image_bytes = await self._fetch_url_image(state)
            else:
                image_bytes = await self._generate_art_image(state)

            if image_bytes is None:
                return  # error already logged by the helper

            _LOGGER.info(
                "Produced image via source=%s (%d bytes), sending to device …",
                source, len(image_bytes),
            )
            success = await self.coordinator.async_display_image(image_bytes)
            if success:
                _LOGGER.info("Image displayed successfully")
                await self.coordinator.async_request_refresh()
            else:
                _LOGGER.error("Failed to display image on device")

        except RuntimeError as err:
            _LOGGER.error("Image generation failed: %s", err)
        except Exception as err:
            _LOGGER.error("Unexpected error producing image: %s", err, exc_info=True)
        finally:
            self._generating = False
            self.async_write_ha_state()

    async def _fetch_camera_image(self, state: dict[str, Any]) -> bytes | None:
        """Fetch the latest frame from the configured camera/image entity."""
        entity_id = state.get("camera_entity_id", "None")
        if not entity_id or entity_id == "None":
            _LOGGER.error("Image source is 'camera' but no entity is selected")
            return None

        hass_state = self.hass.states.get(entity_id)
        if hass_state is None:
            _LOGGER.error("Entity %s not found", entity_id)
            return None

        if hass_state.domain == "camera":
            from homeassistant.components.camera import async_get_image

            try:
                image = await async_get_image(self.hass, entity_id)
                return image.content
            except Exception as err:
                _LOGGER.error("Error getting image from %s: %s", entity_id, err)
                return None

        if hass_state.domain == "image":
            entity_picture = hass_state.attributes.get("entity_picture")
            if not entity_picture:
                _LOGGER.error("Image entity %s has no picture", entity_id)
                return None
            base_url = (
                self.hass.config.external_url
                or self.hass.config.internal_url
                or "http://localhost:8123"
            )
            full_url = entity_picture if entity_picture.startswith("http") else f"{base_url}{entity_picture}"
            return await self._fetch_url_bytes(full_url)

        _LOGGER.error("Entity %s is not a camera or image entity", entity_id)
        return None

    async def _fetch_url_image(self, state: dict[str, Any]) -> bytes | None:
        """Fetch an image from a directly-configured URL."""
        url = state.get("image_url", "").strip()
        if not url:
            _LOGGER.error("Image source is 'url' but no Image URL is configured")
            return None
        return await self._fetch_url_bytes(url)

    async def _fetch_url_bytes(self, url: str) -> bytes | None:
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status != 200:
                    _LOGGER.error("Failed to fetch image from %s: HTTP %s", url, response.status)
                    return None
                return await response.read()
        except aiohttp.ClientError as err:
            _LOGGER.error("Failed to fetch image from %s: %s", url, err)
            return None

    async def _generate_art_image(self, state: dict[str, Any]) -> bytes | None:
        """Run the selected generator (DLA / Mandelbrot / Goban) and return bytes."""
        art_type = state.get("art_type", ART_TYPE_DLA)
        _LOGGER.info("Generating %s artwork …", art_type)

        if art_type == ART_TYPE_DLA:
            mgr   = _dla_manager(self._entry.entry_id)
            frame = mgr.next_frame()
            _LOGGER.info("DLA: frame %d / %d", frame, DLA_SEQUENCE_LENGTH)
            return await generate_dla(DLAParams(frame=frame))

        if art_type == ART_TYPE_MANDELBROT:
            mb_mgr = _mandelbrot_manager(self._entry.entry_id)
            mode   = state.get("mb_mode", "single")
            is_seq = (mode == "zoom_sequence")

            params = MandelbrotParams(
                fg         = state.get("mb_fg",   "white"),
                bg         = state.get("mb_bg",   "black"),
                single     = not is_seq,
                frames     = 1,              # one step per Generate press
                state_path = _mandelbrot_state_path(self.hass, self._entry.entry_id)
                           if is_seq else "",
            )
            image_bytes = await generate_mandelbrot(params)
            if is_seq:
                mb_mgr.advance()
            return image_bytes

        if art_type == ART_TYPE_GOBAN:
            return await generate_goban(GobanParams(
                sgf_source     = state.get("goban_source",     "library"),
                sgf_text       = state.get("goban_sgf_text",   ""),
                library_id     = state.get("goban_library_id", ""),
                sgf_url        = state.get("goban_url",        ""),
                move           = state.get("goban_move",        0),
                bg             = state.get("goban_bg",          "white"),
                board          = state.get("goban_board",       "yellow"),
                white_color    = state.get("goban_white_color", "green"),
                black_color    = state.get("goban_black_color", "black"),
                grid_thickness = int(state.get("goban_grid_thickness", "1")),
                highlight      = state.get("goban_highlight",   "ring"),
            ))

        _LOGGER.error("Unknown art type: %s", art_type)
        return None
