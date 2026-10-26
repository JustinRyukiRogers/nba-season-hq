"""When a player is covering for a teammate who is still out, should his recent minutes count for more?

The shipped minutes model (seasonkit/model.py, research/minutes_backtest.py) blends a player's recent
minutes with his preseason minutes, his own counting as much as the preseason guess after
MINUTES_PRIOR_GAMES (km) = 4 games. That backtest found that *who he was covering for* did not help in
general: crediting a share of the open minutes, or down-weighting games played in a different lineup.

This tests the narrower case the 2026-10-25 analyst sweep raised (Jaxson Hayes, 26 minutes a game
against a 16-minute projection, every game with Nurkic out and Nurkic still out): a teammate who
matters (SIGNIFICANT minutes) is out now, and the player's last games were played with exactly the
same teammates out ("covering"). Then the run of games in the current lineup is his role for as long
as it lasts, so the blend may lean on it harder:

    shipped   km 4 on his recent minutes, every row
    km_out    km_out instead of km when covering (grid below)
    run       when covering, the recent minutes are the mean of the current same-lineup run only,
              blended by its length with km_out

Rows and scoring come from research/minutes_backtest.py (its saved rows: run that first). Fitted on
three seasons, scored on the fourth, each in turn; error is RMSE in points a game, weighted by games.
Also a pick test: at each checkpoint, the 3 best by each model from a waiver-level pool, scored on
their next week.

A follow-up asks whether it is the teammate at all, or just the jump in minutes: "bump" uses
km_up when his recent minutes run BUMP or more over the preseason guess (whatever the reason),
km_cov when that player is also covering, and km_down for a drop of BUMP or more.

Results (2026-10-25), RMSE in points a game, mean of four held-out seasons:
    covering only (km_out)  week 8.536 -> 8.534 (covering rows 8.347 -> 8.333, km_out 2); ros no change
    run of the current lineup only: worse (week 8.541, ros 5.746)
    bump (fitted per fold; T 5 in 5 of 8 folds, else 3 or 8): week better in all four (-0.010 to -0.028), ros
        better in three (2022 +0.003). Fixed bump 5, km_up 2, km_cov 2, km_down 4: week -0.004 to -0.018
        in all four, ros +0.002, -0.013, -0.012, -0.004. A faster weight for drops helps the week but
        costs ros in all four (+0.03 to +0.04), so drops keep km 4.
    picks (3 a checkpoint from a waiver-level pool, points a week each): shipped 96.6, covering only
        97.2, bump 98.0 (+2.7 +0.0 +0.4 +2.6 by season), bump with km_cov 1 98.1.
So the teammate is not the signal; the jump in minutes is, whatever causes it, and a covering player
gets no reliable extra. Shipped in seasonkit/model.py: MINUTES_BUMP 5, MINUTES_BUMP_PRIOR_GAMES 2.

    python3 research/teammate_out_minutes.py
"""
import itertools
import json
import random
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from draftkit.config import HISTORY
from research import minutes_backtest as mb

SEASONS = mb.SEASONS
RUN_MIN = 2                 # games in the current lineup before he counts as covering
KM_OUT = [0.25, 0.5, 1.0, 2.0, 4.0]
SHIP = mb.SHIPPED


def load():
    rows = json.loads((HISTORY / "minutes_backtest_rows.json").read_text())
    for r in rows:
        r["day"] = date(*map(int, r["day"].split("-")))
        run = 0
        for c in reversed(r["ctx"]):
            if c:
                break
            run += 1
        r["run"] = run
        r["covering"] = r["now_who"] >= 1 and run >= RUN_MIN
    return rows


def model(r, km_out, mode):
    """Points a game: the shipped minutes model, with km_out (and the run's minutes) when covering."""
    if not r["covering"] or mode == "shipped":
        return mb.minutes_model(r, SHIP)[2]
    q = r["pre"]
    if mode == "km_out":
        recent, gp = q[("m", SHIP["hl"], SHIP["rho"])], q["gp"]
    else:                                          # run: only the games in the current lineup
        tail = r["past"][-r["run"]:]
        recent, gp = sum(m for m, _ in tail) / len(tail), len(tail)
    pm = r["pmpg"] if r["pmpg"] else recent
    mins = max(0.0, min(40.0, pm + gp / (gp + km_out) * (recent - pm)))
    rate = (q["prate"] * SHIP["kr"] + q["tot_pts"]) / (SHIP["kr"] + q["tot_min"])
    return mins * rate


def bump_model(r, T, km_up, km_cov, km_down):
    dev = r["pre"][("m", SHIP["hl"], SHIP["rho"])] - r["pmpg"] if r["pmpg"] else 0.0
    km = (km_cov if r["covering"] else km_up) if dev >= T else km_down if dev <= -T else SHIP["km"]
    return mb.minutes_model(r, dict(SHIP, km=km))[2]


BUMP_GRID = list(itertools.product([3.0, 5.0, 8.0], [1.0, 2.0, 4.0], [0.5, 1.0, 2.0, 4.0], [1.0, 2.0, 4.0]))


def bump_test(rows):
    base = lambda r: bump_model(r, 5.0, 4.0, 4.0, 4.0)
    for target in ("week", "ros"):
        print("\nbump, %s (fitted on three seasons)" % target)
        for y in SEASONS:
            train = [r for r in rows if r["y"] != y and r["pmpg"]]
            train = random.Random(1).sample(train, min(len(train), 12000))
            g = min(BUMP_GRID, key=lambda g: mb.score(train, lambda r: bump_model(r, *g), target))
            te = [r for r in rows if r["y"] == y]
            print("  %d  T/km_up/km_cov/km_down %s  %.3f -> %.3f" % (
                y, g, mb.score(te, base, target), mb.score(te, lambda r: bump_model(r, *g), target)))
        for g in [(5.0, 2.0, 2.0, 4.0), (5.0, 2.0, 1.0, 4.0), (5.0, 2.0, 2.0, 2.0)]:
            print("  fixed %s: %s" % (g, " ".join("%+.3f" % (
                mb.score(te, lambda r: bump_model(r, *g), target) - mb.score(te, base, target))
                for te in ([r for r in rows if r["y"] == y] for y in SEASONS))))
    mb.picks(rows, {"shipped": base, "bump": lambda r: bump_model(r, 5.0, 2.0, 2.0, 4.0),
                    "bump+cov": lambda r: bump_model(r, 5.0, 2.0, 1.0, 4.0)})


def main():
    rows = load()
    for r in rows:
        mb.prepare(r)
    cov = sum(r["covering"] for r in rows)
    print("%d rows, %d covering (%.1f%%)" % (len(rows), cov, 100 * cov / len(rows)))
    for target in ("week", "ros"):
        res = defaultdict(list)
        for test in SEASONS:
            train = [r for r in rows if r["y"] != test and r["covering"]]
            te = [r for r in rows if r["y"] == test]
            tc = [r for r in te if r["covering"]]
            fits = {m: min(KM_OUT, key=lambda k: mb.score(train, lambda r: model(r, k, m), target)) for m in ("km_out", "run")}
            for label, sub in (("all", te), ("covering", tc)):
                res[label].append((test, len(sub), mb.score(sub, lambda r: model(r, 4.0, "shipped"), target),
                                   *(mb.score(sub, lambda r: model(r, fits[m], m), target) for m in ("km_out", "run")),
                                   fits["km_out"], fits["run"]))
        for label, rs in res.items():
            print("\n%s, %s rows" % (target, label))
            print("  season    rows  shipped   km_out     run   (km_out fit, run fit)")
            for test, n, a, b, c, kb, kc in rs:
                print("  %d  %6d   %6.3f   %6.3f  %6.3f   (%g, %g)" % (test, n, a, b, c, kb, kc))
            print("  mean            %6.3f   %6.3f  %6.3f" % tuple(sum(x[i] for x in rs) / len(rs) for i in (2, 3, 4)))
    for k in (0.5, 1.0, 2.0):
        print("\npicks, km_out %g:" % k)
        mb.picks(rows, {"shipped": lambda r: model(r, 4.0, "shipped"), "km_out": lambda r, k=k: model(r, k, "km_out"),
                        "run": lambda r, k=k: model(r, k, "run")})
    bump_test(rows)


if __name__ == "__main__":
    main()
