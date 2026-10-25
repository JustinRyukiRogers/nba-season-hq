"""Does fitting each team's preseason minutes into 240 a game help the minutes model?

ESPN projects every player on his own, so a deep team's projected minutes can add up to far more
than a game holds (Houston 2026-27: 308 a game). Here the preseason minutes of each team are scaled
before the minutes model (research/minutes_backtest.py, settings as shipped) blends in his games:

    demand   sum over the team's projected players of minutes x share of games ESPN expects him
             to play (a typical night's call on the 240 minutes)
    prop     every player's minutes x (240 / demand) ** beta
    rel      every player's minutes x (typical team's demand that season / demand) ** beta: crowding
             against the other teams, which does not depend on how many players ESPN projected
    bench    the excess (demand - 240) x beta comes off players in proportion to how far they are
             below 36 minutes, so starters keep theirs and the bench is squeezed

The team is the one he first played for that season. Players ESPN did not project are not in the
demand (they cannot be scaled, and their minutes are the surprise no budget sees).

Fitted on three seasons, scored on the fourth, each in turn, with the same measures as the main
backtest (next-week and rest-of-season error, the waiver pick test).

Result (2026-10-25): no help, not shipped. For next week every fold, form and season fitted beta 0
(no budget); for rest of season beta 0.25, worse in 2022 and 2025, better in 2024 and 2026, net
~0.00. A direct check agrees: ESPN's preseason minutes run 1-2 high for everyone who played 20+
games, about as much on the least crowded quarter of teams as on the most (only 2024-25 showed the
expected slope). A team's paper crowding is mostly undone by injuries, and a player's own minutes
take over within a few games anyway.
Side finding: next week alone prefers MINUTES_PRIOR_GAMES 2 to the shipped 4 in every fold (picks
+2.7 +0.8 +0.2 +1.5 a week by season); rest of season prefers 4.

    python3 research/minutes_budget.py
"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import minutes_backtest as mb
from draftkit.config import GP
from draftkit.espn import stat_split
from draftkit.history import load_espn

GAME = 240.0
FULL = 36.0
BETAS = [0.0, 0.25, 0.5, 0.75, 1.0]
KMS = [2.0, 4.0, 6.0, 8.0]


def team_budgets(season):
    """-> {pid: (team demand, sum of bench room on the team, his preseason minutes)}."""
    espn = load_espn(season.year)
    team, avail = {}, {}
    for pid, (_, pm, _) in season.prior.items():
        g = season.games.get(pid)
        if not g or not pm:
            continue
        pr = stat_split(espn.get(int(pid)), "10%d" % season.year)
        team[pid] = g[0][2]
        avail[pid] = min(1.0, (pr.get(GP, 0) if pr else 0) / 82.0)
    demand, room = defaultdict(float), defaultdict(float)
    for pid, t in team.items():
        pm = season.prior[pid][1]
        demand[t] += pm * avail[pid]
        room[t] += max(0.0, FULL - pm) * avail[pid]
    ds = sorted(demand.values())
    typical = ds[len(ds) // 2]
    return {pid: (demand[t], room[t], season.prior[pid][1], typical) for pid, t in team.items()}


def scaled_pm(r, form, beta):
    pm = r["pmpg"]
    if not pm or r.get("budget") is None or beta == 0:
        return pm
    demand, room, _, typical = r["budget"]
    if form == "prop":
        return pm * (GAME / demand) ** beta
    if form == "rel":
        return pm * (typical / demand) ** beta
    if room <= 0:
        return pm
    return max(0.0, pm - beta * (demand - GAME) * max(0.0, FULL - pm) / room)


def model(form, beta, km):
    p = dict(mb.SHIPPED, km=km)

    def f(r):
        pm = scaled_pm(r, form, beta)
        if not pm:
            return mb.minutes_model(r, p)[2]
        q = r["pre"]
        recent = q[("m", p["hl"], 1.0)]
        gp = q["gp"]
        mins = max(0.0, min(40.0, pm + gp / (gp + km) * (recent - pm)))
        rate = (q["prate"] * p["kr"] + q["tot_pts"]) / (p["kr"] + q["tot_min"])
        return mins * rate
    return f


def main():
    rows = []
    for y in mb.SEASONS:
        s = mb.Season(y)
        b = team_budgets(s)
        demand = sorted({v[0] for v in b.values()})
        print(y, "team demand per game: %.0f to %.0f, median %.0f" % (demand[0], demand[-1], demand[len(demand) // 2]),
              file=sys.stderr)
        rr = mb.rows_for(s)
        for r in rr:
            r["budget"] = b.get(r["pid"])
            mb.prepare(r)
        rows += rr
    shipped = model("prop", 0.0, mb.SHIPPED["km"])
    out = defaultdict(list)
    for test in mb.SEASONS:
        train = [r for r in rows if r["y"] != test]
        te = [r for r in rows if r["y"] == test]
        for target in ("week", "ros"):
            best = min(((form, beta, km) for form in ("prop", "bench", "rel") for beta in BETAS for km in KMS),
                       key=lambda c: mb.score(train, model(*c), target))
            crowded = [r for r in te if r.get("budget") and r["budget"][0] > 1.08 * r["budget"][3]]
            early = [r for r in te if len(r["past"]) <= 12]
            out[target].append((test, best,
                                [(mb.score(sub, shipped, target), mb.score(sub, model(*best), target))
                                 for sub in (te, early, crowded)], len(crowded)))
    for target, res in out.items():
        print("\n%s: shipped -> with budget (all / first 30 days / crowded teams, 8%% over the season's typical demand)" % target)
        for test, best, sc, nc in res:
            print("  %d  %-22s %s   (%d crowded rows)" % (test, "%s beta %.2f km %g" % best,
                                                         "   ".join("%.2f -> %.2f" % x for x in sc), nc))
    # pick test with each season's week fit
    fits = {test: best for test, best, _, _ in out["week"]}
    print()
    for test, best in fits.items():
        te = [r for r in rows if r["y"] == test]
        print("season %d, %s:" % (test, best))
        mb.picks(te, {"shipped": shipped, "budget": model(*best)})


if __name__ == "__main__":
    main()
