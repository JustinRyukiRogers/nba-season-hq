"""One refresh: pull from ESPN, value every player, work out the moves, write the page's data.

    python3 -m seasonkit refresh        -> site/data.json (and a summary in the terminal)

The hosted copy runs this from GitHub Actions every 20 minutes, then encrypts the result.
"""
import json
import time
from datetime import datetime

from draftkit import calibrate, espn
from draftkit.config import DATA, ROOT, SEASON, SLOT_NAMES, TEAMS, load_env, read_json, write_json
from draftkit.sources import norm_name

from . import advice, model, pull

SITE = ROOT / "site"
CACHE = ROOT / "cache"                 # public data only (news, schedule, injuries); safe to keep in CI
NOTES = DATA / "season_notes.json"
POSITIONS = ("PG", "SG", "SF", "PF", "C")
MY_TEAM_FALLBACK = 3


class Stale(Exception):
    """A source failed and there is no earlier copy to fall back on."""


def cached(name, fetch, problems):
    """Fetch a public source, keeping the last good copy in cache/ for when ESPN hiccups."""
    path = CACHE / ("%s.json" % name)
    try:
        data = fetch()
        write_json(path, {"at": int(time.time()), "data": data})
        return data
    except Exception as err:
        old = read_json(path)
        if not old:
            raise Stale("%s: %s" % (name, err))
        problems.append("%s failed (%s); using the copy from %s" % (name, type(err).__name__,
                        time.strftime("%b %d %H:%M", time.localtime(old["at"]))))
        return old["data"]


# ---------- normalizing ESPN's shapes ----------

def normalize_league(raw, swid=None):
    s = raw["settings"]
    counts = {SLOT_NAMES[int(k)]: n for k, n in s["rosterSettings"]["lineupSlotCounts"].items()
              if n and int(k) in SLOT_NAMES}
    starting = [name for sid, name in sorted(SLOT_NAMES.items()) if name not in ("BE", "IR")
                for _ in range(counts.get(name, 0))]
    acq = s.get("acquisitionSettings") or {}
    me = None
    teams = {}
    for t in raw.get("teams", []):
        rec = (t.get("record") or {}).get("overall") or {}
        owners = set(t.get("owners") or [])
        if swid and swid in owners:
            me = t["id"]
        teams[str(t["id"])] = {
            "id": t["id"], "name": (t.get("name") or "").strip(), "abbrev": t.get("abbrev"),
            "record": {"w": rec.get("wins", 0), "l": rec.get("losses", 0), "t": rec.get("ties", 0),
                       "pf": round(rec.get("pointsFor", 0.0), 1), "pa": round(rec.get("pointsAgainst", 0.0), 1)},
            "waiver_rank": t.get("waiverRank"), "seed": t.get("playoffSeed"),
            "adds": (t.get("transactionCounter") or {}).get("matchupAcquisitionTotals") or {},
            "roster": [{"id": e["playerId"], "slot": SLOT_NAMES.get(e.get("lineupSlotId"), "BE"),
                        "acquired": e.get("acquisitionType"), "since": (e.get("acquisitionDate") or 0) // 1000}
                       for e in (t.get("roster") or {}).get("entries", [])],
        }
    matchups = []
    for m in raw.get("schedule", []):
        home, away = m.get("home") or {}, m.get("away") or {}
        matchups.append({"period": m["matchupPeriodId"], "home": home.get("teamId"), "away": away.get("teamId"),
                         "home_pts": home.get("totalPointsLive", home.get("totalPoints")),
                         "away_pts": away.get("totalPointsLive", away.get("totalPoints")),
                         "winner": m.get("winner"), "playoff": m.get("playoffTierType") != "NONE"})
    status = raw.get("status") or {}
    sched = s["scheduleSettings"]
    per_day = acq.get("matchupLimitPerScoringPeriod")
    return {
        "name": s["name"], "size": s["size"], "me": me or MY_TEAM_FALLBACK, "me_from_cookie": bool(me),
        "slots": counts, "starting": starting, "ir_slots": counts.get("IR", 0),
        "acq": {"type": acq.get("acquisitionType"), "waiver_hours": acq.get("waiverHours"),
                "limit": acq.get("matchupAcquisitionLimit"), "per_day": bool(per_day)},
        "periods": len(sched.get("matchupPeriods") or {}), "regular": sched.get("matchupPeriodCount"),
        "final_period": status.get("finalScoringPeriod"), "period": status.get("currentMatchupPeriod"),
        "scoring_period": raw.get("scoringPeriodId"),
        "trade_deadline": (s.get("tradeSettings") or {}).get("deadlineDate"),
        "teams": teams, "matchups": matchups,
    }


def _split(stats, key_id):
    s = next((x for x in stats if x.get("id") == key_id), None)
    if not s or not s.get("stats") or not s["stats"].get("42"):
        return None
    gp = s["stats"]["42"]
    pts = s.get("appliedTotal")
    return [int(round(gp)), round(s["stats"].get("40", 0) / gp, 1), round((pts or 0) / gp, 1)]


def normalize_player(entry, starting, ids):
    pl = entry["player"]
    stats = pl.get("stats") or []
    own = pl.get("ownership") or {}
    eligible = {SLOT_NAMES.get(s) for s in pl.get("eligibleSlots", [])}
    recent = [[x.get("scoringPeriodId"), round(x.get("appliedTotal") or 0, 1), round((x.get("stats") or {}).get("40", 0))]
              for x in stats if x.get("statSplitTypeId") == 5 and x.get("statSourceId") == 0 and x.get("stats")
              and x.get("seasonId") == SEASON]
    return {
        "id": pl["id"], "name": pl["fullName"], "team": TEAMS.get(pl.get("proTeamId"), "FA"),
        "pos": [p for p in POSITIONS if p in eligible], "slots": sorted(e for e in eligible if e in starting),
        "owner": entry.get("onTeamId") or 0, "fa": entry.get("status"),
        "waiver_until": (entry.get("waiverProcessDate") or 0) // 1000 if entry.get("status") == "WAIVERS" else None,
        "status": pl.get("injuryStatus") or "ACTIVE", "droppable": pl.get("droppable", True),
        "own": round(own.get("percentOwned", 0.0), 1), "own_chg": round(own.get("percentChange", 0.0), 2),
        "last_news": pl.get("lastNewsDate"),
        "stats": {k: v for k, v in ((k, _split(stats, sid)) for k, sid in ids.items()) if v},
        "recent": sorted(recent, key=lambda r: -(r[0] or 0)),
    }


def load_notes(players):
    """data/season_notes.json, keyed by player name -> {player id: note}."""
    raw = (read_json(NOTES, {}) or {}).get("players") or {}
    by_name = {norm_name(p["name"]): pid for pid, p in players.items()}
    out = {}
    for nm, note in raw.items():
        pid = by_name.get(norm_name(nm))
        if pid:
            out[pid] = note
    return out


def normalize_transactions(txs, limit=40):
    out = []
    for t in txs[:limit]:
        adds = [i["playerId"] for i in t.get("items", []) if i.get("type") == "ADD"]
        drops = [i["playerId"] for i in t.get("items", []) if i.get("type") == "DROP"]
        trades = [i for i in t.get("items", []) if i.get("type") == "TRADE"]
        out.append({"at": (t.get("processDate") or t.get("proposedDate") or 0) // 1000, "team": t.get("teamId"),
                    "type": t.get("type"), "adds": adds, "drops": drops,
                    "trade": [[i["playerId"], i.get("fromTeamId"), i.get("toTeamId")] for i in trades]})
    return out


# ---------- the refresh ----------

def build(cookies=None, now=None, verbose=True, with_news=True):
    cookies = cookies or load_env()
    if not cookies:
        raise SystemExit("No ESPN cookies: set ESPN_S2 and SWID in .env or the environment")
    now = int(now or time.time())
    problems = []
    raw_league = pull.league(cookies)
    league = normalize_league(raw_league, cookies.get("SWID"))
    me = league["me"]
    raw_pool = pull.pool(cookies)
    games = cached("schedule", pull.schedule, problems)
    injuries = {int(k): v for k, v in cached("injuries", pull.injuries, problems).items()}
    calendar = model.Calendar(games, league["periods"], league["final_period"], league["regular"], now=now)

    ids = pull.stat_ids()
    players = {}
    for entry in raw_pool:
        p = normalize_player(entry, league["starting"], ids)
        p["inj"] = injuries.get(p["id"])
        players[p["id"]] = p
    # rostered players beyond the pool limit (rare: someone holds a deep bench player)
    for t in league["teams"].values():
        for e in t["roster"]:
            if e["id"] not in players:
                problems.append("player %s on %s is outside the pulled pool" % (e["id"], t["abbrev"]))

    priors = read_json(DATA / "priors.json", {"players": {}})
    cal_fit = priors.get("calibration") or calibrate.load()
    notes = load_notes(players)
    for p in players.values():
        model.value_player(p, priors, injuries, notes, calendar, cal_fit)
        if p["id"] in notes and notes[p["id"]].get("note"):
            p["note"] = notes[p["id"]]["note"]
    fas = [p for p in players.values() if p["owner"] == 0]
    replacement = model.replacement_level(fas)
    model.finish_values(players.values(), replacement)

    # schedule: the next week of games for every player in the pool, for cards and stream lists
    horizon = now + 8 * 86400
    for p in players.values():
        p["next"] = [[str(g["date"]), g["opp"], g["home"], g["tip"]] for g in calendar.remaining(p["team"])
                     if g["tip"] <= horizon]
        week = calendar.current_week()
        days = {str(d) for d in calendar.days_left_in_week(week)} if week is not None else set()
        p["week"] = {"games": sum(1 for d in days if d in p["odds"]),
                     "points": round(sum(p["odds"].get(d, 0.0) for d in days) * p["ros"]["fppg"], 1)}

    # transactions from the last four scoring days (also: has today's add been used?)
    sp = league["scoring_period"] or 1
    try:
        txs = normalize_transactions(pull.transactions(cookies, range(max(1, sp - 3), sp + 1)))
    except Exception as err:
        problems.append("transactions failed (%s)" % type(err).__name__)
        txs = []

    # moves
    ir, free_ir = advice.ir_actions(league, players, me)
    lineup = advice.lineup_check(league, players, me, calendar)
    ups = advice.upgrades(league, players, me)
    stash_list = advice.stash(league, players, me, free_ir)
    stream_list = advice.streams(league, players, me, calendar, txs)
    hunch_list = advice.hunches(league, players, me, calendar, stream_list)
    on_waivers = [p for p in fas if p["fa"] == "WAIVERS"]
    clears = min((p["waiver_until"] for p in on_waivers if p["waiver_until"]), default=None)
    waivers = {"all_on_waivers": bool(fas) and len(on_waivers) == len(fas), "clears": clears,
               "clears_label": datetime.fromtimestamp(clears, model.ET).strftime("%a %b %-d, %-I:%M %p ET") if clears else None,
               "my_rank": league["teams"][str(me)]["waiver_rank"],
               "order": sorted((t["waiver_rank"] or 99, int(tid)) for tid, t in league["teams"].items())}
    # a stash worth beating a deadline for takes the first add of the week plan
    pending = (advice.stash_action(league, players, me, stash_list, waivers, calendar)
               if stash_list["free_ir"] > 0 and stash_list["list"] else None)
    reserve = {"id": pending["player"], "drop": pending["drop"]} if pending and pending["urgency"] in ("now", "soon") else None
    plan = advice.week_plan(league, players, me, calendar, txs, reserve)
    trades = advice.trade_ideas(league, players, me, calendar)

    # starts, not games: what each roster's best daily lineups get out of it, if it is held
    for tid, t in league["teams"].items():
        ids = [e["id"] for e in t["roster"] if e["slot"] != "IR" and e["id"] in players]
        outlook, pts = advice.lineup_outlook(ids, players, league["starting"], calendar)
        weeks = len(calendar.weeks) - (calendar.current_week() or 0)
        t["outlook"] = {"points": round(pts), "per_week": round(pts / weeks) if weeks else 0}
        if int(tid) == me:
            for pid, o in outlook.items():
                players[pid]["lineup"] = o
    match = advice.matchup(league, players, me, calendar)
    opp = match["opp"] if match else None
    acts = advice.actions(league, players, me, calendar, ir, lineup, ups, stash_list, stream_list, waivers, plan, trades)
    report = advice.team_report(league, players, me, priors, calendar)

    # news: your roster, this week's opponent, and the free agents worth a look
    interesting = {e["id"] for e in league["teams"][str(me)]["roster"]}
    if opp:
        interesting |= {e["id"] for e in league["teams"][str(opp)]["roster"]}
    interesting |= {r["id"] for r in ups["list"]} | {r["id"] for r in stash_list["list"]} | {r["id"] for r in stream_list["list"]}
    interesting |= {r["id"] for r in hunch_list["list"]}
    interesting |= {p["id"] for p in sorted(fas, key=lambda p: -p["ros"]["value"])[:40]}
    interesting |= {p["id"] for p in players.values() if p["owner"] and p["status"] != "ACTIVE"}
    news_cache = (read_json(CACHE / "news.json", {}) or {})
    fetched = 0
    if with_news:
        fetched = pull.refresh_news(news_cache, sorted(interesting), {pid: players[pid]["last_news"] for pid in interesting})
        write_json(CACHE / "news.json", news_cache)
    for pid in interesting:
        items = (news_cache.get(str(pid)) or {}).get("items")
        if items:
            players[pid]["news"] = items[:6]

    data = {
        "generated": now, "generated_label": datetime.fromtimestamp(now, model.ET).strftime("%a %b %-d, %-I:%M %p ET"),
        "problems": problems,
        "league": {k: league[k] for k in ("name", "size", "me", "me_from_cookie", "slots", "starting", "ir_slots", "acq",
                                          "periods", "regular", "period", "scoring_period", "trade_deadline", "teams",
                                          "matchups")},
        "calendar": {"today": str(calendar.today), "start": str(calendar.start), "end": str(calendar.end),
                     "weeks": [[str(a), str(b)] for a, b in calendar.weeks], "regular": calendar.regular,
                     "week": calendar.current_week()},
        "replacement": round(replacement, 2),
        "model": {"prior_games": model.PRIOR_GAMES, "dtd_play": model.DTD_PLAY, "priors_from": priors.get("board_generated")},
        "players": {str(pid): page_player(p, calendar) for pid, p in players.items()},
        "actions": acts, "lineup": lineup, "upgrades": ups, "stash": stash_list, "streams": stream_list,
        "hunches": hunch_list,
        "plan": plan, "trades": trades,
        "matchup": match, "waivers": waivers, "drop_order": advice.drop_order(league, players, me), "team_report": report,
        "injuries": advice.injury_board(league, players, me, opp, calendar), "transactions": txs,
        "news_fetched": fetched,
    }
    SITE.mkdir(exist_ok=True)
    write_json(SITE / "data.json", data)
    if verbose:
        summary(data)
    return data


def page_player(p, calendar):
    """What the page needs of a player: the per-game odds only for the next 8 days."""
    out = {k: v for k, v in p.items() if k not in ("last_news", "odds")}
    soon = {str(g[0]) for g in p.get("next", [])}
    out["odds"] = {d: v for d, v in p["odds"].items() if d in soon}
    return out


def summary(data):
    pl, lg = data["players"], data["league"]
    me = str(lg["me"])
    print("refreshed %s: %d players, replacement %.1f FPPG, team %s%s" % (
        data["generated_label"], len(pl), data["replacement"], lg["teams"][me]["abbrev"],
        "" if lg["me_from_cookie"] else " (fallback id: cookie did not match an owner)"))
    for prob in data["problems"]:
        print("  note:", prob)
    print("\nactions:")
    for a in data["actions"]:
        by = " (by %s)" % datetime.fromtimestamp(a["by"], model.ET).strftime("%a %-I:%M %p") if a.get("by") else ""
        print("  [%s%s] %s" % (a["urgency"], by, a["title"]))
        if a.get("why"):
            print("      why: %s" % a["why"])
    print("\nyour roster (lowest value first is the drop order):")
    for e in sorted(lg["teams"][me]["roster"], key=lambda e: -pl[str(e["id"])]["ros"]["value"]):
        p = pl[str(e["id"])]
        r, lu = p["ros"], p.get("lineup") or {}
        print("  %-3s %-24s %-3s %-11s ros %5.1f  games %4.1f  starts %4.1f  value %6.1f%s" % (
            e["slot"], p["name"][:24], p["team"], p["status"], r["fppg"], r["games"], lu.get("starts", 0), r["value"],
            "  back %s" % r["back"] if r["out"] else ""))
    plan = data["plan"]
    if plan["days"]:
        print("\nweek plan: %.0f points from your lineups (%.0f without moves)" % (plan["points"], plan["points_now"]))
        for m in plan["moves"]:
            what = "IR stash" if m["kind"] == "stash" else "+%.0f that day" % m["gain"]
            print("  %s: add %s, drop %s (%s)" % (m["day"], pl[str(m["id"])]["name"],
                                                  pl[str(m["drop"])]["name"] if m["drop"] else "nobody: open spot", what))
    if data["hunches"]["list"]:
        print("\nstreams on a hunch (%s; net vs the top stream):" % data["hunches"]["days"][0])
        for r in data["hunches"]["list"]:
            print("  %-24s %+5.0f (%+.0f)  %s" % (pl[str(r["id"])]["name"], r["net"], r["vs_top"], "; ".join(r["signs"])))
    print("\ntrade ideas (your gain in season lineup points; theirs on ESPN's numbers):")
    for t in data["trades"]["list"][:6]:
        names = lambda ids: " + ".join(pl[str(i)]["name"] for i in ids)
        print("  %-4s give %s, get %s: %+d (them %+d ESPN, %+d here)" % (
            lg["teams"][str(t["team"])]["abbrev"], names(t["give"]), names(t["get"]), t["gain"], t["their_espn"], t["their_gain"]))
    print("\nbest free agents (rest-of-season value):")
    for row in data["upgrades"]["list"][:8]:
        p = pl[str(row["id"])]
        print("  %-24s %-3s %-9s %-11s ros %5.1f  value %6.1f  +%.0f over %s" % (
            p["name"][:24], p["team"], p["fa"], p["status"], p["ros"]["fppg"], p["ros"]["value"], row["gain"],
            pl[str(data["upgrades"]["drop"])]["name"]))
    print("\nIR stash candidates (free IR slots: %d):" % data["stash"]["free_ir"])
    for row in data["stash"]["list"][:8]:
        p = pl[str(row["id"])]
        print("  %-24s %-3s back %s  ros %5.1f  games after %4.1f  value %6.1f" % (
            p["name"][:24], p["team"], row["back"], p["ros"]["fppg"], row["games_after"], p["ros"]["value"]))
    t, chk = data["team_report"]["team"], data["team_report"]["league"]
    if t["games"]:
        print("\nyour players vs projection: %d games, %d points vs %d projected; %.1f min vs %s projected" % (
            t["games"], t["points"], t["projected"], t["mpg"], t.get("proj_mpg", "?")))
    if chk["games"]:
        print("projection check, %d rostered players over %d games: board misses %.1f/game (bias %+.1f), ESPN %.1f (%+.1f)" % (
            chk["players"], chk["games"], chk["board_miss"], chk["board_bias"], chk["espn_miss"], chk["espn_bias"]))
    m = data["matchup"]
    if m and "me" in m:
        print("\nweek %d vs %s: %.0f-%.0f now, projected %.0f-%.0f" % (
            m["period"], lg["teams"][str(m["opp"])]["abbrev"], m["me"]["score"], m["them"]["score"],
            m["me"]["projected"], m["them"]["projected"]))
