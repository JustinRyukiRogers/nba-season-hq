"""Should a game cut short (an ejection, an early injury exit, foul trouble) count in a player's minutes?

The shipped minutes model (seasonkit/model.py) averages a player's last MINUTES_RECENT games' minutes,
recent games weighted more, and blends that with his preseason minutes. One game far under his usual
minutes drags it down: the 2026-10-26 sweep found Tari Eason at 22.8 expected minutes because his
opener lasted 3 (ejected), against 27, 31 and 33 since.

    shipped   the model as shipped: the bump rule too (MINUTES_BUMP 5, km 2 above it, 4 otherwise)
    cut f     the same, leaving out of the minutes average (and its game count) any game under f x
              the median minutes of his last MINUTES_RECENT games; kept in the points-a-minute totals
    cut f+r   and left out of the points-a-minute totals as well

The logs carry no reason for a short game, so the rule can't tell an ejection from a benching; a
real demotion shows as several short games, which moves the median, so it is not dropped for long.

Rows and scoring from research/minutes_backtest.py (its saved rows: run that first). f is fitted on
three seasons and scored on the fourth, each in turn; error is RMSE in points a game, weighted by
games. Also the rows the rule touches (a cut-short game in his last 12, or in his last 3), and the
pick test: the 3 best from a waiver-level pool at each checkpoint, scored on their next week.

Results (2026-10-27), change in RMSE (points a game) against shipped, by held-out season 2022 2024 2025 2026:
    cut 0.25 (fitted f in every fold)  week +0.038 +0.020 +0.003 +0.016   ros +0.076 +0.030 +0.011 +0.044
        on the rows it touches (9.5%) week +0.25 and ros +0.38 on average; with a cut game in the
        last 3, +0.64 and +0.76. Larger f is worse still; leaving them out of the rate too, a little worse.
    picks  shipped 98.0 a week each; cut 0.25 96.9, 0.33 95.8, 0.40 95.1, 0.50 93.9
    narrower, the Eason case: one short game between normal ones (the game before and the next 2-3 at
        0.75 x his median or more; 2-7% of rows): week -0.001 to +0.005, ros -0.007 to +0.008, mixed
        by season; on the touched rows no better; picks 97.9-98.2 (noise)
So a short game carries information about the minutes to come; dropping it never helps, and the
isolated dip is a wash. Not shipped. A known one-off (an ejection) is a call for an analyst take.

    python3 research/cut_short_games.py
"""
import json
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from draftkit.config import HISTORY
from research import minutes_backtest as mb

SEASONS = mb.SEASONS
RECENT, HL, KR = 12, 5.0, 400.0           # MINUTES_RECENT, MINUTES_HALF_LIFE, RATE_PRIOR_MINUTES
BUMP, KM_UP, KM = 5.0, 2.0, 4.0           # MINUTES_BUMP, MINUTES_BUMP_PRIOR_GAMES, MINUTES_PRIOR_GAMES
FRACS = [0.25, 0.33, 0.4, 0.5]


def load():
    rows = json.loads((HISTORY / "minutes_backtest_rows.json").read_text())
    for r in rows:
        r["day"] = date(*map(int, r["day"].split("-")))
        tail = [m for m, _ in r["past"][-RECENT:]]
        r["med"] = statistics.median(tail)
        tot_min = sum(m for m, _ in r["past"])
        tot_pts = sum(f for _, f in r["past"])
        prate = (r["pfppg"] / r["pmpg"]) if r["pfppg"] and r["pmpg"] else (tot_pts / tot_min if tot_min else 0.8)
        r["prate"] = prate * (0.92 if r["pfppg"] and not r["board"] else 1.0)
    return rows


def model(r, f=None, rate_too=False):
    """Points a game: the shipped minutes x rate, leaving out games under f x his median (f None: none)."""
    tail = r["past"][-RECENT:]
    short = [f is not None and m < f * r["med"] for m, _ in tail]
    keep = [(i, m) for i, ((m, _), s) in enumerate(zip(tail, short)) if not s] or [(i, m) for i, (m, _) in enumerate(tail)]
    ws = [0.5 ** ((len(tail) - 1 - i) / HL) for i, _ in keep]
    played = sum(w * m for w, (_, m) in zip(ws, keep)) / sum(ws)
    pm = r["pmpg"] or played
    n = len(keep)
    k = KM_UP if played - pm >= BUMP else KM
    mins = max(0.0, min(40.0, pm + n / (n + k) * (played - pm)))
    games = r["past"]
    if rate_too and f is not None:
        cut = set(len(games) - len(tail) + i for i, s in enumerate(short) if s)
        games = [g for j, g in enumerate(games) if j not in cut]
    tot_min = sum(m for m, _ in games)
    tot_pts = sum(x for _, x in games)
    rate = (r["prate"] * KR + tot_pts) / (KR + tot_min)
    return mins * rate


def touched(r, f, last):
    return any(m < f * r["med"] for m, _ in r["past"][-RECENT:][-last:])


def main():
    rows = load()
    print("%d rows" % len(rows))
    for f in FRACS:
        print("  f %.2f: %.1f%% of rows have a cut-short game in the last 12, %.1f%% in the last 3" % (
            f, 100 * sum(touched(r, f, 12) for r in rows) / len(rows), 100 * sum(touched(r, f, 3) for r in rows) / len(rows)))
    for rate_too in (False, True):
        for target in ("week", "ros"):
            print("\n%s, cut games out of the minutes%s (f fitted on the other three seasons)" % (
                target, " and the rate" if rate_too else ""))
            print("  season  f      all rows            cut in last 12        cut in last 3")
            sums = defaultdict(float)
            for y in SEASONS:
                train = [r for r in rows if r["y"] != y]
                f = min(FRACS, key=lambda f: mb.score(train, lambda r: model(r, f, rate_too), target))
                te = [r for r in rows if r["y"] == y]
                out = []
                for sub in (te, [r for r in te if touched(r, f, 12)], [r for r in te if touched(r, f, 3)]):
                    a = mb.score(sub, model, target)
                    b = mb.score(sub, lambda r: model(r, f, rate_too), target)
                    out.append("%.3f -> %.3f (%+.3f)" % (a, b, b - a))
                    sums[len(out)] += b - a
                print("  %d  %.2f  %s" % (y, f, "   ".join(out)))
            print("  mean change   %+.3f                %+.3f                 %+.3f" % tuple(sums[i] / len(SEASONS) for i in (1, 2, 3)))
    for f in FRACS:
        print("\nfixed f %.2f, change by season (week | ros): %s | %s" % (f, " ".join(
            "%+.3f" % (mb.score(te, lambda r: model(r, f), "week") - mb.score(te, model, "week"))
            for te in ([r for r in rows if r["y"] == y] for y in SEASONS)), " ".join(
            "%+.3f" % (mb.score(te, lambda r: model(r, f), "ros") - mb.score(te, model, "ros"))
            for te in ([r for r in rows if r["y"] == y] for y in SEASONS))))
    mb.picks(rows, {"shipped": model, **{"cut %.2f" % f: (lambda r, f=f: model(r, f)) for f in FRACS}})


if __name__ == "__main__":
    main()
