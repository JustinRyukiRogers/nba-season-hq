"""What to do with the roster: IR moves, today's lineup, pickups, stashes and streams.

Everything here reads the normalized league and the valued players from build.py and returns
plain data for the page. Nothing writes to ESPN.
"""
from datetime import timedelta

from .model import best_lineup, project_days

STREAM_DROPS = 3          # drop candidates tried for each stream: your lowest-valued players
STREAM_POOL = 40          # free agents tried as streams, by expected points this week
LINEUP_MIN_GAIN = 0.5     # expected points before a lineup change is worth suggesting


def roster(league, team_id, include_ir=True):
    return [e for e in league["teams"][str(team_id)]["roster"] if include_ir or e["slot"] != "IR"]


def weakest(players, entries, exclude=()):
    """The droppable player with the lowest rest-of-season value. -> pid or None."""
    pool = [players[e["id"]] for e in entries if e["id"] in players and e["id"] not in exclude
            and players[e["id"]].get("droppable", True)]
    return min(pool, key=lambda p: p["ros"]["value"])["id"] if pool else None


def name(players, pid):
    return players[pid]["name"] if pid in players else "Player %s" % pid


# ---------- injured reserve ----------

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
            acts.append({"kind": "ir_blocked", "level": "critical", "player": p["id"], "drop": drop,
                         "title": "%s is no longer Out, so ESPN blocks your adds" % p["name"],
                         "detail": "Activate him from IR before any pickup or claim. That needs a free roster "
                                   "spot: %s has the lowest value on your roster." % name(players, drop)})
    outs = [players[e["id"]] for e in active if e["id"] in players and players[e["id"]]["status"] == "OUT"]
    outs.sort(key=lambda p: -p["ros"]["absent"])
    ir_players = [players[e["id"]] for e in on_ir if e["id"] in players and players[e["id"]]["status"] == "OUT"]
    for p in outs:
        back = p["ros"]["back"]
        when = "out for the season" if p["ros"]["season_over"] else ("expected back %s" % back if back else "no return date")
        if free_ir > 0:
            free_ir -= 1
            acts.append({"kind": "to_ir", "level": "high", "player": p["id"],
                         "title": "Move %s to IR" % p["name"],
                         "detail": "He is tagged Out (%s). The move is free and opens a roster spot for a pickup." % when})
            continue
        soonest = min(ir_players, key=lambda q: q["ros"]["absent"]) if ir_players else None
        if soonest and p["ros"]["absent"] > soonest["ros"]["absent"] + 1:
            acts.append({"kind": "ir_swap", "level": "medium", "player": p["id"], "other": soonest["id"],
                         "title": "IR is full: swap %s in for %s" % (p["name"], soonest["name"]),
                         "detail": "%s is due back sooner (%s), so he costs you fewer games on the bench than "
                                   "%s (%s)." % (soonest["name"], soonest["ros"]["back"], p["name"], when)})
        else:
            acts.append({"kind": "ir_full", "level": "medium", "player": p["id"],
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
    for e in roster(league, me, include_ir=False):
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

def free_agents(players, instant_only=False):
    return [p for p in players.values() if p["owner"] == 0
            and (p["fa"] == "FREEAGENT" or (not instant_only and p["fa"] == "WAIVERS"))]


def upgrades(league, players, me, top=12):
    """Free agents worth more over the rest of the season than your weakest player."""
    active = roster(league, me, include_ir=False)
    drop = weakest(players, active)
    if drop is None:
        return {"drop": None, "list": []}
    floor = players[drop]["ros"]["value"]
    fas = sorted((p for p in free_agents(players) if not p["ros"]["out"]),   # Out players: see stash()
                 key=lambda p: -p["ros"]["value"])
    rows = [{"id": p["id"], "gain": round(p["ros"]["value"] - floor, 1)} for p in fas if p["ros"]["value"] > floor]
    return {"drop": drop, "list": rows[:top]}


def stash(league, players, me, free_ir, top=12):
    """Free agents tagged Out (so IR-eligible) ranked by what they add once back. Holding one costs
    nothing while he is Out, but the add itself needs a roster spot at the time."""
    rows = [p for p in free_agents(players) if p["status"] == "OUT" and not p["ros"]["season_over"]
            and p["ros"]["value"] > 0]
    rows.sort(key=lambda p: -p["ros"]["value"])
    active = roster(league, me, include_ir=False)
    return {"free_ir": free_ir, "drop": weakest(players, active),
            "list": [{"id": p["id"], "back": p["ros"]["back"], "games_after": p["ros"]["games"]} for p in rows[:top]]}


def streams(league, players, me, calendar, top=10):
    """Best single add for the rest of this matchup week: the points it adds to your daily lineups,
    against what dropping your weakest players gives up. Adds count from tomorrow once today's first
    game has tipped off (conservative: rosters lock at the day's first game)."""
    week = calendar.current_week()
    if week is None:
        return {"days": [], "list": []}
    days = calendar.days_left_in_week(week)
    first = calendar.first_tip(calendar.today)
    if days and days[0] == calendar.today and (calendar.today < calendar.start or (first and calendar.now >= first)):
        days = days[1:]
    days = [d for d in days if d >= calendar.start]
    starting = league["starting"]
    mine = [e["id"] for e in roster(league, me, include_ir=False)]
    base, _ = project_days(mine, days, players, starting, calendar)
    drops = sorted((pid for pid in mine if pid in players and players[pid].get("droppable", True)),
                   key=lambda pid: players[pid]["ros"]["value"])[:STREAM_DROPS]

    def week_points(p):
        return sum(p["odds"].get(str(d), 0.0) for d in days) * p["ros"]["fppg"]

    pool = sorted((p for p in free_agents(players, instant_only=True) if not p["ros"]["out"]),
                  key=lambda p: -week_points(p))[:STREAM_POOL]
    best = {}
    for c in pool:
        for x in drops:
            alt, _ = project_days([pid for pid in mine if pid != x] + [c["id"]], days, players, starting, calendar)
            gain = alt - base
            cost = max(0.0, players[x]["ros"]["value"] - c["ros"]["value"])
            row = {"id": c["id"], "drop": x, "gain": round(gain, 1), "ros_cost": round(cost, 1),
                   "net": round(gain - cost, 1),
                   "games": sum(1 for d in days if str(d) in c["odds"])}
            if c["id"] not in best or row["net"] > best[c["id"]]["net"]:
                best[c["id"]] = row
    rows = sorted(best.values(), key=lambda r: -r["net"])
    return {"days": [str(d) for d in days], "base": round(base, 1), "list": [r for r in rows if r["gain"] > 0][:top]}


def drop_order(league, players, me):
    active = [players[e["id"]] for e in roster(league, me, include_ir=False) if e["id"] in players]
    return [p["id"] for p in sorted(active, key=lambda p: p["ros"]["value"])]


# ---------- the matchup ----------

def matchup(league, players, me, calendar):
    week = calendar.current_week()
    if week is None:
        return None
    period = week + 1
    m = next((m for m in league["matchups"] if m["period"] == period and me in (m["home"], m["away"])), None)
    if not m:
        return None
    opp = m["away"] if m["home"] == me else m["home"]
    days = [d for d in calendar.days_left_in_week(week) if d >= calendar.start]
    score = {me: m["home_pts"] if m["home"] == me else m["away_pts"],
             opp: m["away_pts"] if m["home"] == me else m["home_pts"]}
    out = {"period": period, "opp": opp, "playoffs": week >= calendar.regular,
           "dates": [str(calendar.weeks[week][0]), str(calendar.weeks[week][1])]}
    for side, tid in (("me", me), ("them", opp)):
        if tid is None:
            continue
        ids = [e["id"] for e in roster(league, tid, include_ir=False)]
        pts, per_day = project_days(ids, days, players, league["starting"], calendar)
        out[side] = {"team": tid, "score": round(score[tid] or 0.0, 1), "remaining": round(pts, 1),
                     "projected": round((score[tid] or 0.0) + pts, 1),
                     "starts": sum(n for _, _, n in per_day), "by_day": per_day}
    return out


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
        if p["ros"]["out"] and back and (calendar.today + timedelta(days=7)) <= _day(back):
            pos = set(p["pos"])
            mates = [q for q in free_agents(players) if q["team"] == p["team"] and q["id"] != p["id"]
                     and pos & set(q["pos"]) and not q["ros"]["out"]]
            mates.sort(key=lambda q: -q["ros"]["value"])
            row["beneficiaries"] = [q["id"] for q in mates[:beneficiaries]]
        rows.append(row)
    order = {"mine": 0, "opp": 1, "fa": 2, "league": 3}
    rows.sort(key=lambda r: (order[r["who"]], -players[r["id"]]["ros"]["value"]))
    return rows


def _day(s):
    from .model import parse_day
    return parse_day(s)


# ---------- the action list ----------

def actions(league, players, me, calendar, ir, lineup, ups, stash_list, stream_list, waivers):
    """The few things worth doing now, most urgent first."""
    acts = list(ir)
    if lineup.get("state") == "season":
        for w in lineup["warnings"]:
            acts.append({"kind": "lineup_warning", "level": "high", "player": w["player"], "title": w["text"]})
        if lineup["start"]:
            acts.append({"kind": "lineup", "level": "high", "title": "Fix today's lineup (+%.1f expected points)" % lineup["gain"],
                         "start": lineup["start"], "sit": lineup["sit"]})
    if waivers.get("all_on_waivers"):
        acts.append({"kind": "waivers", "level": "info",
                     "title": "Every free agent is on waivers until %s" % waivers["clears_label"],
                     "detail": "Claims process then in waiver order; you are #%s of %d." % (waivers["my_rank"], league["size"])})
    if stream_list["list"]:
        s = stream_list["list"][0]
        if s["net"] >= 10:
            acts.append({"kind": "stream", "level": "medium", "player": s["id"], "drop": s["drop"],
                         "title": "Stream %s for %s (+%.0f points this week)" % (name(players, s["id"]), name(players, s["drop"]), s["gain"]),
                         "detail": "%d game%s left this week." % (s["games"], "" if s["games"] == 1 else "s")
                                   + (" Costs %.0f of rest-of-season value." % s["ros_cost"] if s["ros_cost"] >= 1 else "")})
    if ups["list"] and ups["list"][0]["gain"] >= 25:
        u = ups["list"][0]
        acts.append({"kind": "upgrade", "level": "medium", "player": u["id"], "drop": ups["drop"],
                     "title": "Upgrade: %s over %s (+%.0f value)" % (name(players, u["id"]), name(players, ups["drop"]), u["gain"])})
    if stash_list["free_ir"] > 0 and stash_list["list"]:
        st = stash_list["list"][0]
        n = stash_list["free_ir"]
        acts.append({"kind": "stash", "level": "low", "player": st["id"], "drop": stash_list["drop"],
                     "title": "IR stash: %s (back %s)" % (name(players, st["id"]), st["back"]),
                     "detail": "You have %d free IR slot%s. He costs nothing while he is Out, but ESPN may want a "
                               "free roster spot for the add itself: if so, drop %s, add him, move him to IR, and "
                               "refill the spot the next day.%s"
                               % (n, "" if n == 1 else "s", name(players, stash_list["drop"]),
                                  " That would use your last IR slot, leaving none for your own injuries." if n == 1 else "")})
    rank = {"critical": 0, "high": 1, "medium": 2, "info": 3, "low": 4}
    acts.sort(key=lambda a: rank[a["level"]])
    return acts
