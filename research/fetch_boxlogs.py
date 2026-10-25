"""Past seasons' game logs with the full box score, for testing usage and efficiency.

Same source as research/fetch_gamelogs.py (ESPN's public athlete gamelog), but each game keeps the
counting stats, so usage (shots, free throws and turnovers a minute, or a share of the team's) and
shooting efficiency can be rebuilt. One file per season:

    data/history/boxlogs_<year>.json
        {player id: [[date, game id, team, min, fpts, pts, fgm, fga, tpm, tpa, ftm, fta,
                      reb, ast, stl, blk, to], ...]}

    python3 research/fetch_boxlogs.py 2022 2024 2025 2026
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from draftkit.config import GP, HISTORY, TEAM_ALIASES
from draftkit.espn import stat_split
from draftkit.history import load_espn
from fetch_gamelogs import URL, fantasy, get, made_att


def box(row):
    """[pts, fgm, fga, tpm, tpa, ftm, fta, reb, ast, stl, blk, to] from a gamelog row
    (labels MIN FG FG% 3PT 3P% FT FT% REB AST BLK STL PF TO PTS)."""
    fgm, fga = made_att(row[1])
    tpm, tpa = made_att(row[3])
    ftm, fta = made_att(row[5])
    reb, ast, blk, stl, to, pts = (float(row[i]) for i in (7, 8, 9, 10, 12, 13))
    return [pts, fgm, fga, tpm, tpa, ftm, fta, reb, ast, stl, blk, to]


def one(pid, year):
    d = get(URL % (pid, year))
    if not d:
        return pid, None
    meta, out = d.get("events", {}), []
    for st in d.get("seasonTypes", []):
        if "Regular Season" not in st.get("displayName", ""):
            continue
        for cat in st.get("categories", []):
            for ev in cat.get("events") or []:
                m = meta.get(ev["eventId"])
                if not m or not ev.get("stats"):
                    continue
                day = (datetime.fromisoformat(m["gameDate"].replace("Z", "+00:00")) - timedelta(hours=6)).date()
                team = m.get("team", {}).get("abbreviation", "")
                try:
                    out.append([str(day), ev["eventId"], TEAM_ALIASES.get(team, team), float(ev["stats"][0]),
                                round(fantasy(ev["stats"]), 1)] + box(ev["stats"]))
                except (ValueError, IndexError):
                    continue
    out.sort()
    return pid, out


def fetch(year):
    path = HISTORY / ("boxlogs_%d.json" % year)
    have = json.loads(path.read_text()) if path.exists() else {}
    want = [pid for pid, pl in load_espn(year).items()
            if (a := stat_split(pl, "00%d" % year)) and a.get(GP, 0) > 0 and str(pid) not in have]
    print(year, "fetching", len(want), "players", flush=True)
    with ThreadPoolExecutor(8) as ex:
        for pid, log in ex.map(lambda p: one(p, year), want):
            if log is not None:
                have[str(pid)] = log
    path.write_text(json.dumps(have, separators=(",", ":")))
    print(year, "saved", len(have), "players,", sum(len(v) for v in have.values()), "games", flush=True)


if __name__ == "__main__":
    for y in sys.argv[1:] or ["2022", "2024", "2025", "2026"]:
        fetch(int(y))
