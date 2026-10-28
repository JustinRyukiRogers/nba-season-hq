"""How often does a player tagged Probable, Questionable or Doubtful on the NBA's official injury report play?

The HQ reads ESPN's injury feed, which folds all three into one Day-To-Day tag, and gives every
day-to-day player DTD_PLAY (0.6) for his next game (seasonkit/model.py). The NBA's own report keeps
them apart and is updated through the day. Its PDFs are archived at ak-static.cms.nba.com, so the
play rate of each tag can be measured: every game day of two seasons, the report at 1 PM and at 5 PM
ET, joined to who played (data/history/gamelogs_YYYY.json, minutes > 0).

Only players in the game logs count (the ones that matter for fantasy; two-way and G League
assignments mostly drop out). Minutes when he plays are compared with his average over his previous
five games, to see whether a tagged player who plays is held back.

Results (2026-10-28): 2024-25 334 of 336 reports, 2025-26 164 of 338 (the archive refused requests partway
through the download, so later days are missing). Play rate, 1 PM / 5 PM report:
    Probable      92.6% / 94.5%  (n 674 / 634; 2024-25 92.0 / 94.3, 2025-26 91.6 / 92.2)
    Questionable  50.3% / 49.5%  (n 1865 / 1587; 49.5 / 48.7, 51.5 / 51.1)
    Doubtful       4.0% /  1.6%  (n 227 / 187)
    Out            0.3% /  0.1%
    Available     87.8% / 89.1%  (cleared; treated as untagged)
A tagged player who plays loses about a minute against his last five games (Questionable -1.0 to -1.2,
Probable -0.5 to -0.8): too little to model. The HQ's flat DTD_PLAY 0.6 for every day-to-day player
undervalued Probable by a third and overvalued Doubtful. Shipped as model.TAG_PLAY (Probable 0.93,
Questionable 0.5, Doubtful 0.03, Out 0). This run's parser dropped rows whose name shares a line with the
game and team columns (the first player listed for a team in a game); fixed since (pull.parse_report),
and there is no reason the dropped rows play at a different rate.

    python3 research/injury_tags.py          # downloads ~700 PDFs once (cached as parsed JSON), needs pdftotext
"""
import json
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from seasonkit import pull  # noqa: E402

HISTORY = ROOT / "data" / "history"
SEASONS = [2025, 2026]
HOURS = [1, 5]                       # PM ET
TAGS = ["Available", "Probable", "Questionable", "Doubtful", "Out"]


def norm(name):
    name = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", name.lower())
    return re.sub(r"[^a-z]", "", name)


def fetch(day, hour):
    """-> {normalized "first last": tag} for games on `day` from the report at `hour` PM, or None."""
    for stamp in ("%02dPM" % hour, "%02d_00PM" % hour, "%02d_30PM" % hour):
        text = pull.report_text(day, stamp)
        if text:
            return {norm(name): t[str(day)] for name, t in pull.parse_report(text).items() if str(day) in t}
    return None


def reports(year, days):
    path = HISTORY / ("injury_reports_%d.json" % year)
    cache = json.loads(path.read_text()) if path.exists() else {}
    todo = [(d, h) for d in days for h in HOURS if "%s %d" % (d, h) not in cache]
    if todo:
        print("%d: fetching %d reports..." % (year, len(todo)), file=sys.stderr)
        with ThreadPoolExecutor(8) as ex:
            for (d, h), rep in zip(todo, ex.map(lambda x: fetch(*x), todo)):
                cache["%s %d" % (d, h)] = rep
        path.write_text(json.dumps(cache))
    return cache


def names(year):
    data = json.loads((HISTORY / ("espn_%d.json" % year)).read_text())
    return {norm(p["player"]["fullName"]): str(p["id"]) for p in data["players"]}


def main():
    total = defaultdict(lambda: [0, 0, 0.0, 0.0, 0])   # (hour, tag) -> [listed, played, minutes, usual minutes, with usual]
    for year in SEASONS:
        logs = json.loads((HISTORY / ("gamelogs_%d.json" % year)).read_text())
        games = {pid: [(date(*map(int, r[0].split("-"))), r[2], r[3]) for r in rows] for pid, rows in logs.items()}
        team_days = defaultdict(set)
        for g in games.values():
            for day, team, _ in g:
                team_days[team].add(day)
        days = sorted({d for s in team_days.values() for d in s})
        reps = reports(year, days)
        ids = names(year)
        found = sum(1 for k, v in reps.items() if v is not None)
        print("%d: %d game days, %d of %d reports found" % (year, len(days), found, len(reps)))
        for key, rep in reps.items():
            if not rep:
                continue
            ds, h = key.split()
            day = date.fromisoformat(ds)
            for nm, tag in rep.items():
                pid = ids.get(nm)
                if not pid or pid not in games:
                    continue
                g = games[pid]
                before = [x for x in g if x[0] < day]
                if not before or day not in team_days[before[-1][1]]:
                    continue                    # no game for his team that day (or no history yet)
                row = total[(int(h), tag)]
                row[0] += 1
                today = [x for x in g if x[0] == day and x[2] > 0]
                if today:
                    row[1] += 1
                    recent = [x[2] for x in before if x[2] > 0][-5:]
                    if recent:
                        row[2] += today[0][2]
                        row[3] += sum(recent) / len(recent)
                        row[4] += 1
    print("\nreport  tag            listed  played   minutes when he plays vs his last 5")
    for h in HOURS:
        for tag in TAGS:
            n, p, m, u, k = total[(h, tag)]
            if n:
                print("%2d PM   %-13s %6d  %5.1f%%   %s" % (h, tag, n, 100 * p / n,
                      "%.1f vs %.1f (%+.1f)" % (m / k, u / k, (m - u) / k) if k else "-"))


if __name__ == "__main__":
    main()
