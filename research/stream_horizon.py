"""How many days a streaming-spot add is judged on (2026-10-29). The HQ judged a stream on the add's
first day only (STREAM_HORIZON = 1), so dropping a player who still has games later in the matchup
week looked free: on Oct 29 Coulibaly for Draymond Green showed +2 for Friday while it cost Green's
Sunday game (-22 over the week). The Oct 19 test (stream_spot.py) found 1 day best, but before the
spot held players (SPOT_VALUE_SHARE, Oct 21), when a streamer was turned over again the next day.

This replays the rest of the season day by day with the HQ's own pick (advice.add_options on the
upside view, one add a day, half the season value change counted) for each horizon, and scores every
day's lineup with the dashboard's simulation. Free agents: everyone available, or the best 30 taken
by others and the rest scoring 10% under projection. A dropped player sits on waivers for 3 days.

    python3 research/stream_horizon.py [weeks]
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
WEEKS = int(sys.argv[1]) if len(sys.argv) > 1 else 8
days = [d for d in cal.days_left() if d > cal.today]
first_week = cal.week_of(days[0])
days = [d for d in days if cal.week_of(d) < first_week + WEEKS]
mine = [e["id"] for e in advice.roster(L, me, include_ir=False) if e["id"] in P0]
fas_all = sorted((p for p in P0.values() if p["owner"] == 0 and p["fa"] == "FREEAGENT" and not p["ros"]["out"]),
                 key=lambda p: -p["ros"]["value"])


def window(i, horizon):
    if horizon in ("week", "week+defer"):       # the rest of this matchup week (at least the add's day)
        w = cal.week_of(days[i])
        return [d for d in days[i:] if cal.week_of(d) == w]
    return days[i:i + horizon]


def run(horizon, skip_top, haircut, cooldown=3):
    P = copy.copy(P0)
    pool = {p["id"] for p in fas_all[skip_top:]}
    for pid in pool:
        q = dict(P[pid]); q["ros"] = dict(q["ros"]); q["ros"]["fppg"] *= (1 - haircut)
        q["ros"]["upside"] *= (1 - haircut); P[pid] = q
    up = advice.upside_view(P)
    held, blocked, adds, plan = list(mine), {}, 0, []
    spot = advice.stream_spots(up, held)
    for i, d in enumerate(days):
        cands = [up[pid] for pid in pool if pid not in held and blocked.get(pid, -1) < i]
        _, rows = advice.add_options(held, window(i, horizon), cands, up, SL, cal, spot)
        # week+defer: a move that loses points this week waits (the same add can be made next week)
        row = next((r for r in rows if r["ok"] and (horizon != "week+defer" or r["gain"] >= 0)), None)
        if row:
            held = [pid for pid in held if pid != row["drop"]] + [row["id"]]
            blocked[row["drop"]] = i + cooldown
            pool.discard(row["id"])
            pool.add(row["drop"])     # back on waivers for `cooldown` days, then a free agent
            if row["spot"]:
                spot = (spot - {row["drop"]}) | {row["id"]}
            adds += 1
        plan.append((d, list(held)))
    wk = defaultdict(float)
    for d, ids in plan:
        wk[cal.week_of(d)] += model.project_days(ids, [d], P, SL, cal, samples=200)[0]
    return wk, adds, held


hold = defaultdict(float)
for d in days:
    hold[cal.week_of(d)] += model.project_days(mine, [d], P0, SL, cal, samples=200)[0]
H = sum(hold.values())
print("%d weeks from %s, hold the roster: %.0f (%.0f a week)" % (len(hold), days[0], H, H / len(hold)))
print("%-38s %8s %8s %6s  %s" % ("case", "vs hold", "per wk", "adds", "weekly points vs hold"))
res = {}
for skip, hc in [(0, 0.0), (30, 0.10)]:
    for hz in [1, "week", "week+defer", 3]:
        t = time.time()
        wk, adds, held = run(hz, skip, hc)
        res[(skip, hz)] = wk
        S = sum(wk.values())
        print("%-38s %+8.0f %+8.1f %6d  %s  (%.0fs)" % (
            "skip top %d, FA -%d%%, horizon %s" % (skip, hc * 100, hz), S - H, (S - H) / len(hold), adds,
            " ".join("%+.0f" % (wk[w] - hold[w]) for w in sorted(hold)), time.time() - t))
    for x, y in [(1, "week"), ("week", "week+defer")]:
        a, b = res[(skip, x)], res[(skip, y)]
        print("   %s vs %s, by week: %s; better in %d of %d, worse in %d" % (
            y, x, " ".join("%+.0f" % (b[w] - a[w]) for w in sorted(hold)), sum(b[w] > a[w] + 1 for w in hold), len(hold),
            sum(b[w] < a[w] - 1 for w in hold)))
