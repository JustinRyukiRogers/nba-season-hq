"""Every add against every drop, before a take overrides the HQ: this week's and the rest of the season's
points from simulated lineups (slot eligibility, who plays each day), on the HQ's numbers, and on the
take's own number for a player when one is given.

    python3 .claude/skills/judgement-call/compare_adds.py "Tari Eason" "Matisse Thybulle=24.5"
    python3 .claude/skills/judgement-call/compare_adds.py "Tari Eason" --drops "Andrew Wiggins" "Neemias Queta"

Adds are names (NAME=points a game to try a take's number beside the HQ's). Drops default to your
five lowest-valued players. Runs a refresh first (about 15 s; writes site/data.json as usual). The
HQ's own top add (from its streams) is always included, so a take is compared against it, not only
against the player dropped.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from seasonkit import advice, build
from seasonkit.model import project_days


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("adds", nargs="+")
    ap.add_argument("--drops", nargs="*")
    a = ap.parse_args()

    cap = {}
    orig = advice.week_plan

    def spy(league, players, me, calendar, *x, **k):
        cap.update(league=league, players=players, me=me, calendar=calendar)
        return orig(league, players, me, calendar, *x, **k)
    advice.week_plan = spy
    data = build.build(verbose=False, with_news=False)
    L, P, me, cal = cap["league"], cap["players"], cap["me"], cap["calendar"]
    by = {p["name"]: p["id"] for p in P.values()}
    ids = [e["id"] for e in advice.roster(L, me, include_ir=False) if e["id"] in P]
    drops = a.drops or [P[i]["name"] for i in sorted(ids, key=lambda i: P[i]["ros"]["value"])[:5]]
    on_ir = [d for d in drops if by[d] not in ids]
    if on_ir:   # an IR player frees an IR slot, not a roster spot: dropping him makes no room for an add
        print("skipped (in IR, frees no roster spot): " + ", ".join(on_ir))
        drops = [d for d in drops if d not in on_ir]
    adds = [x.split("=") for x in a.adds]
    top = next((m["id"] for m in (data.get("plan") or {}).get("moves", []) if m["kind"] == "add"), None)
    if top and P[top]["name"] not in [x[0] for x in adds]:
        adds.append([P[top]["name"]])
    week, rest, S = advice.week_days(cal), cal.days_left(), L["starting"]

    def run(roster):
        return (project_days(roster, week, P, S, cal)[0], project_days(roster, rest, P, S, cal, samples=60)[0])

    base = run(ids)
    print("as is: this week %.0f, rest of season %.0f (days left this week: %s)" % (
        base[0], base[1], ", ".join(d.strftime("%a") for d in week)))
    for add in adds:
        pid = by[add[0]]
        hq = P[pid]["ros"]["fppg"]
        tries = [hq] + [float(x) for x in add[1:]]
        for fp in tries:
            P[pid]["ros"]["fppg"] = fp
            print("\n%s (%s) at %.1f a game%s; games this week: %s" % (
                add[0], "/".join(P[pid]["pos"]), fp, " (HQ)" if fp == hq else " (take)",
                ", ".join(g[0][5:] for g in P[pid]["next"] if g[0] in {str(d) for d in week}) or "none"))
            for drop in drops:
                r = [x for x in ids if x != by[drop]] + [pid]
                w, s = run(r)
                print("   drop %-24s this week %+5.0f   season %+6.0f" % (drop, w - base[0], s - base[1]))
        P[pid]["ros"]["fppg"] = hq


if __name__ == "__main__":
    main()
