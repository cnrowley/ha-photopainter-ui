"""Constants for the PhotopainterArt integration."""

DOMAIN = "photopainter_art"

# Configuration
CONF_HOST = "host"
CONF_HA_URL = "ha_url"

# Default values
DEFAULT_SCAN_INTERVAL = 60  # seconds
DEFAULT_PORT = 80

# API endpoints
API_CONFIG = "/api/config"
API_BATTERY = "/api/battery"
API_SENSOR = "/api/sensor"
API_SYSTEM_INFO = "/api/system-info"
API_DISPLAY_IMAGE = "/api/display-image"
API_ROTATE = "/api/rotate"
API_OTA_STATUS = "/api/ota/status"
API_CURRENT_IMAGE = "/api/current_image"

# Services
SERVICE_ROTATE = "rotate"
SERVICE_DISPLAY_IMAGE = "display_image"
SERVICE_GENERATE_ART = "generate_art"

# Image serving
IMAGE_ENDPOINT_PATH = "/api/photopainter_art/image"

# ── Image source ─────────────────────────────────────────────────────────────
# Top-level picker that reorients the UI around how the next image is
# produced.  "generative" is the primary/first-class path; "camera" and
# "url" preserve the original upload/HA-image-serving behaviour.
SOURCE_GENERATIVE = "generative"
SOURCE_CAMERA     = "camera"
SOURCE_URL        = "url"
IMAGE_SOURCES     = [SOURCE_GENERATIVE, SOURCE_CAMERA, SOURCE_URL]

# hass.data key under which art parameter state is stored (per entry)
ART_STATE_KEY = f"{DOMAIN}_art_params"

# Generative art type identifiers
ART_TYPE_DLA        = "dla"
ART_TYPE_MANDELBROT = "mandelbrot"
ART_TYPE_GOBAN      = "goban"
ART_TYPES           = [ART_TYPE_DLA, ART_TYPE_MANDELBROT, ART_TYPE_GOBAN]

# Mandelbrot colour options (must match the binary's colorMap)
MANDELBROT_COLOURS  = ["black", "white", "green", "blue", "red", "yellow", "orange"]
MANDELBROT_MODES    = ["single", "zoom_sequence"]

# Goban options (kept here for reference; entity modules import the
# canonical lists directly from art_generator.py)
GOBAN_SOURCES = ["library", "url", "inline"]
