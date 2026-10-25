"""Do usage and shooting efficiency make a better points-a-minute projection?

Season HQ projects a game as expected minutes x points a minute, the rate being ESPN's preseason
points a minute blended with this season's as if he had played another 400 minutes at it
(seasonkit/model.py, research/minutes_backtest.py). Usage settles in a few games and shooting takes
months, so splitting the rate might read early games better:

    rate = plays a minute x shooting points a play + other points a minute

    plays     shots, free-throw trips (0.44 an attempt) and turnovers: usage, per minute rather than
              as a share of the team's (within a player that is the same signal, less pace)
    shooting  points, made shots and the misses' penalty under this league's scoring
    other     rebounds, assists, steals, blocks, turnovers

each blended with its preseason number by its own weight (ku, ko minutes; ke plays). With all three
weights alike it is close to the shipped rate.

Replays 2021-22, 2023-24, 2024-25 and 2025-26 from full box-score logs (research/fetch_boxlogs.py).
Every CHECK_EVERY days each player's rate is projected from his games so far and scored against his
points a minute over the next 7 days and over the rest of the season, as points a game at the minutes
he actually played (so the minutes model is held fixed). Weights are fitted on three seasons and
scored on the fourth, each in turn.

Results (2026-10-25), error in points a game, mean of the four held-out seasons (early = first 4 weeks):
    week   shipped 5.517, split 5.504 (early 5.244 -> 5.210); better in all four seasons
    ros    shipped 2.698, base refit k 600 2.680, split 2.678 (worse than base in 2025-26)
The fitted weights say what the theory says: usage settles twice as fast as the rate (ku 100-200
minutes against 400), shooting slower (ke 400 plays, about 800 minutes). But the gain is 0.01-0.03
points a game, a fifth of a percent: not worth a second rate model, so not shipped. Usage and true
shooting are shown on the Stats tab instead, for reading a player, not for the projection.

    python3 research/usage_backtest.py
"""
import bisect
import itertools
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from draftkit.config import FGA, FGM, FTA, FTM, GP, HISTORY, MIN, PTS, TO, TPM
from draftkit.espn import fantasy_points, stat_split
from draftkit.history import load_espn

SEASONS = [2022, 2024, 2025, 2026]
CHECK_EVERY = 3
WEEK = 7
RELEVANT = 15.0
EARLY_DAYS = 28           # "early season": checkpoints in the first four weeks
FT_PLAY = 0.44

BASE_K = [100, 200, 300, 400, 600, 800, 1200]
KU = [25, 50, 100, 200, 400, 800]
KE = [50, 100, 200, 400, 800, 1600]
KO = [100, 200, 400, 800, 1600]


def d(s):
    return date(*map(int, s.split("-")))


def shooting(pts, fgm, fga, tpm, ftm, fta):
    return pts + tpm - fga + 2 * fgm - fta + ftm


def season_rows(year):
    logs = json.loads((HISTORY / ("boxlogs_%d.json" % year)).read_text())
    prior = {}
    for pid, pl in load_espn(year).items():
        pr = stat_split(pl, "10%d" % year)
        if not pr or not pr.get(GP) or not pr.get(MIN):
            continue
        g = lambda k: pr.get(k) or 0
        fp = fantasy_points(pr)
        s = shooting(g(PTS), g(FGM), g(FGA), g(TPM), g(FTM), g(FTA))
        plays = g(FGA) + FT_PLAY * g(FTA) + g(TO)
        if plays <= 0:
            continue
        prior[str(pid)] = {"fppg": fp / g(GP), "rate": fp / g(MIN), "u": plays / g(MIN), "e": s / plays,
                           "o": (fp - s) / g(MIN)}
    games = {}
    for pid, rows in logs.items():
        g = []
        for r in rows:
            if r[3] <= 0:
                continue
            pts, fgm, fga, tpm, tpa, ftm, fta, reb, ast, stl, blk, to = r[5:17]
            s = shooting(pts, fgm, fga, tpm, ftm, fta)
            g.append((d(r[0]), r[3], r[4], s, fga + FT_PLAY * fta + to))
        if g:
            games[pid] = g
    start = min(x[0] for g in games.values() for x in g)
    end = max(x[0] for g in games.values() for x in g)
    out = []
    day = start + timedelta(days=CHECK_EVERY)
    while day < end - timedelta(days=WEEK):
        for pid, g in games.items():
            pr = prior.get(pid)
            if not pr:
                continue
            i = bisect.bisect_left([x[0] for x in g], day)
            past, future = g[:i], g[i:]
            if not past or not future:
                continue
            m, fp, s, pl = (sum(x[k] for x in past) for k in (1, 2, 3, 4))
            if max(pr["fppg"], fp / len(past)) < RELEVANT:
                continue
            week = [x for x in future if x[0] < day + timedelta(days=WEEK)]
            row = {"y": year, "early": (day - start).days <= EARLY_DAYS, "pr": pr, "m": m, "fp": fp, "s": s, "pl": pl}
            for name, part in (("week", week), ("ros", future)):
                fm = sum(x[1] for x in part)
                row[name] = (fm, sum(x[2] for x in part), len(part)) if part and fm > 0 else None
            out.append(row)
        day += timedelta(days=CHECK_EVERY)
    return out


def base(r, k):
    p = r["pr"]
    return (p["rate"] * k + r["fp"]) / (k + r["m"])


def split(r, ku, ke, ko):
    p = r["pr"]
    u = (p["u"] * ku + r["pl"]) / (ku + r["m"])
    e = (p["e"] * ke + r["s"]) / (ke + r["pl"])
    o = (p["o"] * ko + (r["fp"] - r["s"])) / (ko + r["m"])
    return u * e + o


def error(rows, rate, horizon):
    """Mean absolute error in points a game, at the minutes he went on to play."""
    tot = n = 0
    for r in rows:
        h = r[horizon]
        if not h:
            continue
        fm, ffp, ng = h
        tot += abs(rate(r) - ffp / fm) * fm / ng
        n += 1
    return tot / n if n else float("nan")


def fit(rows, horizon):
    b = min(BASE_K, key=lambda k: error(rows, lambda r: base(r, k), horizon))
    s = min(itertools.product(KU, KE, KO), key=lambda w: error(rows, lambda r: split(r, *w), horizon))
    return b, s


def main():
    rows = {y: season_rows(y) for y in SEASONS}
    print("rows:", {y: len(v) for y, v in rows.items()})
    for horizon in ("week", "ros"):
        print("\n%s: error in points a game (held-out season)" % horizon)
        print("  %-6s %8s %8s %8s   %-14s %-16s   early: %8s %8s %8s" % (
            "season", "shipped", "base*", "split*", "base k", "split ku/ke/ko", "shipped", "base*", "split*"))
        tot = [0.0] * 6
        for y in SEASONS:
            train = [r for x in SEASONS if x != y for r in rows[x]]
            k, w = fit(train, horizon)
            test, early = rows[y], [r for r in rows[y] if r["early"]]
            e = [error(test, lambda r: base(r, 400), horizon), error(test, lambda r: base(r, k), horizon),
                 error(test, lambda r: split(r, *w), horizon),
                 error(early, lambda r: base(r, 400), horizon), error(early, lambda r: base(r, k), horizon),
                 error(early, lambda r: split(r, *w), horizon)]
            tot = [a + b / len(SEASONS) for a, b in zip(tot, e)]
            print("  %-6d %8.3f %8.3f %8.3f   %-14s %-16s   early: %8.3f %8.3f %8.3f" % (
                y, *e[:3], k, "/".join(map(str, w)), *e[3:]))
        print("  %-6s %8.3f %8.3f %8.3f   %-14s %-16s   early: %8.3f %8.3f %8.3f" % ("mean", *tot[:3], "", "", *tot[3:]))


if __name__ == "__main__":
    main()
