"""When a teammate a player has been covering for comes back, should his expected minutes drop ahead of it?

The shipped minutes model (seasonkit/model.py) averages a player's recent minutes, recent games
weighted more, blended with his preseason minutes, and trusts a jump of MINUTES_BUMP+ faster
(research/teammate_out_minutes.py). It does not know when an absence ends: a fill-in keeps his
inflated minutes until his own average catches up after the starter is back. The 2026-10-28 sweep
raised it (Alex Caruso, Hayes, Champagnie: streams and hunches chase exactly these players).

Rows are the saved checkpoints of research/minutes_backtest.py (run that first) where a teammate who
matters (15+ minutes) is out now. For each, the teammate's actual return (his next game for the team)
stands in for the injury report's return date, so this is an upper bound on what a return date is
worth: real dates are guesses and move. The rule:

    with      the player's minutes in his last games played alongside that teammate (at least
              WITH_MIN of them, from this season)
    f         the share of the window's games (next week, or rest of season) after the return
    minutes   shipped minutes x (1 - f)  +  f x (w x with + (1 - w) x shipped minutes)

w is fitted on three seasons and scored on the fourth, each in turn (w 0 is the shipped model).
Returns within the week matter most for streams; rest of season for values. Error is RMSE in points
a game weighted by games, on every row (the rule changes only rows with a returning teammate) and on
the rows it touches; plus the pick test (3 best from a waiver-level pool at each checkpoint, next
week's points).

Results (2026-10-28), RMSE in points a game by season 2022 2024 2025 2026, change against shipped:
    week, w fitted per fold (0.25, 0, 0.25, 0): all rows +0.004 +0.000 +0.002 +0.000; covering rows whose
        teammate returns in the week +0.066 +0.000 +0.032 +0.000. Fixed w 0.5: +0.013 -0.004 +0.008 +0.008.
    ros, w fitted 0 in every fold; fixed w 0.5: +0.028 to +0.059, worse in all four.
    picks: shipped 98.0, w 0.5 98.4 (+0.3 +1.2 +0.9 -0.9 by season, noise), w 1 97.5
    why: covering 3+ minutes over his minutes with the teammate, teammate back within the week (2387 rows):
        shipped expects 23.6 minutes, his minutes with the teammate were 18.4, he played 22.9.
A fill-in keeps most of his minutes when the starter comes back (the role was earned, or the starter
is eased in), so the shipped model's persistence is right, even knowing the exact return date (an
upper bound: real return dates are guesses). Not shipped.

    python3 research/teammate_return.py
"""
import bisect
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research import cut_short_games as cs
from research import minutes_backtest as mb

SEASONS = mb.SEASONS
WITH_MIN = 3                 # games alongside the returning teammate before his minutes with him count
WITH_LAST = 10               # the most recent games alongside him that count
W_GRID = [0.0, 0.25, 0.5, 0.75, 1.0]


def shipped(r):
    """-> (minutes, rate): the shipped model, bump rule included (as cut_short_games.model with f None)."""
    tail = r["past"][-cs.RECENT:]
    ws = [0.5 ** ((len(tail) - 1 - i) / cs.HL) for i in range(len(tail))]
    played = sum(w * m for w, (m, _) in zip(ws, tail)) / sum(ws)
    pm = r["pmpg"] or played
    n = len(tail)
    k = cs.KM_UP if played - pm >= cs.BUMP else cs.KM
    mins = max(0.0, min(40.0, pm + n / (n + k) * (played - pm)))
    rate = (r["prate"] * cs.KR + sum(f for _, f in r["past"])) / (cs.KR + sum(m for m, _ in r["past"]))
    return mins, rate


def augment(rows):
    """Adds to each row with a teammate out now: the returning teammate's effect (with, f_week, f_ros)."""
    by_year = defaultdict(list)
    for r in rows:
        if r["now_who"]:
            by_year[r["y"]].append(r)
    touched = 0
    for y, rs in by_year.items():
        s = mb.Season(y)
        gid_of = {pid: [x[1] for x in g] for pid, g in s.games.items()}
        for r in rs:
            pid, day = r["pid"], r["day"]
            g = s.games[pid]
            i = bisect.bisect_left(s.dates[pid], day)
            team = g[i - 1][2]
            now = s.out_now(team, day)
            if not now:
                continue
            future = g[i:]
            best = None
            for t in now[1]:
                j = bisect.bisect_left(s.dates[t], day)
                if j >= len(s.games[t]) or s.games[t][j][2] != team:
                    continue                      # never back for this team this season
                back = s.games[t][j][0]
                with_ = [g[k][3] for k in range(i) if t in s.played[g[k][1]]][-WITH_LAST:]
                if len(with_) < WITH_MIN:
                    continue
                role = s.role(t, day)
                if best is None or role > best[0]:
                    best = (role, back, sum(with_) / len(with_))
            if not best:
                continue
            _, back, with_m = best
            week = [x for x in future if x[0] < day + timedelta(days=mb.WEEK)]
            r["ret"] = {"with": with_m, "back": back,
                        "f_week": sum(x[0] >= back for x in week) / len(week) if week else 0.0,
                        "f_ros": sum(x[0] >= back for x in future) / len(future)}
            touched += 1
    return touched


def model(r, w, target):
    mins, rate = shipped(r)
    ret = r.get("ret")
    if ret and w:
        f = ret["f_week"] if target == "week" else ret["f_ros"]
        mins = (1 - f) * mins + f * (w * ret["with"] + (1 - w) * mins)
    return mins * rate


def main():
    rows = cs.load()
    print("%d rows; augmenting the ones with a teammate out now..." % len(rows), file=sys.stderr)
    n = augment(rows)
    print("%d rows (%.1f%%) have a teammate out now who returns this season and played with him %d+ games"
          % (n, 100 * n / len(rows), WITH_MIN))
    for target in ("week", "ros"):
        key = "f_week" if target == "week" else "f_ros"
        print("\n%s (w fitted on the other three seasons)" % target)
        print("  season  w     all rows                 rows he returns in the window   covering & returning")
        for y in SEASONS:
            train = [r for r in rows if r["y"] != y and r.get("ret") and r["ret"][key] > 0]
            w = min(W_GRID, key=lambda w: mb.score(train, lambda r: model(r, w, target), target))
            te = [r for r in rows if r["y"] == y]
            hit = [r for r in te if r.get("ret") and r["ret"][key] > 0]
            cov = [r for r in hit if shipped(r)[0] - r["ret"]["with"] >= 3]   # playing 3+ more than with him
            cells = []
            for sub in (te, hit, cov):
                a, b = mb.score(sub, lambda r: model(r, 0.0, target), target), mb.score(sub, lambda r: model(r, w, target), target)
                cells.append("%.3f -> %.3f (%+.3f) n%d" % (a, b, b - a, len(sub)))
            print("  %d  %.2f  %s" % (y, w, "   ".join(cells)))
        for w in W_GRID[1:]:
            print("  fixed w %.2f: all rows %s" % (w, " ".join("%+.3f" % (
                mb.score(te, lambda r: model(r, w, target), target) - mb.score(te, lambda r: model(r, 0.0, target), target))
                for te in ([r for r in rows if r["y"] == y] for y in SEASONS))))
    mb.picks(rows, {"shipped": lambda r: model(r, 0.0, "week"),
                    **{"w %.2f" % w: (lambda r, w=w: model(r, w, "week")) for w in (0.5, 1.0)}})
    # how much the minutes move on the rows that matter: covering now, teammate back within the week
    rs = [r for r in rows if r.get("ret") and r["ret"]["f_week"] > 0 and r["week_min"] is not None
          and shipped(r)[0] - r["ret"]["with"] >= 3]
    if rs:
        m = lambda f: sum(f(r) for r in rs) / len(rs)
        print("\ncovering (3+ minutes over his minutes with the teammate) and the teammate back within the week: %d rows" % len(rs))
        print("  shipped minutes %.1f, minutes with him %.1f, actual next week %.1f" % (
            m(lambda r: shipped(r)[0]), m(lambda r: r["ret"]["with"]), m(lambda r: r["week_min"])))


if __name__ == "__main__":
    main()
