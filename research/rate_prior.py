"""Which preseason points-a-minute should the in-season minutes model start from?

Season HQ projects a player as expected minutes x points a minute (seasonkit/model.py, tested in
research/minutes_backtest.py). The rate starts from a preseason rate and moves to his own as his
minutes pile up (RATE_PRIOR_MINUTES 400). The backtest that chose that model started from ESPN's
projected rate (projected points / projected minutes). The HQ, though, starts from the draft
board's per-game number over ESPN's minutes, and the board's per-game is ESPN's after the draft
kit's calibration (draftkit/calibrate.py), which cuts players aged 28+, on a new team, or back
from a short season, and moves him toward his two-year record. Those cuts were fitted to per-game
outcomes; turned into a rate, they also hold down a player whose minutes have since gone up (the
2026-10-27 sweep: Matisse Thybulle at 0.80 a minute against 1.13-1.19 the last two seasons).

    espn      ESPN's projected rate (what the backtest validated)
    board     the calibrated per-game over ESPN's minutes (what the HQ ships)
    record    ESPN's rate moved toward his own last season's rate, by minutes played (a full
              season of minutes at most halfway)

Everything else is the shipped model (minutes with the bump rule, rate weight 400 minutes). The
calibration was fitted on these seasons, so "board" has an in-sample edge. Rows from
research/minutes_backtest.py (run that first); only players the board would have valued (the
top POOL of ESPN's projections each season). RMSE in points a game, weighted by games; and the
pick test (3 best from a waiver-level pool each checkpoint, next week's points).

    python3 research/rate_prior.py
"""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from draftkit import calibrate
from draftkit.config import HISTORY
from draftkit.history import build_rows
from research import cut_short_games as cs
from research import minutes_backtest as mb

POOL = 300
RECORD_MINUTES = 2000.0     # minutes of his own record that count as much as ESPN's rate


def main():
    cal = calibrate.load()
    seasons = mb.SEASONS
    info = {}
    for r in build_rows(pool_size=POOL, seasons=seasons):
        flags = calibrate.player_flags(r["age"], r["vgp"], r["changed"], r["rookie"])
        board = calibrate.apply_per_game(cal, r["pfppg"], r["vfppg"], r["vgp"], r["v2fppg"], r["v2gp"], flags, r["crowd"])
        info[(r["y"], str(r["id"]))] = {"board": board, "vfppg": r["vfppg"], "vgp": r["vgp"], "vmpg": r["vmpg"],
                                        "flags": [k for k, v in flags.items() if v]}
    rows = [r for r in cs.load() if (r["y"], r["pid"]) in info and r["pmpg"] and r["pfppg"]]
    print("%d rows with a board prior" % len(rows))

    def rate_prior(r, kind):
        espn = r["pfppg"] / r["pmpg"]
        i = info[(r["y"], r["pid"])]
        if kind == "espn":
            return espn
        if kind == "board":
            return i["board"] / r["pmpg"]
        if i["vgp"] and i["vmpg"]:
            own_min = i["vgp"] * i["vmpg"]
            w = min(0.5, own_min / (own_min + RECORD_MINUTES))
            return espn + w * (i["vfppg"] / i["vmpg"] - espn)
        return espn

    def model(r, kind):
        save = r["prate"]
        r["prate"] = rate_prior(r, kind)
        out = cs.model(r)
        r["prate"] = save
        return out

    kinds = ("espn", "board", "record")
    flagged = lambda r: bool(info[(r["y"], r["pid"])]["flags"]) and any(
        f in info[(r["y"], r["pid"])]["flags"] for f in ("old", "new_team", "inj_return"))
    for target in ("week", "ros"):
        print("\n%s: RMSE by season (all rows | rows with an old / new team / short-season flag)" % target)
        for y in seasons:
            te = [r for r in rows if r["y"] == y]
            tf = [r for r in te if flagged(r)]
            print("  %d  %s  |  %s" % (y, "  ".join("%s %.3f" % (k, mb.score(te, lambda r: model(r, k), target)) for k in kinds),
                                      "  ".join("%s %.3f" % (k, mb.score(tf, lambda r: model(r, k), target)) for k in kinds)))
    # the case behind the question: flagged, and playing 5+ minutes more than ESPN projected
    up = lambda r: r["pre"]["m"] - r["pmpg"] >= 5
    for r in rows:
        tail = r["past"][-cs.RECENT:]
        ws = [0.5 ** ((len(tail) - 1 - i) / cs.HL) for i in range(len(tail))]
        r["pre"] = {"m": sum(w * m for w, (m, _) in zip(ws, tail)) / sum(ws)}
    for target in ("week", "ros"):
        print("%s, flagged and minutes up 5+: %s" % (target, "  ".join("%d %s" % (y, " ".join(
            "%s %.3f" % (k, mb.score([r for r in rows if r["y"] == y and flagged(r) and up(r)], lambda r: model(r, k), target))
            for k in kinds)) for y in seasons)))
    e = {k: [(model(r, k) - r["week"]) for r in rows if r["week"] is not None and flagged(r) and up(r)] for k in kinds}
    print("  mean error, week: %s (%d rows)" % ("  ".join("%s %+.2f" % (k, sum(v) / len(v)) for k, v in e.items()), len(e["espn"])))
    # bias: does the board rate run low once he plays?
    for k in kinds:
        e = [(model(r, k) - r["week"]) for r in rows if r["week"] is not None and flagged(r)]
        print("mean error, flagged rows, week, %s: %+.2f" % (k, sum(e) / len(e)))
    mb.picks(rows, {k: (lambda r, k=k: model(r, k)) for k in kinds})


if __name__ == "__main__":
    main()
