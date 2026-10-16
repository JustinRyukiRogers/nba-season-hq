"""Paths, constants and the few knobs the valuation depends on."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"            # latest pulls for the current season
HISTORY = DATA / "history"    # past seasons, used to fit the calibration
STATIC = Path(__file__).resolve().parent / "static"

SEASON = 2027                 # ESPN names a season by its ending year: 2026-27 == 2027
LEAGUE_ID = 1997238870
PORT = 8765

# ESPN stat ids
PTS, BLK, STL, AST, REB, TO, FGM, FGA, FTM, FTA, TPM, TPA, MIN, GP = (
    "0", "1", "2", "3", "6", "11", "13", "14", "15", "16", "17", "18", "40", "42")
STAT_NAMES = {PTS: "PTS", BLK: "BLK", STL: "STL", AST: "AST", REB: "REB", TO: "TO", FGM: "FGM", FGA: "FGA",
              FTM: "FTM", FTA: "FTA", TPM: "3PM", TPA: "3PA", MIN: "MIN", GP: "GP"}
# ESPN's default points-league scoring; the league file overrides it if the league differs
DEFAULT_SCORING = {PTS: 1, TPM: 1, FGA: -1, FGM: 2, FTA: -1, FTM: 1, REB: 1, AST: 2, STL: 4, BLK: 4, TO: -2}

# ESPN lineup slot ids
SLOT_NAMES = {0: "PG", 1: "SG", 2: "SF", 3: "PF", 4: "C", 5: "G", 6: "F", 7: "SG/SF", 8: "G/F", 9: "PF/C",
              10: "F/C", 11: "UT", 12: "BE", 13: "IR"}
POSITIONS = ["PG", "SG", "SF", "PF", "C"]

# ESPN pro team id -> abbreviation (Basketball Reference spellings, which the history files use)
TEAMS = {1: "ATL", 2: "BOS", 3: "NOP", 4: "CHI", 5: "CLE", 6: "DAL", 7: "DEN", 8: "DET", 9: "GSW", 10: "HOU",
         11: "IND", 12: "LAC", 13: "LAL", 14: "MIA", 15: "MIL", 16: "MIN", 17: "BRK", 18: "NYK", 19: "ORL",
         20: "PHI", 21: "PHO", 22: "POR", 23: "SAC", 24: "SAS", 25: "OKC", 26: "UTA", 27: "WAS", 28: "TOR",
         29: "MEM", 30: "CHO"}
# other sources' spellings -> ours
TEAM_ALIASES = {"BKN": "BRK", "CHA": "CHO", "PHX": "PHO", "PHL": "PHI", "GS": "GSW", "NO": "NOP", "NY": "NYK",
                "SA": "SAS", "UTAH": "UTA", "WSH": "WAS", "NOR": "NOP"}

# ---- valuation knobs (see board.py for how each is used) ----
# Share of a held player's missed games that a waiver pickup ends up covering. Long absences go to
# an IR slot and get replaced; one-off rest days mostly don't, because the 7 weekly adds are already
# being spent on streaming.
BACKFILL = 0.6
REPLACEMENT_AVAILABILITY = 0.88   # a replacement-level player is swapped out when hurt
REPLACEMENT_RANK_PAD = 6          # replacement = the (teams x roster + pad)th healthy player
PLAYOFF_WEEK_WEIGHT = 1.5         # a game in fantasy playoff weeks counts this much
# Players who start the year on a long layoff come back later than announced more often than not:
# in the backtest, those ESPN projected for under 55 games played about 40% of that projection.
# The sample is tiny (8 players), so these are deliberately rough.
LONG_ABSENCE_DAYS, LONG_ABSENCE_FACTOR = 60, 0.55
SHORT_ABSENCE_DAYS, SHORT_ABSENCE_FACTOR = 14, 0.90
ESPN_WEIGHT = 0.7                 # baseline per-game blend: ESPN vs FantasyPros
INTEL_CONFIDENCE = {"high": 1.0, "medium": 0.7, "low": 0.4}
INTEL_MAX_MINUTES = 6.0           # cap on a research adjustment to minutes per game
INTEL_MAX_RATE = 0.10             # cap on a research adjustment to per-minute production


def snake(order, rounds):
    """Round-1 order -> the team on the clock for every pick of a snake draft."""
    return [t for r in range(rounds) for t in (order if r % 2 == 0 else order[::-1])]


def team_abbrev(name):
    name = (name or "").upper().strip()
    return TEAM_ALIASES.get(name, name)


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path, obj, indent=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=indent, ensure_ascii=False))
    tmp.replace(path)


def load_env():
    """ESPN login cookies from .env (ESPN_S2=..., SWID=...) or the environment. -> dict or None."""
    env = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip().upper()] = value.strip().strip("'\"")     # espn_s2= works too
    s2 = env.get("ESPN_S2") or os.environ.get("ESPN_S2")
    swid = env.get("SWID") or os.environ.get("SWID")
    if s2 and swid:
        return {"espn_s2": s2, "SWID": swid}
    return None


def load_league():
    league = read_json(DATA / "league.json")
    if league is None:
        raise SystemExit("data/league.json is missing. Run: python3 -m draftkit refresh")
    return league
