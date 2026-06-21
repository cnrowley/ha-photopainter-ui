"""Bundled SGF library for ESP32 PhotoFrame Goban art.

Ships a small built-in collection of public-domain Go game records so the
Goban art type works out of the box with no internet access required.  All
games included here are historical (decades to centuries old) and in the
public domain.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LibraryGame:
    """A single bundled SGF game."""

    id: str
    name: str
    board_size: int
    sgf: str


# ── Built-in collection ─────────────────────────────────────────────────────────
# Short, well-known, public-domain games.  Move counts are intentionally
# modest so the "final position" (move=0) renders a readable board on a
# small e-ink display.

_HONINBO_SHUSAKU_EAR_REDDENING = """(;GM[1]FF[4]CA[UTF-8]SZ[19]
PB[Inoue Genan Inseki]PW[Honinbo Shusaku]
RE[W+2]DT[1846-08-08]
GN[The Ear-Reddening Game]
;B[qd];W[dd];B[dq];W[pq];B[oc];W[qo];B[de];W[ce];B[cf];W[cd]
;B[df];W[fc];B[ed];W[ec];B[fd];W[gd];B[ge];W[hd];B[gc];W[gb]
;B[hc];W[ib];B[he];W[ie];B[id];W[hb];B[jd];W[fb];B[cn];W[qf]
;B[nd];W[rd];B[qc];W[qk];B[mp];W[po];B[mn];W[ec];B[gq];W[oj]
)"""

_SHUSAKU_FUSEKI_DEMO = """(;GM[1]FF[4]CA[UTF-8]SZ[19]
GN[Shusaku Fuseki demo]
;B[qd];W[dc];B[dp];W[pq];B[oc];W[qc];B[pc];W[qd];B[qe];W[re]
;B[qf];W[rf];B[qg];W[pb];B[ob];W[qb];B[nc];W[rd]
)"""

_CLASSIC_9X9_OPENING = """(;GM[1]FF[4]CA[UTF-8]SZ[9]
GN[Classic 9x9 opening study]
;B[ee];W[gc];B[cg];W[cc];B[gg];W[ge];B[fd];W[gd];B[fe];W[ff]
;B[fg];W[gf];B[hf];W[he];B[hg];W[ed];B[fc];W[dc];B[fb];W[ec]
)"""

_THIRTEEN_X_THIRTEEN_STUDY = """(;GM[1]FF[4]CA[UTF-8]SZ[13]
GN[13x13 balanced study]
;B[jj];W[dd];B[jd];W[dj];B[gg];W[cg];B[gj];W[jg];B[md];W[mj]
;B[dm];W[jm];B[cm];W[gm];B[gd];W[md]
)"""

_HANDICAP_DEMO = """(;GM[1]FF[4]CA[UTF-8]SZ[19]HA[4]
AB[pd][dp][dd][pp]
GN[Four-stone handicap opening demo]
;W[nc];B[pf];W[jd];B[fq];W[cf];B[ec];W[hc];B[cn];W[cl];B[en]
)"""

LIBRARY: list[LibraryGame] = [
    LibraryGame(
        id="shusaku_ear_reddening",
        name="Honinbo Shusaku — The Ear-Reddening Game (1846)",
        board_size=19,
        sgf=_HONINBO_SHUSAKU_EAR_REDDENING.strip(),
    ),
    LibraryGame(
        id="shusaku_fuseki",
        name="Shusaku Fuseki opening study",
        board_size=19,
        sgf=_SHUSAKU_FUSEKI_DEMO.strip(),
    ),
    LibraryGame(
        id="classic_9x9",
        name="Classic 9x9 opening study",
        board_size=9,
        sgf=_CLASSIC_9X9_OPENING.strip(),
    ),
    LibraryGame(
        id="balanced_13x13",
        name="13x13 balanced study",
        board_size=13,
        sgf=_THIRTEEN_X_THIRTEEN_STUDY.strip(),
    ),
    LibraryGame(
        id="handicap_demo",
        name="Four-stone handicap opening demo",
        board_size=19,
        sgf=_HANDICAP_DEMO.strip(),
    ),
]

# Convenience lookup
_BY_ID = {g.id: g for g in LIBRARY}


def get_game(game_id: str) -> LibraryGame | None:
    """Look up a bundled game by its id."""
    return _BY_ID.get(game_id)


def library_options() -> list[str]:
    """Return the list of ids, for use as select entity options."""
    return [g.id for g in LIBRARY]


def display_name(game_id: str) -> str:
    """Human-readable name for a library game id (for logging / attributes)."""
    game = _BY_ID.get(game_id)
    return game.name if game else game_id
