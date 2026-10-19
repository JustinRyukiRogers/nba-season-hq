"""Season simulation: hold the roster vs turn the weakest spot(s) into a streaming spot (2026-10-19).
Same lineup simulation and fixed draws as the dashboard (model.project_days). Pulls from ESPN like a
refresh (needs the cookies in .env), then compares holding the roster all season with streaming one
or two spots day by day, under pessimistic cases: other teams take the best free agents first, and
free agents score under their projection. A few seconds a case.

    python3 research/stream_spot.py
"""
import copy
import sys
import time
from collections import defaultdict
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from seasonkit import model, advice



class Captured(Exception):
    pass


def capture():
    """Run a refresh up to its lineup check and keep what it has valued by then."""
    from seasonkit import build
    got = {}

    def grab(league, players, me, calendar):
        got.update(league=league, players=players, me=me, calendar=calendar)
        raise Captured
    orig, advice.lineup_check = advice.lineup_check, grab
    try:
        build.build(verbose=False, with_news=False)
    except Captured:
        pass
    finally:
        advice.lineup_check = orig
    return got


c = capture()
L, P0, me, cal = c["league"], c["players"], c["me"], c["calendar"]
SL = L["starting"]
days = cal.days_left()
mine = [e["id"] for e in L["teams"][str(me)]["roster"] if e["slot"] != "IR"]
by_value = sorted(mine, key=lambda i: P0[i]["ros"]["value"])
fas_all = sorted((p for p in P0.values() if p["owner"] == 0 and p["fa"] == "FREEAGENT" and not p["ros"]["out"]),
                 key=lambda p: -p["ros"]["value"])

def proj(P, ids, ds, samples=200):
    return model.project_days(ids, ds, P, SL, cal, samples=samples)[0]

def week_points(P, ids_by_day):
    """points per matchup week given the roster on each day"""
    wk = defaultdict(float)
    for d, ids in ids_by_day:
        wk[cal.week_of(d)] += proj(P, ids, [d])
    return wk

def run(n_stream, skip_top, horizon, haircut, threshold=1.0, cooldown=3):
    P = copy.copy(P0)
    pool = [p["id"] for p in fas_all[skip_top:]]
    for pid in pool:                       # free agents' per-game number cut by `haircut`
        q = dict(P[pid]); q["ros"] = dict(q["ros"]); q["ros"]["fppg"] *= (1 - haircut); P[pid] = q
    fixed = [i for i in mine if i not in by_value[:n_stream]]
    slots = [None] * n_stream
    blocked, adds, plan = {}, 0, []
    for i, d in enumerate(days):
        win = days[i:i + horizon]
        cur = fixed + [s for s in slots if s]
        base = proj(P, cur, win, samples=60)
        cands = [pid for pid in pool if pid not in slots and blocked.get(pid, -1) < i
                 and any(str(x) in P[pid]["odds"] for x in win[:2])]
        cands.sort(key=lambda pid: -sum(P[pid]["odds"].get(str(x), 0) for x in win) * P[pid]["ros"]["fppg"])
        best = (threshold, None, None)
        for pid in cands[:25]:
            for k in range(n_stream):
                alt = fixed + [s for j, s in enumerate(slots) if s and j != k] + [pid]
                g = proj(P, alt, win, samples=60) - base
                if g > best[0]:
                    best = (g, pid, k)
        if best[1]:
            old = slots[best[2]]
            if old:
                blocked[old] = i + cooldown       # a dropped player sits on waivers
            slots[best[2]] = best[1]
            adds += 1
        plan.append((d, fixed + [s for s in slots if s]))
    return week_points(P, plan), adds

hold = week_points(P0, [(d, mine) for d in days])
H = sum(hold.values()); W = sum(cal.weight(w) * v for w, v in hold.items())
nweeks = len(hold)
print("hold all season: %.0f points (%.0f a week); dropping for streams: %s" % (H, H / nweeks, [P0[i]["name"] for i in by_value[:2]]))
print("%-44s %8s %8s %8s %6s %s" % ("streaming setup", "season", "per wk", "wtd", "adds", "weeks won by streaming"))
for n, skip, hz, hc in [(1, 0, 3, 0.0), (1, 10, 3, 0.0), (1, 30, 3, 0.10), (1, 10, 7, 0.0),
                        (1, 10, 1, 0.0), (1, 30, 1, 0.10), (1, 50, 1, 0.15), (2, 10, 3, 0.0), (2, 30, 3, 0.10)]:
    t = time.time()
    wk, adds = run(n, skip, hz, hc)
    S = sum(wk.values()); Ws = sum(cal.weight(w) * v for w, v in wk.items())
    won = sum(1 for w in hold if wk[w] > hold[w])
    print("%-44s %+8.0f %+8.1f %+8.0f %6d %d of %d   (%.0fs)" % (
        "%d spot(s), skip top %d FAs, horizon %dd, FA -%d%%" % (n, skip, hz, hc * 100),
        S - H, (S - H) / nweeks, Ws - W, adds, won, nweeks, time.time() - t))
