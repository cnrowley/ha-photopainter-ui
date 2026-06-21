"""Generative art CLI wrappers for ESP32 PhotoFrame.

Each generator calls an external CLI program and returns the resulting BMP
image as raw bytes.  All generators are run via asyncio subprocesses so they
never block the event loop.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DLA  –  dla.x
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Stateful / directory-based:

    dla.x <out_dir> --init       # start a new sequence; writes checkpoints
    dla.x <out_dir> --to <N>     # advance to frame N; writes latest_display.bmp

Directory layout after ``--to``:

    <out_dir>/
        checkpoint.bin
        checkpoint.json
        latest_display.bmp   ← served to the display

The sequence loops at DLA_SEQUENCE_LENGTH (120) frames.  Frame 1 triggers
``--init`` automatically.  The temp directory is deleted after every call.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Mandelbrot fractal  –  mandelbrot.x
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    mandelbrot.x \
        -width  <int>          (default 600) \
        -height <int>          (default 448) \
        -out    <dir>          output directory; writes <dir>/current.bmp \
        -single                generate exactly one frame (no sequence) \
        -frames <int>          number of frames for a zoom sequence \
        -state  <path>         JSON state file for zoom continuation \
        -fg     <colour>       foreground colour name \
        -bg     <colour>       background colour name

    Available colours: black white green blue red yellow orange

Two modes are supported:

Single-frame (``-single``):
    Generates one frame of the fractal (at the current zoom position stored
    in the state file, or the default starting position).  Writes
    ``<out>/current.bmp`` and exits.

Zoom sequence (``-frames N``):
    Generates N frames advancing a zoom sequence, persisting position in a
    JSON state file.  We use this in "sequence mode" (like DLA) where each
    Generate press advances the zoom one step.

The wrapper creates a temp directory for ``-out``, optionally copies in the
persistent state file before calling the binary, then copies the state file
back (if it changed) and reads ``current.bmp``.  The out directory is always
deleted afterwards; the state file lives in a persistent location under
HA's config directory.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Goban  –  goban.x
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    goban.x \
        -input          <sgf_file_path>      (required) \
        -move           <int>                 move number, 0 = final position \
        -output         <bmp_file_path>        (default "frame.bmp") \
        -bg             white|black            board background \
        -board          yellow|white            board colour \
        -white-color    white|green|blue|red    stone colour for White \
        -black-color    black|red               stone colour for Black \
        -grid-thickness 1|2                     grid line weight \
        -highlight      dot|ring|none            last-move marker style

``goban.x`` takes an SGF **file path**, not an inline string, so the wrapper
always materialises the SGF text to a temp file first.  The SGF source can
come from three places, resolved in this order by ``GobanParams.sgf_source``:

    "inline"  – ``sgf_text`` is used directly (paste-your-own)
    "library" – a bundled game from ``sgf_library.py`` is looked up by id
    "url"     – the SGF is downloaded from ``sgf_url`` at generation time

Downloaded / library SGF text is written to a temp file alongside the BMP
output, and the whole temp directory is deleted after the BMP is read.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field

import aiohttp

_LOGGER = logging.getLogger(__name__)

# ── Display geometry ───────────────────────────────────────────────────────────
DISPLAY_WIDTH  = 600
DISPLAY_HEIGHT = 448

# ── DLA sequence config ────────────────────────────────────────────────────────
DLA_SEQUENCE_LENGTH = 120

# ── Mandelbrot colour options (must match the binary's colorMap) ───────────────
MANDELBROT_COLOURS = ["black", "white", "green", "blue", "red", "yellow", "orange"]

# ── Goban colour / style options (must match the binary's flag.String choices) ─
GOBAN_BG_COLOURS        = ["white", "black"]
GOBAN_BOARD_COLOURS     = ["yellow", "white"]
GOBAN_WHITE_STONE_COLOURS = ["white", "green", "blue", "red"]
GOBAN_BLACK_STONE_COLOURS = ["black", "red"]
GOBAN_GRID_THICKNESS    = [1, 2]
GOBAN_HIGHLIGHT_MODES   = ["dot", "ring", "none"]

# Max SGF download size (bytes) – guards against pathological/huge responses
SGF_DOWNLOAD_MAX_BYTES = 2 * 1024 * 1024   # 2 MB

# ── CLI executable names (override with env vars if not on $PATH) ──────────────
DLA_CLI        = os.environ.get("DLA_GENERATOR_CMD",        "dla.x")
MANDELBROT_CLI = os.environ.get("MANDELBROT_GENERATOR_CMD", "mandelbrot.x")
GOBAN_CLI      = os.environ.get("GOBAN_GENERATOR_CMD",      "goban.x")


# ── Parameter dataclasses ──────────────────────────────────────────────────────

@dataclass
class DLAParams:
    """Parameters for the DLA sequence generator.

    ``frame`` is the 1-based position in the current sequence.
    Frame 1 triggers a fresh ``--init`` automatically.
    """
    frame: int = 1      # 1 … DLA_SEQUENCE_LENGTH


@dataclass
class MandelbrotParams:
    """Parameters for mandelbrot.x.

    Two modes:
    - ``single=True``  → generate one frame at the current zoom position.
    - ``single=False`` → advance ``frames`` steps in a persistent zoom
                         sequence (state file is carried across calls).

    ``state_path`` is the full filesystem path of the persistent JSON state
    file.  When empty (first call) the binary starts from the default position
    and creates the file.  Subsequent calls pass the same path so the zoom
    sequence continues from where it left off.

    ``fg`` and ``bg`` are colour names from MANDELBROT_COLOURS.
    """
    width:      int  = DISPLAY_WIDTH
    height:     int  = DISPLAY_HEIGHT
    fg:         str  = "white"
    bg:         str  = "black"
    single:     bool = True      # True = one frame; False = sequence step
    frames:     int  = 1         # ignored when single=True
    state_path: str  = ""        # path to persistent state JSON; "" = fresh start


@dataclass
class GobanParams:
    """Parameters for goban.x.

    ``sgf_source`` selects where the SGF text comes from:
        "inline"  – ``sgf_text`` holds the literal SGF content
        "library" – ``library_id`` references a bundled game in sgf_library.py
        "url"     – ``sgf_url`` is downloaded fresh at generation time

    ``move`` is passed straight through (0 = final position in the file).

    Colour / style fields map 1:1 onto goban.x flags; see the choices lists
    GOBAN_BG_COLOURS, GOBAN_BOARD_COLOURS, GOBAN_WHITE_STONE_COLOURS,
    GOBAN_BLACK_STONE_COLOURS, GOBAN_GRID_THICKNESS, GOBAN_HIGHLIGHT_MODES.
    """
    sgf_source:      str = "library"   # inline | library | url
    sgf_text:        str = ""          # used when sgf_source == "inline"
    library_id:      str = ""          # used when sgf_source == "library"
    sgf_url:         str = ""          # used when sgf_source == "url"

    move:            int = 0
    bg:              str = "white"
    board:           str = "yellow"
    white_color:     str = "green"
    black_color:     str = "black"
    grid_thickness:  int = 1
    highlight:       str = "ring"


# ── Low-level subprocess helper ────────────────────────────────────────────────

async def _run(argv: list[str]) -> tuple[str, str]:
    """Run a subprocess; raise RuntimeError on non-zero exit.

    Returns (stdout_text, stderr_text) for the caller to log or inspect.
    """
    cmd_str = " ".join(str(a) for a in argv)
    _LOGGER.debug("Running: %s", cmd_str)

    proc = await asyncio.create_subprocess_exec(
        *[str(a) for a in argv],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    stdout_t = stdout_b.decode(errors="replace").strip()
    stderr_t = stderr_b.decode(errors="replace").strip()

    if proc.returncode != 0:
        raise RuntimeError(
            f"Command failed (exit {proc.returncode}): {cmd_str!r}\n{stderr_t}"
        )

    if stdout_t:
        _LOGGER.debug("stdout: %s", stdout_t)
    return stdout_t, stderr_t


# ── DLA generator ──────────────────────────────────────────────────────────────

async def generate_dla(params: DLAParams) -> bytes:
    """Run the DLA sequence and return the BMP for the requested frame.

    Lifecycle
    ---------
    1. Create a fresh temp directory.
    2. If ``frame == 1``:  run ``dla.x <dir> --init``.
    3. Run ``dla.x <dir> --to <frame>``.
    4. Read ``<dir>/latest_display.bmp`` into bytes.
    5. Delete temp directory unconditionally (``finally``).

    The caller tracks the frame counter (``DLASequenceManager``).
    """
    out_dir = tempfile.mkdtemp(prefix="dla_")
    _LOGGER.info("DLA: frame=%d, out_dir=%s", params.frame, out_dir)

    try:
        if params.frame == 1:
            _LOGGER.info("DLA: running --init for new sequence")
            await _run([DLA_CLI, out_dir, "--init"])

        _LOGGER.info("DLA: running --to %d", params.frame)
        await _run([DLA_CLI, out_dir, "--to", str(params.frame)])

        bmp_path = os.path.join(out_dir, "latest_display.bmp")
        if not os.path.exists(bmp_path) or os.path.getsize(bmp_path) == 0:
            raise RuntimeError(
                f"DLA produced no image at {bmp_path} after --to {params.frame}"
            )

        with open(bmp_path, "rb") as fh:
            data = fh.read()

        _LOGGER.info("DLA: read %d bytes", len(data))
        return data

    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
        _LOGGER.debug("DLA: deleted temp dir %s", out_dir)


# ── Mandelbrot generator ───────────────────────────────────────────────────────

async def generate_mandelbrot(params: MandelbrotParams) -> bytes:
    """Run mandelbrot.x and return the BMP bytes.

    The binary writes ``<out_dir>/current.bmp``.  If a ``state_path`` is
    provided and exists it is copied into the temp directory as ``state.json``
    before the call so the binary can continue a zoom sequence; after a
    successful run the (possibly updated) state file is copied back to
    ``state_path`` for persistence.

    The temp output directory is always deleted after the BMP is read.
    The state file at ``params.state_path`` is *not* deleted – it persists
    across HA restarts so zoom sequences survive.
    """
    out_dir = tempfile.mkdtemp(prefix="mandelbrot_")
    state_in = os.path.join(out_dir, "state.json")
    _LOGGER.info(
        "Mandelbrot: single=%s, fg=%s, bg=%s, state=%s, out_dir=%s",
        params.single, params.fg, params.bg, params.state_path or "(none)", out_dir,
    )

    try:
        # Copy state file into the temp dir if it exists
        if params.state_path and os.path.isfile(params.state_path):
            shutil.copy2(params.state_path, state_in)
            _LOGGER.debug("Mandelbrot: copied state from %s", params.state_path)
            state_arg = state_in
        else:
            state_arg = ""     # binary will start fresh

        argv = [
            MANDELBROT_CLI,
            "-width",  str(params.width),
            "-height", str(params.height),
            "-out",    out_dir,
            "-fg",     params.fg,
            "-bg",     params.bg,
        ]

        if params.single:
            argv.append("-single")
        else:
            argv += ["-frames", str(max(1, params.frames))]

        if state_arg:
            argv += ["-state", state_arg]

        await _run(argv)

        # Read the output image
        bmp_path = os.path.join(out_dir, "current.bmp")
        if not os.path.exists(bmp_path) or os.path.getsize(bmp_path) == 0:
            raise RuntimeError(
                f"mandelbrot.x produced no image at {bmp_path}"
            )

        with open(bmp_path, "rb") as fh:
            data = fh.read()
        _LOGGER.info("Mandelbrot: read %d bytes", len(data))

        # Persist updated state file back to the configured location
        if params.state_path and os.path.isfile(state_in):
            os.makedirs(os.path.dirname(params.state_path), exist_ok=True)
            shutil.copy2(state_in, params.state_path)
            _LOGGER.debug("Mandelbrot: saved state to %s", params.state_path)

        return data

    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
        _LOGGER.debug("Mandelbrot: deleted temp dir %s", out_dir)


# ── Goban SGF resolution ────────────────────────────────────────────────────────

async def _resolve_sgf_text(params: GobanParams) -> str:
    """Return the literal SGF text to render, based on params.sgf_source."""
    source = params.sgf_source

    if source == "inline":
        if not params.sgf_text.strip():
            raise RuntimeError("sgf_source is 'inline' but sgf_text is empty")
        return params.sgf_text.strip()

    if source == "library":
        from . import sgf_library

        game = sgf_library.get_game(params.library_id)
        if game is None:
            raise RuntimeError(
                f"Unknown library SGF id {params.library_id!r}. "
                f"Available: {sgf_library.library_options()}"
            )
        _LOGGER.info("Goban: using bundled library game %r (%s)", game.id, game.name)
        return game.sgf

    if source == "url":
        if not params.sgf_url.strip():
            raise RuntimeError("sgf_source is 'url' but sgf_url is empty")
        return await _download_sgf(params.sgf_url.strip())

    raise RuntimeError(f"Unknown sgf_source: {source!r}")


async def _download_sgf(url: str) -> str:
    """Download an SGF file from a URL and return its text content.

    Enforces a max size to avoid pathological downloads and a reasonable
    timeout.  Raises RuntimeError on any failure.
    """
    _LOGGER.info("Goban: downloading SGF from %s", url)
    timeout = aiohttp.ClientTimeout(total=15)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"Failed to download SGF: HTTP {response.status} from {url}"
                    )

                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > SGF_DOWNLOAD_MAX_BYTES:
                    raise RuntimeError(
                        f"SGF download too large ({content_length} bytes) from {url}"
                    )

                data = await response.content.read(SGF_DOWNLOAD_MAX_BYTES + 1)
                if len(data) > SGF_DOWNLOAD_MAX_BYTES:
                    raise RuntimeError(
                        f"SGF download exceeded {SGF_DOWNLOAD_MAX_BYTES} bytes from {url}"
                    )

                text = data.decode("utf-8", errors="replace").strip()
                if not text.startswith("("):
                    raise RuntimeError(
                        f"Downloaded content from {url} does not look like SGF "
                        f"(expected to start with '('): {text[:60]!r}"
                    )

                _LOGGER.info("Goban: downloaded %d bytes of SGF from %s", len(data), url)
                return text

    except aiohttp.ClientError as err:
        raise RuntimeError(f"Failed to download SGF from {url}: {err}") from err


# ── Goban generator ────────────────────────────────────────────────────────────

async def generate_goban(params: GobanParams) -> bytes:
    """Resolve the SGF source, run goban.x, and return BMP bytes.

    Lifecycle
    ---------
    1. Resolve the SGF text (inline / bundled library / downloaded from URL).
    2. Create a temp directory; write the SGF text to ``game.sgf`` inside it.
    3. Run ``goban.x -input game.sgf -output frame.bmp ...``.
    4. Read ``frame.bmp`` into bytes.
    5. Delete the temp directory unconditionally.
    """
    sgf_text = await _resolve_sgf_text(params)

    work_dir = tempfile.mkdtemp(prefix="goban_")
    sgf_path = os.path.join(work_dir, "game.sgf")
    output_path = os.path.join(work_dir, "frame.bmp")

    try:
        with open(sgf_path, "w", encoding="utf-8") as fh:
            fh.write(sgf_text)

        argv = [
            GOBAN_CLI,
            "-input",          sgf_path,
            "-move",           str(params.move),
            "-output",         output_path,
            "-bg",             params.bg,
            "-board",          params.board,
            "-white-color",    params.white_color,
            "-black-color",    params.black_color,
            "-grid-thickness", str(params.grid_thickness),
            "-highlight",      params.highlight,
        ]

        await _run(argv)

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError(f"goban.x produced no output at {output_path}")

        with open(output_path, "rb") as fh:
            data = fh.read()

        _LOGGER.info("Goban: read %d bytes from %s", len(data), output_path)
        return data

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        _LOGGER.debug("Goban: deleted temp dir %s", work_dir)
