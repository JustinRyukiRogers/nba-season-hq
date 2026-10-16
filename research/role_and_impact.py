"""Do role and impact metrics tell us anything ESPN's projection and our model do not?

Two ideas, checked on the same five seasons as the main backtest:
  role    how settled a player's minutes are (did he start last year; is ESPN projecting more
          minutes than he has ever been given)
  impact  box plus-minus (BPM) and its cousins from Basketball Reference: how much a player helps
          his team win, which is not what fantasy points measure, but may be what earns minutes

    python3 research/role_and_impact.py
"""
import math
import re
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from draftkit import calibrate
from draftkit.config import HISTORY
from draftkit.history import SEASONS, build_rows
from draftkit.sources import ROW, norm_name, number, text
from draftkit.stats import ols, pearson, predict, rmse, spearman


def load_advanced():
    """{season: {name key: {bpm, vorp, ws48, usg, mp, g, gs}}} from the cached advanced pages."""
    cell = re.compile(r'<t[dh][^>]*data-stat="([^"]+)"[^>]*>(.*?)</t[dh]>', re.S)
    out = {}
    for path in sorted((HISTORY / "raw_bref").glob("NBA_*_advanced.html")):
        year = int(re.search(r"NBA_(\d{4})_", path.name).group(1))
        table = re.search(r'<table[^>]*id="advanced"[^>]*>(.*?)</table>', path.read_bytes().decode("utf-8", "ignore"), re.S)
        season = {}
        for row in ROW.findall(table.group(1) if table else ""):
            c = {k: text(v) for k, v in cell.findall(row)}
            name = c.get("name_display")
            if not name or name == "Player":
                continue
            # a traded player's first row is his season total, which is the one to keep
            season.setdefault(norm_name(name), {
                "bpm": number(c.get("bpm", "")), "vorp": number(c.get("vorp", "")), "ws48": number(c.get("ws_per_48", "")),
                "usg": number(c.get("usg_pct", "")), "mp": number(c.get("mp", "")),
                "g": number(c.get("games", "")), "gs": number(c.get("games_started", ""))})
        out[year] = season
    return out


def main():
    adv = load_advanced()
    rows = build_rows(pool_size=200)
    played = [r for r in rows if r["agp"] >= 20]
    # the kit's own per-game model, predicting each season from the other four
    for test in SEASONS:
        coef = calibrate._fit_coef([r for r in played if r["y"] != test])
        for r in played:
            if r["y"] == test:
                r["model"] = predict(coef, calibrate._row_features(r))
    for r in played:
        a = adv.get(r["y"] - 1, {}).get(norm_name(r["name"]))
        ok = a and a["mp"] and a["mp"] >= 500 and a["bpm"] is not None
        r["bpm"] = a["bpm"] if ok else None
        r["starter"] = (a["gs"] or 0) / a["g"] if ok and a["g"] else None
        r["min_miss"] = r["ampg"] - r["pmpg"]
        r["resid"] = r["afppg"] - r["model"]
        r["min_jump"] = r["pmpg"] - r["vmpg"] if r["vmpg"] else None

    ci = lambda v: 1.96 * st.pstdev(v) / math.sqrt(len(v))

    print("A. How settled are minutes, by where ESPN ranks a player?  (minutes miss = actual - projected per game)")
    print("   %-10s %4s %9s %8s %12s %12s %16s" % ("ESPN rank", "n", "avg miss", "spread", "lost 5+ min", "gained 5+", "our per-game sd"))
    for lo, hi in [(1, 50), (51, 100), (101, 150), (151, 200)]:
        L = [r for r in played if lo <= r["prank"] <= hi]
        m = [r["min_miss"] for r in L]
        print("   %-10s %4d %+9.1f %8.1f %11.0f%% %11.0f%% %16.1f" % ("%d-%d" % (lo, hi), len(L), st.mean(m), st.pstdev(m),
              100 * sum(x <= -5 for x in m) / len(m), 100 * sum(x >= 5 for x in m) / len(m), st.pstdev([r["resid"] for r in L])))

    print("\nB. Role last season, for players ESPN ranks 101-200  (resid = actual - our model, per game)")
    late = [r for r in played if r["prank"] > 100]

    def line(label, L):
        if len(L) >= 12:
            print("   %-52s %4d   minutes %+5.1f ±%.1f   resid %+5.2f ±%.2f" % (
                label, len(L), st.mean(r["min_miss"] for r in L), ci([r["min_miss"] for r in L]),
                st.mean(r["resid"] for r in L), ci([r["resid"] for r in L])))

    line("started most of his games last year", [r for r in late if r["starter"] is not None and r["starter"] >= 0.5])
    line("came off the bench last year", [r for r in late if r["starter"] is not None and r["starter"] < 0.5])
    line("ESPN projects 3+ more minutes than last year", [r for r in late if r["min_jump"] is not None and r["min_jump"] >= 3])
    line("ESPN projects about the same minutes", [r for r in late if r["min_jump"] is not None and -3 < r["min_jump"] < 3])
    line("ESPN projects 3+ fewer minutes than last year", [r for r in late if r["min_jump"] is not None and r["min_jump"] <= -3])
    line("bench last year AND projected for 3+ more minutes", [r for r in late if r["starter"] is not None and r["starter"] < 0.5 and (r["min_jump"] or 0) >= 3])

    print("\nC. Impact last season (BPM), same players ranked 101-200")
    B = [r for r in late if r["bpm"] is not None]
    cut = sorted(r["bpm"] for r in B)
    lo3, hi3 = cut[len(cut) // 3], cut[2 * len(cut) // 3]
    line("bottom third by BPM (below %+.1f)" % lo3, [r for r in B if r["bpm"] < lo3])
    line("middle third", [r for r in B if lo3 <= r["bpm"] < hi3])
    line("top third by BPM (above %+.1f)" % hi3, [r for r in B if r["bpm"] >= hi3])
    allb = [r for r in played if r["bpm"] is not None]
    print("   correlation of last year's BPM with the minutes miss: ranks 101-200 %+.2f (n=%d), everyone %+.2f (n=%d)" % (
        pearson([r["bpm"] for r in B], [r["min_miss"] for r in B]), len(B),
        pearson([r["bpm"] for r in allb], [r["min_miss"] for r in allb]), len(allb)))
    print("   correlation of last year's BPM with our model's miss:  ranks 101-200 %+.2f, everyone %+.2f" % (
        pearson([r["bpm"] for r in B], [r["resid"] for r in B]), pearson([r["bpm"] for r in allb], [r["resid"] for r in allb])))

    print("\nD. Would adding them to the model help?  (fit on four seasons, test on the fifth; players with a BPM, n=%d)" % len(allb))
    rookie = calibrate.PER_GAME_TERMS.index("rookie")       # nobody here is a rookie, so that column is empty
    base = lambda r: [x for i, x in enumerate(calibrate._row_features(r)) if i != rookie]
    specs = [("the kit's model", lambda r: base(r)),
             ("+ last year's BPM", lambda r: base(r) + [r["bpm"]]),
             ("+ started most games last year", lambda r: base(r) + [float((r["starter"] or 0) >= 0.5)]),
             ("+ projected minutes change", lambda r: base(r) + [r["min_jump"] or 0.0]),
             ("+ all three", lambda r: base(r) + [r["bpm"], float((r["starter"] or 0) >= 0.5), r["min_jump"] or 0.0])]
    print("   %-34s %6s %10s   %s" % ("", "RMSE", "rank corr", "added coefficients (full sample)"))
    for label, f in specs:
        pred, corr = {}, []
        for test in SEASONS:
            train = [r for r in allb if r["y"] != test]
            coef = ols([f(r) for r in train], [r["afppg"] for r in train])
            held = [r for r in allb if r["y"] == test]
            for r in held:
                pred[id(r)] = predict(coef, f(r))
            corr.append(spearman([pred[id(r)] for r in held], [r["afppg"] for r in held]))
        full = ols([f(r) for r in allb], [r["afppg"] for r in allb])
        print("   %-34s %6.3f %10.4f   %s" % (label, rmse(r["afppg"] - pred[id(r)] for r in allb), st.mean(corr),
                                              " ".join("%+.2f" % c for c in full[len(base(allb[0])):])))


if __name__ == "__main__":
    main()
