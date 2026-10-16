"""What to do with the roster: IR moves, today's lineup, pickups, stashes and streams, how urgent
each one is, and how your players are doing against their projections.

Everything here reads the normalized league and the valued players from build.py and returns
plain data for the page. Nothing writes to ESPN.
"""
from datetime import datetime, timedelta

from .model import ET, best_lineup, parse_day, project_days

STREAM_DROPS = 3          # drop candidates tried for each stream: your lowest-valued players
STREAM_POOL = 40          # free agents tried as streams, by expected points this week
LINEUP_MIN_GAIN = 0.5     # expected points before a lineup change is worth suggesting

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


def roster(league, team_id, include_ir=True):
    return [e for e in league["teams"][str(team_id)]["roster"] if include_ir or e["slot"] != "IR"]


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
    healthy = sorted((p for p in free_agents(players) if not p["ros"]["out"]), key=lambda p: -p["ros"]["value"])
    return {"free_ir": free_ir, "drop": weakest(players, active), "refill": healthy[0]["id"] if healthy else None,
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

def actions(league, players, me, calendar, ir, lineup, ups, stash_list, stream_list, waivers):
    """The few things worth doing, each with how urgent it is (URGENCY) and, when there is one, the
    deadline ("by", epoch seconds): most urgent first, then the nearest deadline."""
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
    if stream_list["list"] and stream_list["list"][0]["net"] >= 10:
        acts.append(stream_action(league, players, stream_list, calendar))
    if ups["list"] and ups["list"][0]["gain"] >= 25:
        acts.append(upgrade_action(players, ups, waivers))
    if stash_list["free_ir"] > 0 and stash_list["list"]:
        acts.append(stash_action(league, players, me, stash_list, waivers, calendar))
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
    return {"kind": "upgrade", "urgency": "now" if hot else "soon", "by": claim, "player": u["id"], "drop": ups["drop"],
            "title": "Upgrade: %s over %s (+%.0f value)" % (p["name"], name(players, ups["drop"]), u["gain"]),
            "why": " ".join(why)}


def stash_action(league, players, me, stash_list, waivers, calendar):
    """The best stash as a to-do. Holding him costs nothing while he is Out; the add itself may
    need a roster spot, so the steps cover ESPN asking for a drop. Urgency: a claim has a deadline,
    and a player many ESPN leagues roster will interest this one too."""
    st = stash_list["list"][0]
    p = players[st["id"]]
    drop, refill, n = stash_list["drop"], stash_list.get("refill"), stash_list["free_ir"]
    value, claim = p["ros"]["value"], _claim(p)
    interest = p["own"] >= INTEREST_OWNED or p["own_chg"] >= INTEREST_ADDS
    soon = value >= STASH_SOON_VALUE and (bool(claim and claim - calendar.now <= 2 * 86400) or interest)

    why = []
    if claim:
        why.append("Claims process %s, and %s claim before you; after that he is first come, first served."
                   % (clock(claim), plural(_ahead(waivers), "team")))
    why.append("%.0f%% owned across ESPN (%+.1f today)." % (p["own"], p["own_chg"]))
    if not soon:
        why.append("A modest gain, so no rush." if value < STASH_SOON_VALUE else "Few leagues want him, so he should still be there.")

    swap = max(0.0, players[drop]["ros"]["value"] - players[refill]["ros"]["value"]) if drop and refill else 0.0
    mine = [players[e["id"]] for e in roster(league, me) if e["id"] in players and e["id"] != p["id"]]
    like = min(mine, key=lambda q: abs(q["ros"]["value"] - value)) if mine else None
    back = parse_day(st["back"]).strftime("%b %-d") if st["back"] else "?"
    detail = ("Worth about %.0f over a waiver pickup once he is back%s; that already allows for a late return. "
              "He costs nothing while he sits in IR (%s free)." % (
                  value, " (close to %s's value to you)" % like["name"] if like else "", plural(n, "slot")))
    if drop:
        detail += " Gain: +%.0f if ESPN puts him straight into IR, about +%.0f if you have to drop %s for him." % (
            value, value - swap, name(players, drop))

    steps = []
    dname = name(players, drop) if drop else None
    if drop and value - swap <= 0:
        steps.append("Add him only if ESPN lets you put him straight into IR: swapping out %s costs more than he adds." % dname)
    elif claim:
        steps.append("Claim him before %s. If ESPN asks for a drop, pick %s; the drop only happens if you win "
                     "the claim." % (clock(claim), dname))
    else:
        steps.append("Add him. If ESPN asks for a drop, pick %s." % dname)
    steps.append("Once he is yours, move him to IR.")
    if drop and refill and value - swap > 0:
        refill_step = "If you dropped %s, use the open spot on the best free agent (%s right now)." % (dname, name(players, refill))
        if calendar.today < calendar.start and league["acq"].get("per_day"):
            refill_step += (" ESPN may count the days before the opener as one day for the one-add-a-day "
                            "limit; if it refuses, the add goes through %s." % (calendar.start + timedelta(days=1)).strftime("%a %b %-d"))
        steps.append(refill_step)
    if n == 1:
        steps.append("This uses your last IR slot, leaving none for your own injuries.")
    return {"kind": "stash", "urgency": "soon" if soon else "optional", "by": claim if soon else None,
            "player": p["id"], "drop": drop, "title": "IR stash: %s (back %s)" % (p["name"], back),
            "detail": detail, "why": " ".join(why), "steps": steps}


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
