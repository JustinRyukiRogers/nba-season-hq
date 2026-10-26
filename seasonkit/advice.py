"""What to do with the roster: IR moves, today's lineup, pickups, stashes and streams, how urgent
each one is, and how your players are doing against their projections.

Everything here reads the normalized league and the valued players from build.py and returns
plain data for the page. Nothing writes to ESPN.
"""
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import combinations

from .model import ET, REPLACEMENT_FA_RANK, best_lineup, et_date, lineup_day, parse_day, project_days

STREAM_DROPS = 3          # drop candidates tried for each stream: your lowest-valued players
STREAM_POOL = 40          # free agents tried as streams, by expected points this week
LINEUP_MIN_GAIN = 0.5     # expected points before a lineup change is worth suggesting
PLAN_MIN_NET = 10.0       # points, net of rest-of-season value given up, before an add for a real player is planned
# The streaming spot: your lowest-valued healthy player's roster spot turned over to a new free agent
# most days. Tested 2026-10-19 on this roster with the lineup simulation over the whole season: one
# spot streamed day by day beat holding Saddiq Bey by +15 to +47 points a week (pessimistic: other
# teams take the best 50 free agents and free agents score 15% under projection), and won every week.
# A second spot was worth nothing in the pessimistic case. Picking the add on the next day alone
# beat looking 2, 3 or 7 days ahead, and the minimum gain barely mattered (1 to 10 points).
# Changed 2026-10-21 at the user's request (they want holdable players, not a new one every day):
# dropping the spot's holder now costs half the rest-of-season value given up (credits half of any
# gained). Before, the spot was free and the plan turned 100-value free agents (Holiday, Tre Jones,
# Carter) over a day apart. Same season test with every free agent available: about 70 adds a season
# instead of 118, at +30 to +47 points a week over holding instead of +55 to +74.
STREAM_SPOTS = 1
STREAM_HORIZON = 1        # days an add is judged on
STREAM_MIN_GAIN = 5.0     # points before a streaming-spot add is worth the move
SPOT_VALUE_SHARE = 0.5    # share of the rest-of-season value change that counts when the spot turns over

# Trade ideas: a quick screen on rest-of-season value, then the best few per team re-scored with
# the lineup simulation for both sides.
TRADE_SCREEN_FLOOR = -50.0   # value change a trade may show and still be tried (consolidation can beat it)
TRADE_SCREEN = 25            # trades per team re-scored
TRADE_SAMPLES = 60           # simulated days per lineup; every trade is scored on the same draws
TRADE_MIN_GAIN = 100.0       # rest-of-season lineup points (playoff weeks 1.5x) before a trade is listed
TRADE_SHOW, TRADE_PER_TEAM, TRADE_PER_PLAYER = 10, 3, 3
TRADE_ACTION_GAIN = 250.0    # a trade idea this good goes on the to-do list
LEAGUE_MOVES_SHOW = 80       # the league's latest moves (trades, claims, pickups) on the Trades tab
LEAGUE_MOVES_JUDGED = 30     # of those, the latest non-trades whose lineup change is simulated
LEAGUE_TRADE_RECENT = 2 * 86400  # a move this fresh whose players are not on their new rosters yet is flagged

# How urgent a to-do is. now: it costs points or blocks you today. soon: it has a deadline, or
# someone may get there first. optional: a small gain, or no one is racing you. fyi: nothing to do.
URGENCY = ("now", "soon", "optional", "fyi")
STASH_SOON_VALUE = 75.0   # a stash worth at least this much is worth beating a deadline for
INTEREST_OWNED = 25.0     # % of ESPN leagues rostering him: managers here will want him too
INTEREST_ADDS = 0.5       # rise in % owned in a day that counts as interest
HOT_ADDS = 2.0            # rise in % owned in a day: he is being added fast and will not last

# Team report flags: enough games, and far enough off the projection, to be more than noise.
HOT_GAMES, HOT_SHARE = 8, 0.15    # points per game 15% off the projection over 8+ games
ROLE_GAMES, ROLE_MINUTES = 3, 4.0  # minutes per game 4+ off over 3+ games (minutes settle fastest)

# Hunch streams: signs of a bigger role than the projections know about. Judgment calls, untested.
HUNCH_GAMES = 3           # his last few games, for minutes
HUNCH_MINUTES = 5.0       # minutes a game above ESPN's projection over those games
HUNCH_MATE_MINUTES = 24.0  # a teammate projected for this many minutes whose absence opens a role
HUNCH_NEWS_DAYS = 4       # an injury report updated this recently is news, not yet in the projections
HUNCH_ADDS = 1.0          # rise in % owned in a day across ESPN: others smell something


def roster(league, team_id, include_ir=True, today=False):
    """A team's roster from tomorrow (moves made after today's lock included), or as it is today."""
    t = league["teams"][str(team_id)]
    return [e for e in (t.get("roster_today", t["roster"]) if today else t["roster"]) if include_ir or e["slot"] != "IR"]


def weakest(players, entries, exclude=()):
    """The droppable player with the lowest rest-of-season value. -> pid or None."""
    pool = [players[e["id"]] for e in entries if e["id"] in players and e["id"] not in exclude
            and players[e["id"]].get("droppable", True)]
    return min(pool, key=lambda p: p["ros"]["value"])["id"] if pool else None


def name(players, pid):
    return players[pid]["name"] if pid in players else "Player %s" % pid


def clock(ts):
    return datetime.fromtimestamp(ts, ET).strftime("%a %b %-d, %-I:%M %p ET")


def plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


# ---------- injured reserve ----------

def ir_returns(league, players, team_id):
    """Players in a team's IR slots who are no longer tagged Out: ESPN blocks the team's adds until
    they are activated."""
    return [e["id"] for e in roster(league, team_id) if e["slot"] == "IR" and e["id"] in players
            and players[e["id"]]["status"] != "OUT"]


def activated(league, players, team_id, ids):
    """The roster `ids` (outside IR) once every player back from IR is activated, each in place of the
    lowest-valued player when no roster spot is open (as the ir_blocked to-do says).
    -> (ids, [{"id", "drop"}], open roster spots left)."""
    ids, acts, room = list(ids), [], open_room(league, ids)
    for pid in ir_returns(league, players, team_id):
        drop = None if room > 0 else weakest(players, [{"id": x} for x in ids])
        room -= drop is None
        acts.append({"id": pid, "drop": drop})
        ids = [x for x in ids if x != drop] + [pid]
    return ids, acts, room


def ir_actions(league, players, me):
    """ESPN only lets a player tagged Out into an IR slot, and a player in IR who is no longer Out
    blocks every add and claim until he is activated."""
    entries = roster(league, me)
    on_ir = [e for e in entries if e["slot"] == "IR"]
    active = [e for e in entries if e["slot"] != "IR"]
    free_ir = league["ir_slots"] - len(on_ir)
    acts = []
    for e in on_ir:
        p = players.get(e["id"])
        if p and p["status"] != "OUT":
            drop = weakest(players, active)
            acts.append({"kind": "ir_blocked", "urgency": "now", "player": p["id"], "drop": drop,
                         "title": "%s is no longer Out, so ESPN blocks your adds" % p["name"],
                         "detail": "Activate him from IR before any pickup or claim. That needs a free roster "
                                   "spot: %s has the lowest value on your roster." % name(players, drop),
                         "why": "Every add and claim is blocked until he is out of IR."})
    outs = [players[e["id"]] for e in active if e["id"] in players and players[e["id"]]["status"] == "OUT"]
    outs.sort(key=lambda p: -p["ros"]["absent"])
    ir_players = [players[e["id"]] for e in on_ir if e["id"] in players and players[e["id"]]["status"] == "OUT"]
    for p in outs:
        back = p["ros"]["back"]
        when = "out for the season" if p["ros"]["season_over"] else ("expected back %s" % back if back else "no return date")
        if free_ir > 0:
            free_ir -= 1
            acts.append({"kind": "to_ir", "urgency": "soon", "player": p["id"],
                         "title": "Move %s to IR" % p["name"],
                         "detail": "He is tagged Out (%s). The move is free and opens a roster spot for a pickup." % when,
                         "why": "Free to do; do it before your next add."})
            continue
        soonest = min(ir_players, key=lambda q: q["ros"]["absent"]) if ir_players else None
        if soonest and p["ros"]["absent"] > soonest["ros"]["absent"] + 1:
            acts.append({"kind": "ir_swap", "urgency": "soon", "player": p["id"], "other": soonest["id"],
                         "title": "IR is full: swap %s in for %s" % (p["name"], soonest["name"]),
                         "detail": "%s is due back sooner (%s), so he costs you fewer games on the bench than "
                                   "%s (%s)." % (soonest["name"], soonest["ros"]["back"], p["name"], when)})
        else:
            acts.append({"kind": "ir_full", "urgency": "fyi", "player": p["id"],
                         "title": "%s is Out but both IR slots are taken" % p["name"],
                         "detail": "He holds a bench spot until he is back (%s). Keep him unless his value "
                                   "(%.0f) is below the best free agent's." % (when, p["ros"]["value"])})
    return acts, league["ir_slots"] - len(on_ir)


# ---------- today's lineup ----------

def lineup_check(league, players, me, calendar):
    """Compare today's lineup on ESPN with the best one, given who plays and who is hurt. Players
    whose game has tipped off are locked where they are."""
    today, now = calendar.today, calendar.now
    if today < calendar.start:
        return {"state": "preseason", "first_day": str(calendar.start)}
    if today > calendar.end:
        return {"state": "over"}
    starting = league["starting"]
    rows, locked_slots, movable = [], [], []
    for e in roster(league, me, include_ir=False, today=True):
        p = players.get(e["id"])
        if not p:
            continue
        g = next((g for g in calendar.by_team.get(p["team"], []) if g["date"] == today), None)
        prob = p["odds"].get(str(today), 0.0) if g else 0.0
        locked = bool(g and g["tip"] <= now)
        pts = prob * p["ros"]["fppg"] if g and not locked else 0.0
        rows.append({"id": p["id"], "slot": e["slot"], "tip": g["tip"] if g else None,
                     "opp": g["opp"] if g else None, "home": g["home"] if g else None, "locked": locked,
                     "p": round(prob, 2), "pts": round(pts, 1), "status": p["status"]})
        if locked:
            if e["slot"] in starting:
                locked_slots.append(e["slot"])
        else:
            movable.append((p["id"], pts, p["slots"]))
    open_slots = list(starting)
    for s in locked_slots:
        open_slots.remove(s)
    owner, best = best_lineup(movable, open_slots)
    chosen = set(owner.values())
    current = sum(r["pts"] for r in rows if not r["locked"] and r["slot"] in starting)
    start = [r["id"] for r in rows if r["id"] in chosen and r["slot"] not in starting]
    sit = [r["id"] for r in rows if not r["locked"] and r["slot"] in starting and r["id"] not in chosen]
    warnings = []
    for r in rows:
        if r["slot"] in starting and r["tip"] and not r["locked"]:
            if r["p"] == 0:
                warnings.append({"player": r["id"], "text": "%s is Out tonight but in your lineup" % name(players, r["id"])})
            elif r["status"] == "DAY_TO_DAY":
                warnings.append({"player": r["id"], "text": "%s is day-to-day: check his status before tip-off"
                                 % name(players, r["id"])})
    gain = best - current
    return {"state": "season", "date": str(today), "rows": rows, "current": round(current, 1),
            "best": round(best, 1), "gain": round(gain, 1),
            "start": start if gain >= LINEUP_MIN_GAIN else [], "sit": sit if gain >= LINEUP_MIN_GAIN else [],
            "warnings": warnings, "starters_possible": len(chosen) + len(locked_slots),
            "with_games": sum(1 for r in rows if r["tip"])}


# ---------- pickups ----------

def upside_view(players):
    """The players as the pickup logic sees them: points a game and value at their upside
    (model.UPSIDE_RISK), so adds, streams and the plan lean to players who could go off."""
    return {pid: dict(p, ros=dict(p["ros"], fppg=p["ros"]["upside"], value=p["ros"]["value_up"]))
            for pid, p in players.items()}


def free_agents(players, instant_only=False):
    return [p for p in players.values() if p["owner"] == 0
            and (p["fa"] == "FREEAGENT" or (not instant_only and p["fa"] == "WAIVERS"))]


def upgrades(league, players, me, top=12):
    """Free agents worth more over the rest of the season than your weakest player outside the
    streaming spot (the spot turns over to a new free agent most days, so a season-long pickup
    has to beat a player you would otherwise keep)."""
    active = roster(league, me, include_ir=False)
    ids = [e["id"] for e in active if e["id"] in players]
    drop = weakest(players, active, exclude=stream_spots(players, ids, open_room(league, ids)))
    if drop is None:
        return {"drop": None, "list": []}
    floor = players[drop]["ros"]["value"]
    fas = sorted((p for p in free_agents(players) if not p["ros"]["out"]),   # Out players: see stash()
                 key=lambda p: -p["ros"]["value"])
    rows = [{"id": p["id"], "gain": round(p["ros"]["value"] - floor, 1)} for p in fas if p["ros"]["value"] > floor]
    # with an open spot the add needs no drop: the open spot is the streaming spot until he takes it,
    # and then `drop` holds it, so he is still measured against `drop`
    return {"drop": drop, "open": open_room(league, ids), "list": rows[:top]}


def stash(league, players, me, free_ir, top=12):
    """Free agents tagged Out (so IR-eligible) ranked by what they add once back. Holding one costs
    nothing while he is Out, but the add itself needs a free roster spot (confirmed on ESPN
    2026-10-19: with a full roster it asked for a drop). The drop is the streaming spot: once the
    stash is in IR the spot is free again and streaming carries on, so it costs a day of streaming."""
    rows = [p for p in free_agents(players) if p["status"] == "OUT" and not p["ros"]["season_over"]
            and p["ros"]["value"] > 0]
    rows.sort(key=lambda p: -p["ros"]["value"])
    active, _, room = activated(league, players, me,        # after activating anyone back from IR
                                [e["id"] for e in roster(league, me, include_ir=False) if e["id"] in players])
    spot = stream_spots(players, active, room)
    drop = None if room > 0 else (min(spot, key=lambda pid: players[pid]["ros"]["value"]) if spot
                                  else weakest(players, [{"id": x} for x in active]))
    healthy = sorted((p for p in free_agents(players) if not p["ros"]["out"]), key=lambda p: -p["ros"]["value"])
    return {"free_ir": free_ir, "drop": drop, "open": room > 0, "drop_is_spot": drop in spot,
            "refill": healthy[0]["id"] if healthy else None,
            "list": [{"id": p["id"], "back": p["ros"]["back"], "games_after": p["ros"]["games"]} for p in rows[:top]]}


# ---------- this week: when you can make a move, what to add, each day's lineup ----------

def my_adds_today(txs, me, calendar):
    return any(t["team"] == me and t["adds"] and et_date(t["at"]) == calendar.today for t in txs)


def week_days(calendar):
    """Days left in this matchup week (opening week before the season starts)."""
    week = calendar.current_week()
    if week is None:
        return []
    return [d for d in calendar.days_left_in_week(week) if d >= calendar.start]


def next_week_days(calendar):
    """The next matchup week's days, or [] after the last one."""
    week = calendar.current_week()
    if week is None or week + 1 >= len(calendar.weeks):
        return []
    lo, hi = calendar.weeks[week + 1]
    return [lo + timedelta(days=i) for i in range((hi - lo).days + 1)]


def move_days(league, calendar, me, txs=()):
    """Days left this week on which an add still counts. An add counts from the next day whose
    first game has not tipped off (conservative: ESPN locks rosters at the day's first game), and
    with one add a day, today is gone once you have used it. When none is left (the week's last day,
    after the lock), the next week's days: an add made now counts from its first day."""
    days = week_days(calendar)
    if days and days[0] == calendar.today:
        first = calendar.first_tip(calendar.today)
        if (first and calendar.now >= first) or (league["acq"].get("per_day") and my_adds_today(txs, me, calendar)):
            days = days[1:]
    return days or next_week_days(calendar)


def open_room(league, ids):
    """Empty roster spots outside IR: starting slots plus bench, less the players holding them."""
    return max(0, len(league["starting"]) + (league.get("slots") or {}).get("BE", 0) - len(ids))


def stream_spots(players, ids, room=0):
    """The players holding your streaming spots: the lowest-valued droppable ones not tagged Out (an
    Out player belongs in IR). An empty roster spot is a streaming spot already, so `room` of them
    leave fewer players to name. Once a streamer holds the spot he is the lowest, so it stays put."""
    pool = [pid for pid in ids if players[pid].get("droppable", True) and not players[pid]["ros"]["out"]]
    return set(sorted(pool, key=lambda pid: players[pid]["ros"]["value"])[:max(0, STREAM_SPOTS - room)])


def add_options(ids, days, pool, players, starting, calendar, free=(), open_spots=0):
    """Every free agent in `pool` tried in place of each of your lowest-valued players and the
    streaming spot (`free`), over `days` of simulated daily lineups. -> (points now, [row for each
    free agent's best drop, best first]). net = points gained minus the rest-of-season value given
    up when the player dropped is worth more. For the streaming spot (or an open roster spot, worth
    nothing) the cost is SPOT_VALUE_SHARE of the value change either way, so a holdable player is not
    turned over for a day's gain, and adding one is credited. With an open roster spot, an add needs
    no drop (drop None)."""
    base, _, _ = project_days(ids, days, players, starting, calendar)

    def horizon_points(p):
        return sum(p["odds"].get(str(d), 0.0) for d in days) * p["ros"]["fppg"]

    cands = sorted(pool, key=lambda p: -horizon_points(p))[:STREAM_POOL]
    drops = sorted((pid for pid in ids if players[pid].get("droppable", True)),
                   key=lambda pid: players[pid]["ros"]["value"])[:STREAM_DROPS]
    drops += [pid for pid in free if pid in ids and pid not in drops]
    if open_spots > 0:
        drops.append(None)
    best = {}
    for c in cands:
        for x in drops:
            alt, _, by = project_days([pid for pid in ids if pid != x] + [c["id"]], days, players, starting, calendar)
            gain = alt - base
            spot = x is None or x in free
            held = players[x]["ros"]["value"] if x is not None else 0.0
            cost = SPOT_VALUE_SHARE * (held - c["ros"]["value"]) if spot else max(0.0, held - c["ros"]["value"])
            row = {"kind": "add", "id": c["id"], "drop": x, "spot": spot, "gain": round(gain, 1), "ros_cost": round(cost, 1),
                   "net": round(gain - cost, 1), "games": sum(1 for d in days if str(d) in c["odds"]),
                   "starts": round(by.get(c["id"], [0.0])[0], 1)}
            row["ok"] = row["net"] >= (STREAM_MIN_GAIN if row["spot"] else PLAN_MIN_NET)
            if c["id"] not in best or row["net"] > best[c["id"]]["net"]:
                best[c["id"]] = row
    return base, sorted(best.values(), key=lambda r: -r["net"])


def streams(league, players, me, calendar, txs=(), top=10):
    """Best adds for the next day an add still counts, judged on that day as the streaming spot is."""
    days = move_days(league, calendar, me, txs)[:STREAM_HORIZON]
    if not days:
        return {"days": [], "list": []}
    ids = [e["id"] for e in roster(league, me, include_ir=False) if e["id"] in players]
    pool = [p for p in free_agents(players, instant_only=True) if not p["ros"]["out"]]
    room = open_room(league, ids)
    spot = stream_spots(players, ids, room)
    base, rows = add_options(ids, days, pool, players, league["starting"], calendar, spot, room)
    return {"days": [str(d) for d in days], "base": round(base, 1), "spot": sorted(spot), "open": room,
            "list": [r for r in rows if r["gain"] > 0][:top]}


def hunches(league, players, me, calendar, stream_list, top=8):
    """Streams on a hunch: free agents playing on the stream day whose role may be bigger than the
    model knows (it reads projections, not minutes or teammates' injuries). Signs: minutes over his
    last few games above ESPN's projection, a teammate at his positions newly ruled out, or adds
    across ESPN. Each is scored as streams are, so you see what the hunch costs against the top pick."""
    if not stream_list["days"]:
        return {"days": [], "list": []}
    day = parse_day(stream_list["days"][0])
    fresh = calendar.now - HUNCH_NEWS_DAYS * 86400
    signs = {}
    for p in free_agents(players, instant_only=True):
        if p["ros"]["out"] or not p["odds"].get(str(day)):
            continue
        why = []
        proj = (p.get("stats") or {}).get("proj")
        games = [g for g in p.get("recent") or [] if g[2]][:HUNCH_GAMES]
        if proj and games:
            mins = sum(g[2] for g in games) / len(games)
            if mins - proj[1] >= HUNCH_MINUTES:
                why.append("%.0f min a game over his last %s, ESPN projects %.0f; %.1f fantasy points a game" % (
                    mins, plural(len(games), "game"), proj[1], sum(g[1] for g in games) / len(games)))
        for q in players.values():
            inj = q.get("inj") or {}
            back = q["ros"]["back"]
            if (q["team"] != p["team"] or q["id"] == p["id"] or not q["ros"]["out"] or (back and parse_day(back) <= day)
                    or not set(q["pos"]) & set(p["pos"])
                    or ((q.get("stats") or {}).get("proj") or [0, 0])[1] < HUNCH_MATE_MINUTES or not _since(inj.get("updated"), fresh)):
                continue
            why.append("%s (%s, out%s) plays his position" % (
                q["name"], (inj.get("type") or "injury").lower(),
                " until ~%s" % parse_day(back).strftime("%b %-d") if back else ""))
        if p["own_chg"] >= HUNCH_ADDS:
            why.append("%+.1f%% owned across ESPN today" % p["own_chg"])
        if why:
            signs[p["id"]] = why
    if not signs:
        return {"days": stream_list["days"], "list": []}
    ids = [e["id"] for e in roster(league, me, include_ir=False) if e["id"] in players]
    room = open_room(league, ids)
    _, rows = add_options(ids, [day], [players[pid] for pid in signs], players, league["starting"], calendar,
                          stream_spots(players, ids, room), room)
    best = stream_list["list"][0]["net"] if stream_list["list"] else 0.0
    for r in rows:
        r["signs"], r["vs_top"] = signs[r["id"]], round(r["net"] - best, 1)
    return {"days": stream_list["days"], "list": rows[:top]}


def _since(stamp, cutoff):
    """Whether an ESPN timestamp ("2026-10-20T23:50Z") is at or after `cutoff` (epoch seconds)."""
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() >= cutoff
    except (AttributeError, ValueError):
        return False


def week_plan(league, players, me, calendar, txs=(), stash=None, pick=None):
    """The rest of this matchup week, day by day: the adds worth making and when (one a day, each
    counting from the day's first tip-off), and the lineup to set each day with the roster you
    will have then. Each day the streaming spot goes to the free agent who adds most to that day's
    lineup (STREAM_HORIZON), counting half the season value changed (SPOT_VALUE_SHARE); a real player is dropped only if the gain beats his rest-of-season
    value. stash: {"id", "drop"} of an IR stash to make first; it takes the first day's add, and
    the spot it frees (when ESPN wants a drop for it) is streamed from the next day. Re-planned on
    every refresh. pick: the players as the adds are chosen on (upside_view); the lineups and points
    use `players`."""
    pick = pick or players
    days = week_days(calendar)
    if not days or calendar.today > calendar.end:
        return {"days": [], "moves": [], "activate": [], "lineups": [], "points": 0.0, "points_now": 0.0}
    starting = league["starting"]
    can_move = set(move_days(league, calendar, me, txs))
    next_week = bool(can_move) and min(can_move) > days[-1]
    if next_week:                 # this week is done for moves: plan the next one
        days = sorted(can_move)
    acq = league["acq"]
    cap = len(days) if acq.get("per_day") or not acq.get("limit") else max(
        0, int(acq["limit"]) - int((league["teams"][str(me)]["adds"] or {}).get(str(calendar.current_week() + 1), 0)))
    ids = [e["id"] for e in roster(league, me, include_ir=False) if e["id"] in players]
    held = list(ids)
    used, moves = set(ids), []
    # a player in IR who is no longer Out blocks every add: he is activated first (no add used)
    held, activate, open_spots = activated(league, players, me, ids)
    for a in activate:
        used |= {a["id"], a["drop"]}
    spot = stream_spots(pick, held, open_spots)
    for i, d in enumerate(days):
        if d not in can_move or sum(m["kind"] == "add" for m in moves) >= cap:
            continue
        if stash:
            moves.append({"kind": "stash", "id": stash["id"], "drop": stash.get("drop"), "spot": False, "gain": 0.0,
                          "ros_cost": 0.0, "net": 0.0, "games": 0, "starts": 0.0, "day": str(d), "by": calendar.first_tip(d)})
            if stash.get("drop") in held:
                held.remove(stash["drop"])
                spot.discard(stash["drop"])
                open_spots += 1
            used |= {stash["id"], stash.get("drop")}
            stash = None
            continue
        pool = [p for p in free_agents(pick, instant_only=True) if not p["ros"]["out"] and p["id"] not in used]
        _, rows = add_options(held, days[i:i + STREAM_HORIZON], pool, pick, starting, calendar, spot, open_spots)
        row = next((r for r in rows if r["ok"]), None)
        if not row:
            continue
        m = dict(row, day=str(d), by=calendar.first_tip(d))
        moves.append(m)
        held = [pid for pid in held if pid != m["drop"]] + [m["id"]]
        used |= {m["id"], m["drop"]}
        if m["drop"] is None:
            open_spots -= 1
        if m["spot"]:
            spot = (spot - {m["drop"]}) | {m["id"]}

    # an activation waits for the day it is needed: his first game or the first planned move, so the
    # player dropped for him keeps any games before then
    for a in activate:
        need = [d for d in days if str(d) in players[a["id"]]["odds"]] + [parse_day(m["day"]) for m in moves]
        a["day"] = str(min(need)) if need else None
        a["by"] = calendar.first_tip(min(need)) if need else None
    lineups, total, rosters = [], 0.0, list(ids)
    locked = {e["id"]: e["slot"] for e in roster(league, me, include_ir=False, today=True)}
    today_ids = [pid for pid in locked if pid in players]
    today_plan = list(today_ids)
    # the same days with the roster as it is now, no adds: what you get if you make no move
    as_is, as_is_total = [], 0.0
    for d in days:
        day = day_lineup(today_ids if d == calendar.today else ids, d, players, starting, calendar,
                         locked if d == calendar.today else {})
        day["moves"] = []
        as_is_total += day["points"]
        as_is.append(day)
    for d in days:
        for a in activate:
            if a["day"] == str(d):
                rosters = [pid for pid in rosters if pid != a["drop"]] + [a["id"]]
                if d == calendar.today:
                    today_plan = [pid for pid in today_plan if pid != a["drop"]] + [a["id"]]
        for m in moves:
            if m["day"] == str(d):   # a stash goes to IR, out of the lineup
                rosters = [pid for pid in rosters if pid != m["drop"]] + ([m["id"]] if m["kind"] == "add" else [])
        day = day_lineup(today_plan if d == calendar.today else rosters, d, players, starting, calendar,
                         locked if d == calendar.today else {})
        day["moves"] = [m for m in moves if m["day"] == str(d)]
        total += day["points"]
        lineups.append(day)
    now_pts, _, _ = project_days(ids, days, players, starting, calendar)
    return {"days": [str(d) for d in days], "next_week": next_week, "moves": moves, "activate": activate,
            "lineups": lineups, "points": round(total, 1),
            "as_is": {"lineups": as_is, "points": round(as_is_total, 1)},
            "points_now": round(now_pts, 1), "move_days": sorted(str(d) for d in can_move),
            "preseason_add": calendar.today < calendar.start and bool(acq.get("per_day")),
            "spot": sorted(stream_spots(pick, ids, open_room(league, ids))), "open": open_room(league, ids)}


def day_lineup(ids, day, players, starting, calendar, slots_now):
    """The lineup to set on one day: the best by expected points among players whose game has not
    tipped off (slots_now: today's ESPN slots, which hold for anyone already locked). The points
    are simulated, allowing for swapping in a bench player when a starter sits."""
    now = calendar.now
    locked, cands, idle = [], [], []
    for pid in ids:
        p = players.get(pid)
        g = calendar.game_on(p["team"], day) if p else None
        if not g:
            idle.append(pid)
        elif g["tip"] <= now:
            if slots_now.get(pid) in starting:
                locked.append((slots_now[pid], pid))
        else:
            cands.append((pid, p["odds"].get(str(day), 0.0), p["ros"]["fppg"], p["slots"], g))
    open_slots = list(starting)
    for s, _ in locked:
        open_slots.remove(s)
    owner, _ = best_lineup([(c[0], c[1] * c[2], c[3]) for c in cands], open_slots)
    pts, starts = lineup_day([c[:4] for c in cands if c[1] > 0], open_slots, str(day))
    info = {c[0]: c for c in cands}
    rows = [[s, pid, None, None, True, None] for s, pid in locked]
    for i, s in enumerate(open_slots):
        pid = owner.get(i)
        c = info.get(pid)
        rows.append([s, pid, round(c[1], 2) if c else None, round(c[1] * c[2], 1) if c else None, False,
                     c[4]["tip"] if c else None])
    rows.sort(key=lambda r: starting.index(r[0]))
    chosen = set(owner.values())
    bench = sorted(([c[0], round(c[1], 2), round(starts.get(c[0], 0.0), 2), c[4]["tip"]] for c in cands if c[0] not in chosen),
                   key=lambda b: -b[1])
    # points lost to the slots: what every player who plays would score if each could start, less the lineup's
    crunch = max(0.0, sum(c[1] * c[2] for c in cands) - pts)
    return {"date": str(day), "first_tip": calendar.first_tip(day), "points": round(pts, 1), "slots": rows,
            "bench": bench, "idle": idle, "games": len(cands) + len(locked), "crunch": round(crunch, 1),
            "starts": round(sum(starts.values()) + len(locked), 1)}


def lineup_outlook(ids, players, starting, calendar):
    """Expected starts, not games, for the players you hold: this week and the rest of the season
    if you keep this roster. -> {pid: {...}}, season points, season points with playoff weeks
    weighted."""
    week = week_days(calendar)
    _, _, wk = project_days(ids, week, players, starting, calendar)
    rest = calendar.days_left()
    pts, _, ros = project_days(ids, rest, players, starting, calendar)
    out = {}
    for pid in ids:
        p = players[pid]
        out[pid] = {"week_games": sum(1 for d in week if str(d) in p["odds"]),
                    "week_starts": round(wk.get(pid, [0, 0])[0], 1), "week_points": round(wk.get(pid, [0, 0])[1], 1),
                    "starts": round(ros.get(pid, [0, 0])[0], 1), "points": round(ros.get(pid, [0, 0])[1])}
    return out, pts


# ---------- trades ----------

def espn_per_game(p):
    proj = p["stats"].get("proj")
    return proj[2] if proj and proj[0] else p["ros"]["fppg"]


def espn_value(p, replacement):
    """Rest-of-season worth as other managers see it: ESPN's per-game projection (what its trade
    screen shows), above a waiver pickup's, over the games he is expected to play. Counting above
    a pickup is what makes two middling players worth less than one star: the second one takes a
    roster spot a pickup could fill."""
    return (espn_per_game(p) - replacement) * p["ros"]["games"]


def after_trade(team, give, get, players, key, refill):
    """A roster after a trade, back to its size: an opened spot goes to the best free agent, and
    one player too many means dropping the lowest by `key` (not one just received)."""
    new = [pid for pid in team if pid not in give] + list(get)
    while len(new) > len(team):
        pool = [pid for pid in new if pid not in get and players[pid].get("droppable", True)] or new
        new.remove(min(pool, key=key))
    if len(new) < len(team) and refill and refill not in new:
        new.append(refill)
    return new


def trade_ideas(league, players, me, calendar):
    """Trades with each other team that add to your simulated daily lineups over the rest of the
    season (playoff weeks 1.5x), and that the other manager should accept on the numbers they see:
    ESPN's projections, counted above a waiver pickup (espn_value). 1-for-1, 2-for-1 and 1-for-2. A quick screen on values picks the
    candidates; the best per team are then re-scored with the lineup simulation for both sides."""
    if calendar.today > calendar.end:
        return {"list": [], "weeks_left": 0}
    days = calendar.days_left()
    starting = league["starting"]
    teams = {int(tid): [e["id"] for e in t["roster"] if e["id"] in players] for tid, t in league["teams"].items()}
    healthy = sorted((p for p in free_agents(players, instant_only=True) if not p["ros"]["out"]),
                     key=lambda p: -p["ros"]["value"])
    refill = healthy[0]["id"] if healthy else None

    def val(pid):
        return players[pid]["ros"]["value"]

    espn_repl = sorted((espn_per_game(p) for p in healthy), reverse=True)
    espn_repl = espn_repl[min(len(espn_repl) - 1, REPLACEMENT_FA_RANK - 1)] if espn_repl else 0.0

    def espn(pid):
        return espn_value(players[pid], espn_repl)

    def sim(ids):
        return project_days(ids, days, players, starting, calendar, samples=TRADE_SAMPLES, weighted=True)[0]

    mine = teams[me]
    base_me = sim(mine)
    my_val = sum(val(x) for x in mine)
    out = []
    for tid, theirs in teams.items():
        if tid == me or not theirs:
            continue
        their_espn = sum(espn(x) for x in theirs)
        shapes = [(g, t) for g in combinations(mine, 1) for t in combinations(theirs, 1)]
        shapes += [(g, t) for g in combinations(mine, 2) for t in combinations(theirs, 1)]
        shapes += [(g, t) for g in combinations(mine, 1) for t in combinations(theirs, 2)]
        screened = []
        for give, get in shapes:
            dv = sum(val(x) for x in after_trade(mine, give, get, players, val, refill)) - my_val
            if dv < TRADE_SCREEN_FLOOR:
                continue
            de = sum(espn(x) for x in after_trade(theirs, get, give, players, espn, refill)) - their_espn
            if de < 0:
                continue
            screened.append((dv, give, get, de))
        screened.sort(key=lambda s: -s[0])
        base_them = sim(theirs) if screened else 0.0
        rows = []
        for dv, give, get, de in screened[:TRADE_SCREEN]:
            new_me = after_trade(mine, give, get, players, val, refill)
            gain = sim(new_me) - base_me
            if gain < TRADE_MIN_GAIN:
                continue
            new_them = after_trade(theirs, get, give, players, val, refill)
            rows.append({"team": tid, "give": list(give), "get": list(get), "gain": round(gain),
                         "their_gain": round(sim(new_them) - base_them), "their_espn": round(de),
                         "my_drop": next((x for x in mine if x not in new_me and x not in give), None),
                         "my_add": next((x for x in new_me if x not in mine and x not in get), None)})
        rows.sort(key=lambda r: -r["gain"])
        out += rows[:TRADE_PER_TEAM]
    out.sort(key=lambda r: -r["gain"])
    shown, uses = [], defaultdict(int)
    for r in out:                     # variety: one player of yours in at most TRADE_PER_PLAYER ideas
        if all(uses[x] < TRADE_PER_PLAYER for x in r["give"]):
            shown.append(r)
            for x in r["give"]:
                uses[x] += 1
    weeks_left = len(calendar.weeks) - (calendar.current_week() or 0)
    deadline = league.get("trade_deadline")
    return {"list": shown[:TRADE_SHOW], "weeks_left": weeks_left, "deadline": deadline // 1000 if deadline else None,
            "refill": refill, "base": round(base_me)}

def league_moves(league, players, me, calendar, feed, top=LEAGUE_MOVES_SHOW, judged=LEAGUE_MOVES_JUDGED):
    """The league's trades, waiver claims and pickups (pull.league_moves), judged on today's numbers: for
    each team in a move, what it got and gave, the change in its simulated daily lineups over the rest
    of the season (playoff weeks 1.5x: its roster now against the same roster with the move undone),
    the change in value on the dashboard's numbers and on ESPN's, and when it plays you next. The lineup
    change is simulated for every trade and the latest `judged` other moves. Each player also says where
    he is now: rosters are read fresh from ESPN on every refresh, so a player shown on his new team is
    in every recommendation; one found elsewhere soon after the move is listed in "unmatched"."""
    days = calendar.days_left()
    starting = league["starting"]
    rosters = {int(tid): [e["id"] for e in t["roster"] if e["id"] in players] for tid, t in league["teams"].items()}
    healthy = sorted((espn_per_game(p) for p in free_agents(players, instant_only=True) if not p["ros"]["out"]), reverse=True)
    espn_repl = healthy[min(len(healthy) - 1, REPLACEMENT_FA_RANK - 1)] if healthy else 0.0
    weeks_left = max(1, len(calendar.weeks) - (calendar.current_week() or 0))
    week_now = calendar.current_week() or 0
    val = lambda ids: sum(players[x]["ros"]["value"] for x in ids)
    espn = lambda ids: sum(espn_value(players[x], espn_repl) for x in ids)
    fill = [p["id"] for p in sorted((p for p in free_agents(players, instant_only=True)
                                     if not p["ros"]["out"] and p["ros"]["games"] > 0),
                                    key=lambda p: -p["ros"]["fppg"])][REPLACEMENT_FA_RANK - 1:]   # waiver level

    def sim(ids):
        return project_days(ids, days, players, starting, calendar, samples=TRADE_SAMPLES, weighted=True)[0]

    def even(a, b):
        """Both rosters at the same size, the short one topped up with waiver-level free agents (the
        replacement rank down): a spot a move opens or fills is worth a waiver pickup, not nothing."""
        a, b = list(a), list(b)
        for short, other in ((a, b), (b, a)):
            extra = [x for x in fill if x not in a and x not in b]
            while len(short) < len(other) and extra:
                short.append(extra.pop(0))
        return a, b

    def meets(tid):
        """The next matchup week (1-based) against you, from this week on."""
        return next((m["period"] for m in sorted(league["matchups"], key=lambda m: m["period"])
                     if m["period"] >= week_now + 1 and {m["home"], m["away"]} == {me, tid}), None)

    out, unmatched, others, seen = [], [], 0, set()
    for t in feed[:top]:
        simulate = bool(days) and (t["kind"] == "trade" or others < judged)
        others += t["kind"] != "trade"
        moves = []
        for pid, frm, to in t["moves"]:
            owner = players[pid]["owner"] if pid in players else None
            if t["kind"] == "trade" and to not in rosters:   # the feed left the receiver out: the trade's other side
                to = next((m[1] for m in t["moves"] if m[1] in rosters and m[1] != frm), owner)
            moves.append({"id": pid, "from": frm or 0, "to": to or 0, "now": owner})
            if (to and owner != to and pid in players and pid not in seen     # his latest move only
                    and t["at"] >= calendar.now - LEAGUE_TRADE_RECENT):
                unmatched.append(pid)
            seen.add(pid)
        sides = []
        for tid in sorted({m["to"] for m in moves} | {m["from"] for m in moves}):
            if tid not in rosters:
                continue
            got = [m["id"] for m in moves if m["to"] == tid and m["id"] in players]
            gave = [m["id"] for m in moves if m["from"] == tid and m["id"] in players]
            now = rosters[tid]
            before = [x for x in now if x not in got] + [x for x in gave if x not in now]
            if simulate:
                a, b = even(now, before)
                gain = sim(a) - sim(b)
            else:
                gain = None
            sides.append({"team": tid, "got": got, "gave": gave,
                          "gain": None if gain is None else round(gain),
                          "per_week": None if gain is None else round(gain / weeks_left),
                          "value": round(val(got) - val(gave)), "espn": round(espn(got) - espn(gave)),
                          "meets_me": meets(tid) if tid != me else None})
        out.append({"at": t["at"], "kind": t["kind"], "moves": moves, "sides": sides})
    return {"list": out, "unmatched": unmatched, "weeks_left": weeks_left}


def drop_order(league, players, me):
    active = [players[e["id"]] for e in roster(league, me, include_ir=False) if e["id"] in players]
    return [p["id"] for p in sorted(active, key=lambda p: p["ros"]["value"])]


# ---------- the matchup ----------

def matchup(league, players, me, calendar, ahead=0):
    """This week's matchup (ahead=0): the score so far plus each side's best daily lineups for the days
    left. ahead=1: next week's, projected over all its days from both rosters as they are now."""
    week = calendar.current_week()
    if week is None:
        return None
    week += ahead
    if week >= len(calendar.weeks):
        return None
    period = week + 1
    m = next((m for m in league["matchups"] if m["period"] == period and me in (m["home"], m["away"])), None)
    if not m:
        return None
    opp = m["away"] if m["home"] == me else m["home"]
    days = ([d for d in calendar.days_left_in_week(week) if d >= calendar.start] if not ahead
            else [calendar.weeks[week][0] + timedelta(days=i) for i in range((calendar.weeks[week][1] - calendar.weeks[week][0]).days + 1)])
    score = {me: m["home_pts"] if m["home"] == me else m["away_pts"],
             opp: m["away_pts"] if m["home"] == me else m["home_pts"]}
    out = {"period": period, "opp": opp, "playoffs": week >= calendar.regular, "ahead": ahead,
           "dates": [str(calendar.weeks[week][0]), str(calendar.weeks[week][1])]}
    for side, tid in (("me", me), ("them", opp)):
        if tid is None:
            continue
        ids = [e["id"] for e in roster(league, tid, include_ir=False)]
        pts, per_day, _ = project_days(ids, days, players, league["starting"], calendar)
        out[side] = {"team": tid, "score": round(score[tid] or 0.0, 1), "remaining": round(pts, 1),
                     "projected": round((score[tid] or 0.0) + pts, 1),
                     "starts": round(sum(n for _, _, n in per_day), 1), "by_day": per_day}
    return out


# Matchup calls. A margin within this share of their projected total is a toss-up. A guess, untested:
# week 1 here was decided by 76 to 212 points on totals of 950 to 1250.
H2H_CLOSE = 0.10
H2H_THREAT_POOL = 8       # free agents tried each day as the opponent's best stream
H2H_MOVES_DAYS = 10       # the opponent's moves shown, days back


def h2h(league, players, me, calendar, match, plan=None, moves=None, txs=()):
    """The head-to-head detail for a matchup from `matchup`. Each side two ways ("views"): its roster as
    it is, and with moves: yours the week plan's (IR activations, stashes, adds), theirs anyone back
    from IR activated plus the best free agent streamed each day an add counts (one a day, in place of
    their lowest-valued player). Each view: the best lineup for every day left (today's ESPN slots hold
    for anyone locked), its points and expected starts, and who carries it. Also the opponent's recent
    moves and adds, their players in doubt, and the calls that follow. Adds `match["h2h"]`."""
    if not match or not match.get("me") or not match.get("them"):
        return match
    starting, opp, ahead = league["starting"], match["opp"], match["ahead"]
    days = [parse_day(d) for d, _, _ in match["me"]["by_day"]]
    first = calendar.first_tip(calendar.today)
    add_days = [d for d in days if d > calendar.today or (d == calendar.today and not (first and calendar.now >= first))]
    pool = [p for p in free_agents(players, instant_only=True) if not p["ros"]["out"]]

    def view(tid, rosters, score, lineups=None):
        """rosters: each day's players (today's: those ESPN has on the team today)."""
        locked = {} if ahead else {e["id"]: e["slot"] for e in roster(league, tid, include_ir=False, today=True)}
        if lineups is None:
            lineups = [day_lineup(r, d, players, starting, calendar, locked if d == calendar.today else {})
                       for d, r in zip(days, rosters)]
        by = defaultdict(lambda: [0.0, 0.0, 0])
        for d, r in zip(days, rosters):
            _, _, bp = project_days(r, [d], players, starting, calendar)
            for pid, (st, pts) in bp.items():
                by[pid][0] += st
                by[pid][1] += pts
                by[pid][2] += 1
        carry = sorted(([pid, round(st, 1), round(pts, 1), g] for pid, (st, pts, g) in by.items()), key=lambda r: -r[2])
        pts = sum(lu["points"] for lu in lineups)
        # a second read: the same expected starts at each player's points a game so far this season
        form = sum(st * (players[pid]["stats"]["season"][2] if (players[pid]["stats"].get("season") or [0])[0]
                         else players[pid]["ros"]["fppg"]) for pid, (st, _, _) in by.items())
        return {"lineups": lineups, "carry": carry, "points": round(pts, 1), "total": round((score or 0.0) + pts, 1),
                "form": round((score or 0.0) + form, 1),
                "starts": round(sum(lu["starts"] for lu in lineups), 1),
                "open": [round(len(starting) - lu["starts"], 1) for lu in lineups]}

    def base(tid):
        ids = [e["id"] for e in roster(league, tid, include_ir=False) if e["id"] in players]
        today_ids = [e["id"] for e in roster(league, tid, include_ir=False, today=True) if e["id"] in players]
        return ids, today_ids

    def swap(ids, drop, add):
        return [x for x in ids if x != drop] + ([add] if add else [])

    views, info = {}, {}
    for side, tid in (("me", me), ("them", opp)):
        ids, today_ids = base(tid)
        as_is = [today_ids if d == calendar.today and not ahead else ids for d in days]
        views[side] = {"as_is": view(tid, as_is, match[side]["score"])}
        doubt = []
        for pid in ids:
            p = players[pid]
            odds = [p["odds"][str(d)] for d in days if str(d) in p["odds"]]
            if odds and (p["status"] != "ACTIVE" or (p.get("inj") or {}).get("status")):   # on the report, not the far-off discount
                doubt.append({"id": pid, "games": len(odds), "odds": round(sum(odds) / len(odds), 2),
                              "at_stake": round((len(odds) - sum(odds)) * p["ros"]["fppg"], 1)})
        doubt.sort(key=lambda r: -r["at_stake"])
        info[side] = {"doubt": doubt, "adds": (league["teams"][str(tid)].get("adds") or {}).get(str(match["period"]), 0),
                      "ids": ids, "today": today_ids}

    # yours with the week plan, when it covers this matchup
    planned = None
    if plan and (plan.get("moves") or plan.get("activate")) and bool(plan.get("next_week")) == bool(ahead) \
            and [lu["date"] for lu in plan["lineups"]] == [str(d) for d in days]:
        ids, today_ids = info["me"]["ids"], info["me"]["today"]
        rosters = []
        for d in days:
            for a in plan.get("activate", []):
                if a["day"] == str(d):
                    ids, today_ids = swap(ids, a["drop"], a["id"]), swap(today_ids, a["drop"], a["id"])
            for m in plan["moves"]:
                if m["day"] == str(d):   # a stash goes to IR, out of the lineup
                    ids = swap(ids, m["drop"], m["id"] if m["kind"] == "add" else None)
            rosters.append(today_ids if d == calendar.today else ids)
        views["me"]["moves"] = view(me, rosters, match["me"]["score"], plan["lineups"])
        views["me"]["moves"]["activate"] = plan.get("activate", [])
        planned = views["me"]["moves"]["total"]
    elif ahead and plan and (plan.get("moves") or plan.get("activate")) and not plan.get("next_week"):
        ids = info["me"]["ids"]       # next week: your roster once this week's planned moves are made
        for a in plan.get("activate", []):
            ids = swap(ids, a["drop"], a["id"])
        for m in plan["moves"]:
            ids = swap(ids, m["drop"], m["id"] if m["kind"] == "add" else None)
        views["me"]["moves"] = view(me, [ids] * len(days), match["me"]["score"])
        views["me"]["moves"]["activate"] = plan.get("activate", [])
        views["me"]["moves"]["carried"] = True
        planned = views["me"]["moves"]["total"]

    # theirs: anyone back from IR activated, then the best free agent each day an add counts, in place
    # of their lowest-valued droppable player, on that day's simulated lineup
    ids, act, _ = activated(league, players, opp, info["them"]["ids"])
    today_ids = info["them"]["today"]
    for a in act:
        today_ids = swap(today_ids, a["drop"], a["id"])
    drop = weakest(players, [{"id": x} for x in ids])
    threat, rosters = [], []
    for d in days:
        here = today_ids if d == calendar.today and not ahead else ids
        if d not in add_days:
            rosters.append(here)
            continue
        cands = lambda r: [(x, players[x]["odds"].get(str(d), 0.0), players[x]["ros"]["fppg"], players[x]["slots"])
                           for x in r if players[x]["odds"].get(str(d), 0.0) > 0 and calendar.game_on(players[x]["team"], d)]
        base_pts, _ = lineup_day(cands(here), starting, str(d))
        best = sorted((p for p in pool if p["odds"].get(str(d))), key=lambda p: -p["odds"][str(d)] * p["ros"]["fppg"])[:H2H_THREAT_POOL]
        top = (None, 0.0)
        for p in best:
            pts, _ = lineup_day(cands(swap(here, drop, p["id"])), starting, str(d))
            if pts - base_pts > top[1]:
                top = (p["id"], pts - base_pts)
        threat.append([str(d), top[0], round(top[1], 1)])
        rosters.append(swap(here, drop, top[0]) if top[0] else here)
    views["them"]["moves"] = view(opp, rosters, match["them"]["score"])
    views["them"]["moves"]["activate"] = act
    views["them"]["moves"]["streams"] = {t[0]: {"id": t[1], "drop": drop, "gain": t[2]} for t in threat if t[1]}

    # their moves: the league feed (judged) and anything newer in the transactions
    since = calendar.now - H2H_MOVES_DAYS * 86400
    feed = []
    for mv in (moves or {}).get("list", []):
        side = next((s for s in mv["sides"] if s["team"] == opp), None)
        if side and mv["at"] >= since:
            feed.append({"at": mv["at"], "kind": mv["kind"], "got": side["got"], "gave": side["gave"],
                         "gain": side["gain"], "per_week": side["per_week"], "value": side["value"]})
    seen = {(f["at"], tuple(f["got"])) for f in feed}
    for t in txs:
        if t["team"] == opp and t["at"] >= since and (t["at"], tuple(t["adds"])) not in seen and (t["adds"] or t["drops"]):
            feed.append({"at": t["at"], "kind": (t["type"] or "").lower(), "got": t["adds"], "gave": t["drops"],
                         "gain": None, "per_week": None, "value": None})
    feed.sort(key=lambda f: -f["at"])

    reach = round(views["them"]["moves"]["total"] - views["them"]["as_is"]["total"], 1)
    h = {"views": views, "doubt": {s: info[s]["doubt"] for s in info}, "adds": {s: info[s]["adds"] for s in info},
         "threat": threat, "reach": reach, "their_moves": feed, "planned": planned}
    h["calls"] = h2h_calls(players, match, h, calendar, views["me"]["as_is"]["total"], views["them"]["as_is"]["total"],
                           planned, plan if planned is not None and not views["me"]["moves"].get("carried") else None)
    match["h2h"] = h
    return match


def h2h_calls(players, match, h, calendar, me_pts, them_pts, planned, plan=None):
    """The calls a matchup suggests, each with an urgency (URGENCY), a deadline when there is one and
    the reason. Judgment on top of the numbers: the toss-up band (H2H_CLOSE) is a guess."""
    calls = []
    margin = (planned if planned is not None else me_pts) - them_pts
    band = H2H_CLOSE * max(them_pts, 1.0)
    reach = h["reach"]
    what = "with your suggested moves" if planned is not None else "as the rosters stand"
    days_word = "next week" if match["ahead"] else "this week"
    if margin > band + reach:
        calls.append({"urgency": "optional", "title": "Comfortable: protect the season, not this week",
                      "why": "You lead by %d %s, more than the toss-up band (%d) plus all they could stream (%d). "
                             "Skip adds that cost rest-of-season value just for %s's games." % (margin, what, band, reach, days_word)})
    elif margin < -(band + reach):
        calls.append({"urgency": "optional", "title": "Long shot: chase upside, not floor",
                      "why": "You trail by %d %s, beyond the toss-up band (%d). Safe streams won't close it; "
                             "upside adds (the hunches) might, and they hold value if they hit." % (-margin, what, band)})
    else:
        calls.append({"urgency": "soon", "title": "Toss-up: every start counts %s" % days_word,
                      "why": "The margin %s is %+d, inside the toss-up band (±%d) once their streaming (up to %d) is counted. "
                             "Make each planned add before its tip-off and recheck injury news before each lock." % (what, margin, band, reach)})
    # the same rosters at this season's points a game so far: a slow start the projections still discount
    mine_v = h["views"]["me"].get("moves") or h["views"]["me"]["as_is"]
    form = mine_v["form"] - h["views"]["them"]["as_is"]["form"]
    if form < -band and calls[0]["title"].split(":")[0] != "Long shot":
        costly = [m for m in (plan or {}).get("moves", []) if m["kind"] == "add" and m.get("ros_cost", 0) > -1]
        calls[0] = {"urgency": "optional", "title": "Underdog on current form: don't spend season value on this week",
                    "why": "The projections call it %+d, but at this season's points a game so far it is %+d (you %d, them %d). "
                           "That is 2-3 games a player, noisy, yet it is how this week has looked. Make the moves that hold value over the season%s." % (
                               margin, form, mine_v["form"], h["views"]["them"]["as_is"]["form"],
                               "; skip %s, which only gains this week" % ", ".join("%s %s" % (parse_day(m["day"]).strftime("%a"), name(players, m["id"])) for m in costly) if costly else "")}
    # a planned add who is also their best stream: free agents go to whoever adds first
    theirs = {t[1]: t[0] for t in h["threat"] if t[1] and t[2] >= 5}
    for m in (plan or {}).get("moves", []):
        if m["kind"] == "add" and m["id"] in theirs:
            calls.append({"urgency": "now" if m["by"] and m["by"] - calendar.now <= 86400 else "soon", "by": m["by"],
                          "title": "Race: %s is their best stream too" % name(players, m["id"]),
                          "why": "Your plan adds him for %s; he is also the most they could gain on %s. A free agent goes to whoever adds first, and taking him also keeps him off their lineup." % (
                              parse_day(m["day"]).strftime("%a"), parse_day(theirs[m["id"]]).strftime("%a"))})
    # thin days: where your lineup has the most empty starts compared with theirs
    mine = h["views"]["me"].get("moves") or h["views"]["me"]["as_is"]
    gaps = [(d["date"], mine["open"][i], h["views"]["them"]["as_is"]["open"][i]) for i, d in enumerate(mine["lineups"])]
    thin = [g for g in gaps if g[1] - g[2] >= 2]
    if thin:
        lab = ", ".join("%s (%.0f empty vs %.0f)" % (parse_day(d).strftime("%a"), a, b) for d, a, b in thin)
        calls.append({"urgency": "fyi", "title": "Your thin days: %s" % lab,
                      "why": "Expected empty starting slots, you vs them. A streamer with a game on those days fills a start they already have; the week plan aims its adds at them."})
    crunch = [(d["date"], d["crunch"]) for d in mine["lineups"] if d["crunch"] >= 10]
    if crunch:
        calls.append({"urgency": "fyi", "title": "Benched points: %s" % ", ".join("%s %.0f" % (parse_day(d).strftime("%a"), c) for d, c in crunch),
                      "why": "More of your players play those days than you have slots. Don't add anyone who only plays then."})
    if reach >= 10 and h["threat"]:
        best = max(h["threat"], key=lambda t: t[2])
        act = h["views"]["them"].get("moves", {}).get("activate") or []
        calls.append({"urgency": "fyi", "title": "Their reach with moves: up to +%d" % reach,
                      "why": "%sIf they add the best free agent every day an add still counts (they have made %d this week). Their best day: %s, %s (+%d)." % (
                          "".join("%s is back from their IR and counted once activated. " % name(players, x["id"]) for x in act),
                          h["adds"]["them"], parse_day(best[0]).strftime("%a"), name(players, best[1]) if best[1] else "nobody", best[2])})
    for r in h["doubt"]["them"][:2]:
        if r["at_stake"] >= 10:
            calls.append({"urgency": "fyi", "title": "Watch %s (theirs): %d game%s at %d%%" % (
                              name(players, r["id"]), r["games"], "" if r["games"] == 1 else "s", round(100 * r["odds"])),
                          "why": "If he sits every game, about %d expected points come off their total; if he plays them all, about %d more go on." % (
                              r["games"] * r["odds"] * players[r["id"]]["ros"]["fppg"], r["at_stake"])})
    return calls


# ---------- league-wide injuries ----------

def injury_board(league, players, me, opp, calendar, beneficiaries=3):
    """Every injured player anyone holds or might add, and for longer absences the free agents on
    his NBA team at his positions, who stand to get his minutes."""
    rows = []
    for p in players.values():
        if p["status"] == "ACTIVE" and not (p.get("inj") and p["inj"].get("status") == "Out"):
            continue
        if p["owner"] == 0 and p["ros"]["value"] <= 0 and (p.get("own") or 0) < 3:
            continue
        row = {"id": p["id"], "who": "mine" if p["owner"] == me else "opp" if p["owner"] == opp
               else "fa" if p["owner"] == 0 else "league"}
        back = p["ros"]["back"]
        if p["ros"]["out"] and back and (calendar.today + timedelta(days=7)) <= parse_day(back):
            pos = set(p["pos"])
            mates = [q for q in free_agents(players) if q["team"] == p["team"] and q["id"] != p["id"]
                     and pos & set(q["pos"]) and not q["ros"]["out"]]
            mates.sort(key=lambda q: -q["ros"]["value"])
            row["beneficiaries"] = [q["id"] for q in mates[:beneficiaries]]
        rows.append(row)
    order = {"mine": 0, "opp": 1, "fa": 2, "league": 3}
    rows.sort(key=lambda r: (order[r["who"]], -players[r["id"]]["ros"]["value"]))
    return rows


# ---------- the action list ----------

def actions(league, players, me, calendar, ir, lineup, ups, stash_list, stream_list, waivers, plan=None, trades=None):
    """The few things worth doing, each with how urgent it is (URGENCY) and, when there is one, the
    deadline ("by", epoch seconds): most urgent first, then the nearest deadline. With a week plan,
    its next add is the stream to-do."""
    acts = list(ir)
    if lineup.get("state") == "season":
        tip = {r["id"]: r["tip"] for r in lineup["rows"] if r["tip"] and not r["locked"]}
        for w in lineup["warnings"]:
            acts.append({"kind": "lineup_warning", "urgency": "now", "by": tip.get(w["player"]),
                         "player": w["player"], "title": w["text"]})
        if lineup["start"]:
            moved = [tip[i] for i in lineup["start"] + lineup["sit"] if i in tip]
            acts.append({"kind": "lineup", "urgency": "now", "by": min(moved) if moved else None,
                         "title": "Fix today's lineup (+%.1f expected points)" % lineup["gain"],
                         "start": lineup["start"], "sit": lineup["sit"],
                         "why": "Each player locks at his game's tip-off."})
    if waivers.get("all_on_waivers"):
        acts.append({"kind": "waivers", "urgency": "fyi",
                     "title": "Every free agent is on waivers until %s" % waivers["clears_label"],
                     "detail": "Claims process then in waiver order; you are #%s of %d." % (waivers["my_rank"], league["size"])})
    if plan is not None:
        if any(x["kind"] == "add" for x in plan["moves"]):
            acts.append(plan_action(league, players, plan, calendar))
    elif stream_list["list"] and stream_list["list"][0].get("ok"):
        acts.append(stream_action(league, players, stream_list, calendar))
    if ups["list"] and ups["list"][0]["gain"] >= 25:
        acts.append(upgrade_action(players, ups, waivers))
    if stash_list["free_ir"] > 0 and stash_list["list"]:
        st = stash_action(league, players, me, stash_list, waivers, calendar)
        planned = next((m for m in (plan or {}).get("moves", []) if m["kind"] == "stash" and m["id"] == st["player"]), None)
        if planned and planned["by"]:
            if not st["by"] or planned["by"] < st["by"]:
                st["by"] = planned["by"]
            st["why"] += (" The week plan gives him %s's add, so make it before that day's first tip-off; "
                          "streaming starts the day after." % parse_day(planned["day"]).strftime("%A"))
        acts.append(st)
    if trades and trades["list"] and trades["list"][0]["gain"] >= TRADE_ACTION_GAIN:
        acts.append(trade_action(league, players, trades))
    order = {u: i for i, u in enumerate(URGENCY)}
    acts.sort(key=lambda a: (order[a["urgency"]], a.get("by") or float("inf")))
    return acts


def _claim(p):
    """The waiver deadline if he can only be claimed, else None (he can be added outright)."""
    return p.get("waiver_until") if p["fa"] == "WAIVERS" else None


def _ahead(waivers):
    return max(0, (waivers.get("my_rank") or 1) - 1)


def stream_action(league, players, stream_list, calendar):
    s = stream_list["list"][0]
    first = calendar.first_tip(parse_day(stream_list["days"][0]))
    why = "Adds lock at the day's first tip-off%s, and the gain shrinks with each day you wait." % (
        " (%s)" % clock(first) if first else "")
    if league["acq"].get("per_day"):
        why += " Uses your one add for the day."
    return {"kind": "stream", "urgency": "now" if first and first - calendar.now <= 86400 else "soon", "by": first,
            "player": s["id"], "drop": s["drop"],
            "title": "Stream %s for %s (+%.0f points this week)" % (name(players, s["id"]), name(players, s["drop"]), s["gain"]),
            "detail": "%s left this week." % plural(s["games"], "game")
                      + (" Costs %.0f of rest-of-season value." % s["ros_cost"] if s["ros_cost"] >= 1 else ""),
            "why": why}


def plan_action(league, players, plan, calendar):
    """The week plan's next add as a to-do; any later ones are listed in its detail."""
    adds = [x for x in plan["moves"] if x["kind"] == "add"]
    m, later = adds[0], adds[1:]
    day = parse_day(m["day"])
    first = m["by"]
    why = "An add counts from the next day's first tip-off%s; after that it counts a day later and gains less." % (
        " (%s)" % clock(first) if first else "")
    if league["acq"].get("per_day"):
        why += " Uses your one add for that day."
    first_up = "".join("First activate %s from IR%s: ESPN blocks the add until then. " % (
        name(players, a["id"]), " (drop %s)" % name(players, a["drop"]) if a["drop"] else "") for a in plan.get("activate", []))
    detail = first_up + "%s for that day's lineup%s." % ("%+.0f points" % m["gain"],
        " (%s expected to start)" % n1(m["starts"]) if m["games"] else "")
    if m["drop"] is None:
        detail += " It fills an open roster spot, so nothing is dropped."
    elif m["spot"]:
        detail += " %s holds your streaming spot." % name(players, m["drop"])
    if m["ros_cost"] >= 1:
        detail += " Costs %.0f of rest-of-season value." % m["ros_cost"]
    elif m["ros_cost"] <= -1:
        detail += " Credited %.0f for being worth more over the season." % -m["ros_cost"]
    if later:
        detail += " Then: " + "; ".join("%s add %s for %s" % (parse_day(x["day"]).strftime("%a"), name(players, x["id"]),
                                                             name(players, x["drop"]) if x["drop"] else "the open spot")
                                        for x in later) + "."
    when = "today" if day == calendar.today else day.strftime("%a %b %-d")
    return {"kind": "stream", "urgency": "now" if first and first - calendar.now <= 86400 else "soon", "by": first,
            "player": m["id"], "drop": m["drop"],
            "title": "Add %s %s, counting from %s (%+.0f that day)" % (
                name(players, m["id"]), "for %s" % name(players, m["drop"]) if m["drop"] else "into the open spot", when, m["gain"]),
            "detail": detail, "why": why}


def trade_action(league, players, trades):
    t = trades["list"][0]
    other = league["teams"][str(t["team"])]
    weeks = max(1, trades["weeks_left"])
    names = lambda ids: " + ".join(name(players, x) for x in ids)
    deadline = (" The trade deadline is %s." % clock(trades["deadline"])) if trades.get("deadline") else ""
    return {"kind": "trade", "urgency": "optional", "by": None, "give": t["give"], "get": t["get"], "title": "Trade idea with %s: %s for %s (+%.0f season points)" % (
                other["abbrev"], names(t["give"]), names(t["get"]), t["gain"]),
            "detail": "About %+.0f points a week for you over the %s left. They come out %+.0f on ESPN's projections, "
                      "the numbers they will look at." % (t["gain"] / weeks, plural(weeks, "week"), t["their_espn"]),
            "why": "No one is racing you for a trade, but every week you wait is a week of the gain gone.%s" % deadline}


def n1(x):
    return "%.1f" % x


def upgrade_action(players, ups, waivers):
    u = ups["list"][0]
    p = players[u["id"]]
    claim, hot = _claim(p), p["own_chg"] >= HOT_ADDS
    why = []
    if hot:
        why.append("He is being added fast across ESPN (%+.1f%% owned today) and will not last." % p["own_chg"])
    if claim:
        why.append("Claims process %s; %s claim before you." % (clock(claim), plural(_ahead(waivers), "team")))
    elif not hot:
        why.append("First come, first served, but no rush elsewhere (%+.1f%% owned today)." % p["own_chg"])
    if ups.get("open"):
        return {"kind": "upgrade", "urgency": "now" if hot else "soon", "by": claim, "player": u["id"], "drop": None,
                "title": "Upgrade: add %s into your open spot (+%.0f upside value)" % (p["name"], u["gain"]),
                "detail": "No drop needed. %s then becomes your streaming spot, so the gain is counted over him." % name(players, ups["drop"]),
                "why": " ".join(why)}
    return {"kind": "upgrade", "urgency": "now" if hot else "soon", "by": claim, "player": u["id"], "drop": ups["drop"],
            "title": "Upgrade: %s over %s (+%.0f upside value)" % (p["name"], name(players, ups["drop"]), u["gain"]),
            "why": " ".join(why)}


def stash_action(league, players, me, stash_list, waivers, calendar):
    """The best stash as a to-do. Holding him costs nothing while he is Out; the add needs a free
    roster spot, so with a full roster the streaming spot is dropped for him (a day of streaming
    lost) or, failing that, your weakest player (his value lost, less the best free agent's).
    Urgency: a claim has a deadline, and a player many ESPN leagues roster will interest this one
    too; filling your last IR slot is never more than optional unless he is worth twice the bar."""
    st = stash_list["list"][0]
    p = players[st["id"]]
    drop, refill, n = stash_list["drop"], stash_list.get("refill"), stash_list["free_ir"]
    value, claim = p["ros"]["value"], _claim(p)
    swap = 0.0
    if drop and not stash_list.get("drop_is_spot"):
        swap = max(0.0, players[drop]["ros"]["value"] - (players[refill]["ros"]["value"] if refill else 0.0))
    net = value - swap
    interest = p["own"] >= INTEREST_OWNED or p["own_chg"] >= INTEREST_ADDS
    bar = STASH_SOON_VALUE * (2 if n == 1 else 1)
    soon = net >= bar and (bool(claim and claim - calendar.now <= 2 * 86400) or interest)

    why = []
    if claim:
        why.append("Claims process %s, and %s claim before you; after that he is first come, first served."
                   % (clock(claim), plural(_ahead(waivers), "team")))
    why.append("%.0f%% owned across ESPN (%+.1f today)." % (p["own"], p["own_chg"]))
    if not soon:
        why.append("A modest gain, so no rush." if net < STASH_SOON_VALUE
                   else "It would fill your last IR slot, so only if you are sure." if n == 1
                   else "Few leagues want him, so he should still be there.")

    mine = [players[e["id"]] for e in roster(league, me) if e["id"] in players and e["id"] != p["id"]]
    like = min(mine, key=lambda q: abs(q["ros"]["value"] - value)) if mine else None
    back = parse_day(st["back"]).strftime("%b %-d") if st["back"] else "?"
    detail = ("Worth about %.0f over a waiver pickup once he is back%s; that already allows for a late return. "
              "He costs nothing while he sits in IR (%s free)." % (
                  value, " (close to %s's value to you)" % like["name"] if like else "", plural(n, "slot")))
    dname = name(players, drop) if drop else None
    if stash_list.get("open"):
        detail += " You have an open roster spot, so the add needs no drop."
    elif stash_list.get("drop_is_spot"):
        detail += (" ESPN needs a free roster spot for the add, so %s goes; he holds your streaming spot, which is "
                   "free again once the stash is in IR, so it costs a day of streaming." % dname)
    elif drop:
        detail += " ESPN needs a free roster spot for the add, so %s goes: a gain of about +%.0f after losing him." % (dname, net)

    steps = []
    if net <= 0:
        steps.append("Not worth it: dropping %s for him costs more than he adds." % dname)
    elif claim:
        steps.append("Claim him before %s%s; the drop only happens if you win the claim." % (
            clock(claim), ", dropping %s" % dname if drop else ""))
    else:
        steps.append("Add him%s." % (", dropping %s" % dname if drop else " into your open roster spot"))
    steps.append("Once he is yours, move him to IR.")
    if league["acq"].get("per_day"):
        steps.append("He uses your one add for that day, so streaming waits a day.")
    if net > 0:
        steps.append("The day after, stream the open spot again (the Week tab has the pick).")
    if n == 1:
        steps.append("This uses your last IR slot, leaving none for your own injuries.")
    return {"kind": "stash", "urgency": "soon" if soon else "optional", "by": claim if soon else None,
            "player": p["id"], "drop": drop, "title": "IR stash: %s (back %s)" % (p["name"], back),
            "detail": detail, "why": " ".join(why), "steps": steps, "net": round(net, 1)}


# ---------- how your players compare with their projections ----------

def team_games_played(calendar, team):
    return sum(1 for g in calendar.by_team.get(team, []) if g["date"] >= calendar.start and g["tip"] <= calendar.now)


def team_report(league, players, me, priors, calendar):
    """Your players against their preseason projections: this season's points per game and
    minutes next to the draft board's (the prior the values start from) and ESPN's, how far the
    model has moved toward the actual average, and flags when the gap is more than noise."""
    board = priors.get("players") or {}
    rows = []
    for e in roster(league, me):
        p = players.get(e["id"])
        if not p:
            continue
        b = board.get(str(p["id"])) or {}
        espn_proj = p["stats"].get("proj") or [None, None, None]
        season = p["stats"].get("season")
        gp = season[0] if season else 0
        row = {"id": p["id"], "slot": e["slot"], "gp": gp, "team_gp": team_games_played(calendar, p["team"]),
               "fppg": season[2] if gp else None, "mpg": season[1] if gp else None,
               "proj": p["ros"]["prior"], "proj_from": p["ros"]["prior_from"],
               "proj_mpg": b.get("mpg") or espn_proj[1], "espn": b.get("espn_fppg") or espn_proj[2],
               "now": p["ros"]["fppg"], "moved": round(1 - p["ros"]["prior_weight"], 2) if gp else 0.0,
               "gap": None, "flags": []}
        if gp:
            row["gap"] = round(row["fppg"] - row["proj"], 1)
            if gp >= HOT_GAMES and abs(row["gap"]) >= HOT_SHARE * row["proj"]:
                row["flags"].append("hot" if row["gap"] > 0 else "cold")
            if row["proj_mpg"] and gp >= ROLE_GAMES and abs(row["mpg"] - row["proj_mpg"]) >= ROLE_MINUTES:
                row["flags"].append("min_up" if row["mpg"] > row["proj_mpg"] else "min_down")
        rows.append(row)
    rows.sort(key=lambda r: -r["now"])

    played = [r for r in rows if r["gp"]]
    games = sum(r["gp"] for r in played)
    team = {"games": games, "team_games": sum(r["team_gp"] for r in rows)}
    if games:
        team["points"] = round(sum(r["gp"] * r["fppg"] for r in played))
        team["projected"] = round(sum(r["gp"] * r["proj"] for r in played))
        team["mpg"] = round(sum(r["gp"] * r["mpg"] for r in played) / games, 1)
        timed = [r for r in played if r["proj_mpg"]]
        if timed:
            team["proj_mpg"] = round(sum(r["gp"] * r["proj_mpg"] for r in timed) / sum(r["gp"] for r in timed), 1)
    return {"rows": rows, "team": team, "league": projection_check(players, board)}


def projection_check(players, board):
    """Which preseason projection is tracking better, over every rostered player the draft board
    valued who has played: the games-weighted average miss per game, and the average of actual
    minus projected (positive: players are beating it)."""
    n = games = 0
    miss = {"board": 0.0, "espn": 0.0}
    bias = {"board": 0.0, "espn": 0.0}
    for p in players.values():
        b = board.get(str(p["id"]))
        season = p["stats"].get("season")
        if not p["owner"] or not b or not b.get("espn_fppg") or not season or not season[0]:
            continue
        gp, actual = season[0], season[2]
        n, games = n + 1, games + gp
        for k, proj in (("board", b["fppg"]), ("espn", b["espn_fppg"])):
            miss[k] += gp * abs(actual - proj)
            bias[k] += gp * (actual - proj)
    out = {"players": n, "games": games}
    if games:
        for k in miss:
            out[k + "_miss"] = round(miss[k] / games, 1)
            out[k + "_bias"] = round(bias[k] / games, 1)
    return out
