"""How wrong are ESPN's preseason projections, and where do the misses come from?

Sample: the 150 players ESPN projected for the most fantasy points (default points scoring) in each
season where the stored projection is a true preseason snapshot (see draftkit/history.py for which
seasons qualify and why). Run from the project root:

    python3 research/backtest_espn.py
"""
import math
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from draftkit.config import (AST, BLK, FGA, FGM, FTA, FTM, GP, HISTORY, MIN, PTS, RAW, REB, SEASON, STL, TEAMS, TO,
                             TPA, TPM, read_json)
from draftkit.espn import fantasy_points, stat_split
from draftkit.history import POOL, SEASONS, build_rows, load_bref, load_espn
from draftkit.sources import norm_name
from draftkit.stats import ols, pearson, predict, r_squared, spearman

CURRENT_SEASON = SEASON


# ---------- reports ----------

def report_accuracy(D):
    print("\nA. Accuracy by season  (r = correlation of projection with actual)")
    print(f"   {'season':>6} {'projGP':>7} {'actGP':>6} {'<50 GP':>7} {'r total':>8} {'r per-game':>10} {'r GP':>5} {'actual/proj':>11}")
    for y in SEASONS + ["all"]:
        L = [r for r in D if y in ("all", r["y"])]
        P = [r for r in L if r["agp"] >= 20]
        print(f"   {y:>6} {st.mean(r['pgp'] for r in L):7.1f} {st.mean(r['agp'] for r in L):6.1f}"
              f" {sum(r['agp'] < 50 for r in L) / len(L):7.0%}"
              f" {pearson([r['ptot'] for r in L], [r['atot'] for r in L]):8.2f}"
              f" {pearson([r['pfppg'] for r in P], [r['afppg'] for r in P]):10.2f}"
              f" {pearson([r['pgp'] for r in L], [r['agp'] for r in L]):5.2f}"
              f" {sum(r['atot'] for r in L) / sum(r['ptot'] for r in L):11.0%}")


def report_decomposition(D):
    """actual - projected total  ==  (GP miss x projected rate)  +  (actual GP x rate miss), exactly."""
    games = [(r["agp"] - r["pgp"]) * r["pfppg"] for r in D]
    rate = [r["agp"] * ((r["afppg"] or 0) - r["pfppg"]) for r in D]
    err = [r["atot"] - r["ptot"] for r in D]
    v = st.pvariance(err)
    rmse = lambda e: math.sqrt(st.mean(x * x for x in e))
    print("\nB. Where the season-total miss comes from")
    print(f"   variance of the miss: games played {st.pvariance(games) / v:.0%} | per-game rate {st.pvariance(rate) / v:.0%}"
          f" | interaction {(v - st.pvariance(games) - st.pvariance(rate)) / v:.0%}")
    print(f"   RMSE {rmse(err):.0f} FP -> {rmse(rate):.0f} if games played were known | {rmse(games):.0f} if per-game rate were known")
    P = [r for r in D if r["agp"] >= 20]
    mins = [(r["ampg"] - r["pmpg"]) * r["pfppg"] / r["pmpg"] for r in P]
    per_min = [r["ampg"] * (r["afppg"] / r["ampg"] - r["pfppg"] / r["pmpg"]) for r in P]
    v = st.pvariance([r["afppg"] - r["pfppg"] for r in P])
    print(f"   per-game miss splits into: minutes {st.pvariance(mins) / v:.0%} | per-minute production {st.pvariance(per_min) / v:.0%}"
          f"   (r minutes {pearson([r['pmpg'] for r in P], [r['ampg'] for r in P]):.2f},"
          f" r per-minute {pearson([r['pfppg'] / r['pmpg'] for r in P], [r['afppg'] / r['ampg'] for r in P]):.2f})")


def report_buckets(D):
    print("\nC. By projected rank")
    print(f"   {'rank':>8} {'projGP':>7} {'actGP':>6} {'<50 GP':>7} {'<65 GP':>7} {'actual/proj':>11} {'bust <70%':>9} {'boom >110%':>10}")
    for lo, hi in [(1, 12), (13, 24), (25, 36), (37, 60), (61, 100), (101, 150)]:
        L = [r for r in D if lo <= r["prank"] <= hi]
        share = lambda f: sum(map(f, L)) / len(L)
        print(f"   {f'{lo}-{hi}':>8} {st.mean(r['pgp'] for r in L):7.1f} {st.mean(r['agp'] for r in L):6.1f}"
              f" {share(lambda r: r['agp'] < 50):7.0%} {share(lambda r: r['agp'] < 65):7.0%}"
              f" {sum(r['atot'] for r in L) / sum(r['ptot'] for r in L):11.0%}"
              f" {share(lambda r: r['atot'] < 0.7 * r['ptot']):9.0%} {share(lambda r: r['atot'] > 1.1 * r['ptot']):10.0%}")
    gp = sorted(r["agp"] for r in D if r["prank"] <= 60)
    q = lambda p: gp[int(p * (len(gp) - 1))]
    print(f"   top-60 actual GP quantiles: 10%={q(.1):.0f} 25%={q(.25):.0f} 50%={q(.5):.0f} 75%={q(.75):.0f} 90%={q(.9):.0f}")


def report_games(D):
    V = [r for r in D if r["vgp"] is not None]
    y = [r["agp"] for r in V]
    print("\nD. Can games played be predicted?")
    for label, f in [("ESPN projected GP", lambda r: [1, r["pgp"]]), ("last year's GP", lambda r: [1, r["vgp"]]),
                     ("both", lambda r: [1, r["pgp"], r["vgp"]])]:
        X = [f(r) for r in V]
        c = ols(X, y)
        print(f"   R2 {r_squared([predict(c, x) for x in X], y):.3f}  from {label}")
    print(f"   {'last yr GP':>10} {'n':>4} {'projGP':>7} {'actGP':>6} {'<50 GP':>7}")
    for lo, hi in [(0, 49), (50, 65), (66, 74), (75, 82)]:
        L = [r for r in V if lo <= r["vgp"] <= hi]
        print(f"   {f'{lo}-{hi}':>10} {len(L):>4} {st.mean(r['pgp'] for r in L):7.1f} {st.mean(r['agp'] for r in L):6.1f}"
              f" {sum(r['agp'] < 50 for r in L) / len(L):7.0%}")


def report_segments(D):
    def line(name, L):
        L = [r for r in L if r["agp"] >= 20]
        if len(L) < 8:
            return
        err = [r["afppg"] - r["pfppg"] for r in L]
        ci = 1.96 * st.pstdev(err) / math.sqrt(len(L))
        print(f"   {name:36} {len(L):>4} {st.mean(err):+6.2f} ±{ci:.2f} {st.mean(abs(e) for e in err):6.2f}"
              f" {st.mean(r['ampg'] - r['pmpg'] for r in L):+6.1f}")

    aged = [r for r in D if r["age"] is not None]
    jump = lambda r: r["pfppg"] / r["vfppg"] if r["vfppg"] else None
    print("\nE. Per-game miss (actual - projected fantasy points per game) by player type, 20+ GP")
    print(f"   {'':36} {'n':>4} {'bias':>12} {'MAE':>6} {'MPG':>6}")
    line("everyone", D)
    line("changed teams in the offseason", [r for r in D if r["changed"] is True])
    line("same team", [r for r in D if r["changed"] is False])
    line("played <50 games the year before", [r for r in D if r["vgp"] is not None and r["vgp"] < 50])
    for lo, hi in [(18, 21), (22, 24), (25, 27), (28, 30), (31, 33), (34, 45)]:
        line(f"age {lo}-{hi}", [r for r in aged if lo <= r["age"] <= hi])
    line("rookies", [r for r in D if r["rookie"]])
    line("2nd year", [r for r in D if r["yr2"]])
    line("3rd year", [r for r in D if r["yr3"]])
    line("ESPN projects 10%+ jump on last yr", [r for r in D if jump(r) and jump(r) >= 1.10])
    line("ESPN projects within 10% of last yr", [r for r in D if jump(r) and 0.90 < jump(r) < 1.10])
    line("traded mid-season (not knowable)", [r for r in D if r["traded_midseason"]])

    print("   changed-team minus same-team bias, season by season: ", end="")
    for y in SEASONS:
        gap = [st.mean(r["afppg"] - r["pfppg"] for r in D if r["y"] == y and r["agp"] >= 20 and r["changed"] is flag)
               for flag in (True, False)]
        print(f"{y} {gap[0] - gap[1]:+.1f}", end="  ")
    print()

    P = sorted((r for r in D if r["agp"] >= 30), key=lambda r: r["afppg"] - r["pfppg"])
    fmt = lambda r: f"{r['name']} {r['y']} ({r['afppg'] - r['pfppg']:+.0f} FPPG, {r['pmpg']:.0f}->{r['ampg']:.0f} min)"
    print("   biggest busts: " + "; ".join(map(fmt, P[:8])))
    print("   biggest booms: " + "; ".join(map(fmt, P[:-9:-1])))

    resid = {}
    for r in D:
        if r["agp"] >= 20:
            resid.setdefault(r["name"], {})[r["y"]] = r["afppg"] - r["pfppg"]
    pairs = [(d[a], d[a + 1]) for d in resid.values() for a in d if a + 1 in d]
    print(f"   does ESPN keep missing on the same players? year-to-year r of the miss = "
          f"{pearson([p[0] for p in pairs], [p[1] for p in pairs]):+.2f} (n={len(pairs)})")


def report_stats(D):
    P = [r for r in D if r["agp"] >= 20]
    print("\nF. Per-game accuracy by stat, 20+ GP")
    print(f"   {'stat':5} {'r':>5} {'proj':>7} {'actual':>7}")
    for name, k in [("MIN", MIN), ("PTS", PTS), ("REB", REB), ("AST", AST), ("STL", STL), ("BLK", BLK), ("3PM", TPM), ("TO", TO)]:
        x = [r["pstats"].get(k, 0) / r["pgp"] for r in P]
        y = [r["astats"].get(k, 0) / r["agp"] for r in P]
        print(f"   {name:5} {pearson(x, y):5.2f} {st.mean(x):7.2f} {st.mean(y):7.2f}")
    for name, made, att, floor in [("FG%", FGM, FGA, 4), ("FT%", FTM, FTA, 1.5), ("3P%", TPM, TPA, 1.5)]:
        L = [r for r in P if r["pstats"].get(att, 0) > 0 and r["astats"].get(att, 0) / r["agp"] >= floor]
        x = [r["pstats"][made] / r["pstats"][att] for r in L]
        y = [r["astats"][made] / r["astats"][att] for r in L]
        print(f"   {name:5} {pearson(x, y):5.2f} {st.mean(x):7.3f} {st.mean(y):7.3f}")


def report_calibration(D):
    """Leave-one-season-out: do systematic tilts on top of ESPN beat ESPN alone at ordering players?"""
    V = [r for r in D if r["vfppg"] is not None and r["age"] is not None and r["changed"] is not None and r["agp"] >= 20]
    old, young = (lambda r: float(r["age"] >= 28)), (lambda r: float(r["age"] <= 24))
    # (the kit's own fitted model is draftkit/calibrate.py; this table shows where its accuracy comes from)
    models = [
        ("raw ESPN", None),
        ("+ blend with last year", lambda r: [1, r["pfppg"], r["vfppg"] - r["pfppg"]]),
        ("+ age", lambda r: [1, r["pfppg"], r["vfppg"] - r["pfppg"], old(r), young(r)]),
        ("+ team change, injury return", lambda r: [1, r["pfppg"], r["vfppg"] - r["pfppg"], old(r), young(r),
                                                    float(r["changed"]), float(r["vgp"] < 50)]),
    ]
    print(f"\nG. Out-of-sample per-game accuracy (fit on four seasons, test on the fifth), n={len(V)}")
    print(f"   {'model':30} {'RMSE':>5} {'rank corr':>9} {'R2':>6}")
    for name, f in models:
        pred, corr = {}, []
        for test in SEASONS:
            coef = ols([f(r) for r in V if r["y"] != test], [r["afppg"] for r in V if r["y"] != test]) if f else None
            te = [r for r in V if r["y"] == test]
            for r in te:
                pred[id(r)] = predict(coef, f(r)) if f else r["pfppg"]
            corr.append(spearman([pred[id(r)] for r in te], [r["afppg"] for r in te]))
        p, y = [pred[id(r)] for r in V], [r["afppg"] for r in V]
        print(f"   {name:30} {math.sqrt(st.mean((a - b) ** 2 for a, b in zip(y, p))):5.2f} {st.mean(corr):9.3f} {r_squared(p, y):6.3f}")
    coef = ols([models[-1][1](r) for r in V], [r["afppg"] for r in V])
    print("   full-sample coefficients: " + ", ".join(f"{n} {c:+.2f}" for n, c in zip(
        ["const", "ESPN proj", "(last yr - proj)", "age 28+", "age <=24", "changed team", "<50 GP last yr"], coef)))


def report_replacement():
    print("\nH. Replacement level: actual fantasy points per game of the Nth-best player (41+ GP)")
    for y in SEASONS[-3:]:
        fppg = sorted((fantasy_points(a) / a[GP] for pl in load_espn(y).values()
                       if (a := stat_split(pl, f"00{y}")) and a.get(GP, 0) >= 41), reverse=True)
        print(f"   {y}: " + "  ".join(f"#{n}: {fppg[n - 1]:.1f}" for n in (1, 12, 24, 60, 100, 130, 156, 200) if n <= len(fppg)))


def report_current():
    """Risk flags and team-level sanity check on this season's ESPN projections."""
    bref = load_bref()
    last = bref[str(CURRENT_SEASON - 1)]
    cur = []
    for pl in (e["player"] for e in read_json(RAW / "espn_players.json")["players"]):
        pr, prev = stat_split(pl, f"10{CURRENT_SEASON}"), stat_split(pl, f"00{CURRENT_SEASON - 1}")
        if not pr or pr.get(GP, 0) <= 0:
            continue
        b, team = last.get(norm_name(pl["fullName"])), TEAMS.get(pl["proTeamId"])
        vgp = prev.get(GP, 0) if prev else None
        cur.append({
            "name": pl["fullName"], "team": team, "adp": (pl.get("ownership") or {}).get("averageDraftPosition") or 999,
            "min": pr.get(MIN, 0), "pts": pr.get(PTS, 0), "pgp": pr[GP], "pfppg": fantasy_points(pr) / pr[GP],
            "vgp": vgp, "vfppg": fantasy_points(prev) / vgp if vgp else None, "age": b["age"] + 1 if b else None,
            "moved": bool(b) and team not in {t["team"] for t in b["teams"]}, "rookie": b is None and vgp is None,
        })
    top = sorted(cur, key=lambda r: r["adp"])[:120]
    n = lambda f: sum(1 for r in top if f(r))
    print(f"\nI. This season ({CURRENT_SEASON - 1}-{str(CURRENT_SEASON)[2:]}): the top 120 by ESPN ADP")
    print(f"   changed teams this offseason {n(lambda r: r['moved'])} | rookies {n(lambda r: r['rookie'])}"
          f" | <50 GP last year {n(lambda r: r['vgp'] is not None and r['vgp'] < 50)}"
          f" | age 28+ {n(lambda r: (r['age'] or 0) >= 28)}"
          f" | ESPN projects 10%+ per-game jump {n(lambda r: r['vfppg'] and r['pfppg'] >= 1.1 * r['vfppg'])}"
          f" | mean projected GP {st.mean(r['pgp'] for r in top):.1f}")

    # A team only has ~241 minutes and ~115 points a game to hand out. What does its real top nine get?
    actual = []
    for y in (CURRENT_SEASON - 3, CURRENT_SEASON - 2, CURRENT_SEASON - 1):
        teams = {}
        for rec in bref[str(y)].values():
            for t in rec["teams"]:
                teams.setdefault(t["team"], []).append((t["mp"] or 0, t["pts"] or 0))
        actual += [(sum(m for m, _ in sorted(L, reverse=True)[:9]) / 82, sum(p for _, p in sorted(L, reverse=True)[:9]) / 82)
                   for L in teams.values()]
    proj = {}
    for r in cur:
        proj.setdefault(r["team"], []).append((r["min"], r["pts"]))
    proj = {t: (sum(m for m, _ in sorted(L, reverse=True)[:9]) / 82, sum(p for _, p in sorted(L, reverse=True)[:9]) / 82)
            for t, L in proj.items() if t}
    print(f"   real teams' top-9 players, last 3 seasons: {st.mean(a[0] for a in actual):.0f} min and {st.mean(a[1] for a in actual):.0f} pts"
          f" per team game (highest: {max(a[0] for a in actual):.0f} min, {max(a[1] for a in actual):.0f} pts)")
    print(f"   ESPN's projected top-9, this season:      {st.mean(p[0] for p in proj.values()):.0f} min and {st.mean(p[1] for p in proj.values()):.0f} pts")
    print("   most over-booked on points: " + ", ".join(f"{t} {p[1]:.0f}" for t, p in sorted(proj.items(), key=lambda kv: -kv[1][1])[:8]))
    print("   most over-booked on minutes: " + ", ".join(f"{t} {p[0]:.0f}" for t, p in sorted(proj.items(), key=lambda kv: -kv[1][0])[:8]))


if __name__ == "__main__":
    rows = build_rows()
    print(f"{len(rows)} player-seasons: top {POOL} by ESPN projected fantasy points, seasons {SEASONS}")
    report_accuracy(rows)
    report_decomposition(rows)
    report_buckets(rows)
    report_games(rows)
    report_segments(rows)
    report_stats(rows)
    report_calibration(rows)
    report_replacement()
    report_current()
