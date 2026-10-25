"""Past seasons' game logs from ESPN, for testing in-season projections.

Every player with games in a stored season (data/history/espn_<year>.json) gets his regular-season
log from ESPN's public athlete gamelog. Saved compactly, one file per season:

    data/history/gamelogs_<year>.json   {player id: [[date, game id, team, min, fpts], ...]}

    python3 research/fetch_gamelogs.py 2022 2024 2025 2026
"""
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from draftkit.config import GP, HISTORY, TEAM_ALIASES
from draftkit.espn import stat_split
from draftkit.history import load_espn

URL = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/%d/gamelog?season=%d"


def get(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(2 * (i + 1))


def made_att(s):
    m, a = s.split("-")
    return float(m), float(a)


def fantasy(row):
    """ESPN default points from a gamelog row (labels MIN FG FG% 3PT 3P% FT FT% REB AST BLK STL PF TO PTS)."""
    fgm, fga = made_att(row[1])
    tpm, _ = made_att(row[3])
    ftm, fta = made_att(row[5])
    reb, ast, blk, stl, to, pts = (float(row[i]) for i in (7, 8, 9, 10, 12, 13))
    return pts + tpm - fga + 2 * fgm - fta + ftm + reb + 2 * ast + 4 * stl + 4 * blk - 2 * to


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
                # gameDate is the tip in UTC; ET evening games land on the next UTC day
                day = (datetime.fromisoformat(m["gameDate"].replace("Z", "+00:00")) - timedelta(hours=6)).date()
                team = m.get("team", {}).get("abbreviation", "")
                try:
                    out.append([str(day), ev["eventId"], TEAM_ALIASES.get(team, team), float(ev["stats"][0]),
                                round(fantasy(ev["stats"]), 1)])
                except (ValueError, IndexError):
                    continue
    out.sort()
    return pid, out


def fetch(year):
    path = HISTORY / ("gamelogs_%d.json" % year)
    have = json.loads(path.read_text()) if path.exists() else {}
    want = [pid for pid, pl in load_espn(year).items()
            if (a := stat_split(pl, "00%d" % year)) and a.get(GP, 0) > 0 and str(pid) not in have]
    print(year, "fetching", len(want), "players")
    with ThreadPoolExecutor(8) as ex:
        for pid, log in ex.map(lambda p: one(p, year), want):
            if log is not None:
                have[str(pid)] = log
    path.write_text(json.dumps(have, separators=(",", ":")))
    print(year, "saved", len(have), "players,", sum(len(v) for v in have.values()), "games")


if __name__ == "__main__":
    for y in sys.argv[1:] or ["2024", "2025", "2026"]:
        fetch(int(y))
