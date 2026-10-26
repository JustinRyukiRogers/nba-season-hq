"""One refresh: pull from ESPN, value every player, work out the moves, write the page's data.

    python3 -m seasonkit refresh        -> site/data.json (and a summary in the terminal)

The hosted copy runs this from GitHub Actions every 20 minutes, then encrypts the result.
"""
import json
import time
from datetime import datetime, timedelta

from draftkit import calibrate, espn
from draftkit.config import (DATA, DEFAULT_SCORING, FGA, FGM, FTA, FTM, MIN, PTS, ROOT, SEASON, SLOT_NAMES, TEAMS, TO,
                             TPA, TPM, load_env, read_json, write_json)
from draftkit.sources import norm_name

from . import advice, model, pull

SITE = ROOT / "site"
CACHE = ROOT / "cache"                 # public data only (news, schedule, injuries); safe to keep in CI
NOTES = DATA / "season_notes.json"
TAKES = DATA / "takes.json"            # analyst takes: private, committed encrypted as takes.enc
TAKE_STANCES = ("for", "against", "caution", "info")
TAKE_DAYS = 7                          # a take with no end date lapses a week after it was written
GAMELOG = CACHE / "gamelog.json"      # every pool player's finished games this season, for the Stats tab
GAMELOG_SETTLE = 3.5 * 3600            # seconds after tip-off before a game counts as finished
GAMELOG_OVERLAP = 3                    # scoring days re-read each refresh, for stat corrections
GAMELOG_MAX = 200                      # most games asked for at once (a lost cache refills the season)
GAMELOG_VERSION = 3                    # 2: each game keeps its shots; 3: and turnovers (an older log is refilled)
SHOTS = (FGA, FGM, TPA, TPM, FTA, FTM)  # the box score a logged game keeps after points and minutes, then TO
BOXES = CACHE / "boxes.json"           # each finished game's team totals, for usage rates
BOXES_MAX_NEW = 150                    # games' team boxes fetched in one refresh (a lost cache refills over a few)
FT_PLAY = 0.44                         # free throw attempts that end a play (the usual usage-rate weight)
LEAGUE_MIN_PER_PLAY = 2.15             # team minutes per team play (FGA + 0.44 FTA + TO), until this season has boxes
OWN_MIN_MINUTES = 500                  # under this many minutes last season, ESPN's projection joins his record
OWN_TS_SHOTS = 100                     # shots (FGA + FTA) before his own true shooting is shown
OWN_SHRINK = {"2": 150, "3": 200, "ft": 50}  # league-average attempts added before taking his own make rates
DEPTH_MAX_AGE = 3 * 3600               # depth charts change slowly; refetch (30 requests) at most this often
POSITIONS = ("PG", "SG", "SF", "PF", "C")
MY_TEAM_FALLBACK = 3


class Stale(Exception):
    """A source failed and there is no earlier copy to fall back on."""


def cached(name, fetch, problems, max_age=None):
    """Fetch a public source, keeping the last good copy in cache/ for when ESPN hiccups. With
    max_age (seconds), a copy younger than that is used without fetching."""
    path = CACHE / ("%s.json" % name)
    if max_age:
        old = read_json(path)
        if old and time.time() - old["at"] < max_age:
            return old["data"]
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

def roster_entries(t):
    return [{"id": e["playerId"], "slot": SLOT_NAMES.get(e.get("lineupSlotId"), "BE"),
             "acquired": e.get("acquisitionType"), "since": (e.get("acquisitionDate") or 0) // 1000}
            for e in (t.get("roster") or {}).get("entries", [])]


def roster_from_tomorrow(league, raw_next):
    """Rosters as they will stand from the next scoring day. ESPN locks rosters at a day's first tip, so
    an IR move, add or drop made after it counts from tomorrow and only shows in tomorrow's rosters.
    Every move suggestion works from those (adds count from tomorrow too); today's lineup check keeps
    today's. Each team's "roster" becomes tomorrow's players, each keeping today's slot unless he
    moves in or out of IR, and carrying "today": the slot today (None for a player added after the
    lock); "roster_today" keeps today's."""
    nxt = {t["id"]: roster_entries(t) for t in raw_next or []}
    for t in league["teams"].values():
        today = t["roster"]
        t["roster_today"] = today
        if t["id"] not in nxt:
            for e in today:
                e["today"] = e["slot"]
            continue
        now = {e["id"]: e for e in today}
        t["roster"] = []
        for e in nxt[t["id"]]:
            was = now.get(e["id"])
            if was is None:                       # added after the lock
                t["roster"].append(dict(e, today=None))
            elif (was["slot"] == "IR") != (e["slot"] == "IR"):
                t["roster"].append(dict(was, slot=e["slot"], today=was["slot"]))
            else:                                 # tomorrow's lineup slots are not set yet: keep today's
                t["roster"].append(dict(was, today=was["slot"]))
    return league


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
            "roster": roster_entries(t),
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


def load_takes(players, today, problems):
    """data/takes.json: judgment calls shown beside the numbers, never fed into them. Each take names
    a player (and any others it is about, "also") and lapses after "until" (default TAKE_DAYS after
    "written"). -> [{"pids", "stance", "on", "text", "written", "until"}], live ones only."""
    raw = (read_json(TAKES, {}) or {}).get("takes") or []
    by_name = {norm_name(p["name"]): pid for pid, p in players.items()}
    out = []
    for t in raw:
        written = model.parse_day(t["written"])
        until = model.parse_day(t["until"]) if t.get("until") else written + timedelta(days=TAKE_DAYS)
        if until < today:
            continue
        names = [t["player"]] + list(t.get("also") or [])
        pids = [by_name.get(norm_name(n)) for n in names]
        if not pids[0]:
            problems.append("analyst take on %s: no such player in the pool" % t["player"])
            continue
        stance = t.get("stance") if t.get("stance") in TAKE_STANCES else "info"
        out.append({"pids": [pid for pid in pids if pid], "stance": stance, "on": t.get("on"), "text": t["text"],
                    "written": str(written), "until": str(until)})
    return out


def seal_takes():
    """Encrypt data/takes.json with the site passphrase (.env SITE_PASSPHRASE) to data/takes.enc,
    which is committed; the refresh on GitHub decrypts it. The repo is public, takes are not."""
    import os
    import subprocess
    env = dict(os.environ)
    for line in (ROOT / ".env").read_text().splitlines() if (ROOT / ".env").exists() else []:
        if line.strip().startswith("SITE_PASSPHRASE="):
            env["SITE_PASSPHRASE"] = line.split("=", 1)[1].strip().strip("'\"")
    if not env.get("SITE_PASSPHRASE"):
        raise SystemExit("SITE_PASSPHRASE is not set in .env or the environment")
    json.loads(TAKES.read_text())          # refuse to seal a file that does not parse
    subprocess.run(["node", str(ROOT / "seasonkit" / "crypt.mjs"), "encrypt", str(TAKES), str(DATA / "takes.enc")],
                   check=True, env=env)
    print("sealed %s -> data/takes.enc; commit and push it" % TAKES.name)


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
    if raw_league.get("scoringPeriodId") and raw_league["scoringPeriodId"] < (league["final_period"] or 0):
        try:
            roster_from_tomorrow(league, pull.rosters(cookies, raw_league["scoringPeriodId"] + 1))
        except Exception as err:
            problems.append("tomorrow's rosters failed (%s); moves made after today's lock are not seen" % type(err).__name__)
            roster_from_tomorrow(league, None)
    else:
        roster_from_tomorrow(league, None)
    me = league["me"]
    log = read_json(GAMELOG, {}) or {}
    if log.get("season") != SEASON or log.get("v") != GAMELOG_VERSION:
        log = {"season": SEASON, "v": GAMELOG_VERSION, "period": 0, "players": {}}
    sp_now = league["scoring_period"] or 1
    raw_pool = pull.pool(cookies, games=max(2, min(GAMELOG_MAX, sp_now - log["period"] + GAMELOG_OVERLAP)))
    games = cached("schedule", pull.schedule, problems)
    injuries = {int(k): v for k, v in cached("injuries", pull.injuries, problems).items()}
    calendar = model.Calendar(games, league["periods"], league["final_period"], league["regular"], now=now)
    update_gamelog(log, raw_pool, games, now)
    log["period"] = sp_now
    write_json(GAMELOG, log)
    boxes = update_boxes(games, now, problems)

    ids = pull.stat_ids()
    players = {}
    for entry in raw_pool:
        p = normalize_player(entry, league["starting"], ids)
        p["inj"] = injuries.get(p["id"])
        players[p["id"]] = p
    try:
        depth_roles(cached("depth", pull.depth_charts, problems, DEPTH_MAX_AGE), players)
    except Stale as err:
        problems.append("depth charts unavailable (%s)" % err)
    # rostered players beyond the pool limit (rare: someone holds a deep bench player)
    for t in league["teams"].values():
        for e in t["roster"]:
            if e["id"] not in players:
                problems.append("player %s on %s is outside the pulled pool" % (e["id"], t["abbrev"]))

    priors = read_json(DATA / "priors.json", {"players": {}})
    cal_fit = priors.get("calibration") or calibrate.load()
    notes = load_notes(players)
    for pid, n in notes.items():
        if n.get("skip"):
            players[pid]["skip"] = True
    tip = {g["id"]: g["tip"] for g in games}
    for p in players.values():
        mine = (log["players"].get(str(p["id"])) or {})
        played = [(g[0], g[1]) for gid, g in sorted(mine.items(), key=lambda kv: tip.get(int(kv[0]), 0))]
        model.value_player(p, priors, injuries, notes, calendar, cal_fit, played)
        p["proj_mpg"] = ((priors["players"].get(str(p["id"])) or {}).get("mpg")
                         or ((p["stats"].get("proj") or [None, None])[1]))
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
    up = advice.upside_view(players)        # pickups lean to upside (model.UPSIDE_RISK)
    ups = advice.upgrades(league, up, me)
    # an IR player who is back gets activated first (the week plan does it), which frees his slot
    stash_list = advice.stash(league, players, me, free_ir + len(advice.ir_returns(league, players, me)))
    stream_list = advice.streams(league, up, me, calendar, txs)
    hunch_list = advice.hunches(league, up, me, calendar, stream_list)
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
    plan = advice.week_plan(league, players, me, calendar, txs, reserve, pick=up)
    trades = advice.trade_ideas(league, players, me, calendar)
    try:
        league_moves = advice.league_moves(league, players, me, calendar, pull.league_moves(cookies))
    except Exception as err:
        problems.append("league moves failed (%s)" % type(err).__name__)
        league_moves = None
    if league_moves and league_moves["unmatched"]:
        problems.append("players moved but not yet on their new rosters: %s" % ", ".join(
            players[x]["name"] for x in league_moves["unmatched"]))

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
    match_next = advice.matchup(league, players, me, calendar, ahead=1)
    for mt in (match, match_next):   # the head-to-head: both lineups a day, their moves, the calls
        advice.h2h(league, players, me, calendar, mt, plan, league_moves, txs)
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

    own = own_rates(raw_pool, pull.stat_ids())
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
        "model": {"prior_games": model.PRIOR_GAMES, "dtd_play": model.DTD_PLAY, "upside_risk": model.UPSIDE_RISK, "priors_from": priors.get("board_generated")},
        "players": {str(pid): page_player(p, calendar) for pid, p in players.items()},
        "actions": acts, "lineup": lineup, "upgrades": ups, "stash": stash_list, "streams": stream_list,
        "hunches": hunch_list, "takes": load_takes(players, calendar.today, problems),
        "plan": plan, "trades": trades, "league_moves": league_moves,
        "matchup": match, "next_matchup": match_next, "waivers": waivers, "drop_order": advice.drop_order(league, players, me), "team_report": report,
        "injuries": advice.injury_board(league, players, me, opp, calendar), "transactions": txs,
        "news_fetched": fetched,
        "gamelog": page_gamelog(log, players, games, calendar, own, boxes),
        "usage": page_usage(own, boxes, players),
    }
    SITE.mkdir(exist_ok=True)
    write_json(SITE / "data.json", data)
    if verbose:
        summary(data)
    return data


def update_gamelog(log, raw_pool, games, now):
    """Add each pool player's finished games this season to the log: {player id: {game id: [fantasy
    points, minutes, FGA, FGM, 3PA, 3PM, FTA, FTM, TO]}}. A game counts once GAMELOG_SETTLE has passed since tip-off, so a game still
    being played never shows as a short one; one he sat out (no minutes) is removed."""
    tips = {g["id"]: g["tip"] for g in games}
    for entry in raw_pool:
        pl = entry["player"]
        mine = log["players"].setdefault(str(pl["id"]), {})
        for x in pl.get("stats") or []:
            if x.get("statSplitTypeId") != 5 or x.get("statSourceId") != 0 or x.get("seasonId") != SEASON:
                continue
            gid = int(x.get("externalId") or 0)
            if not tips.get(gid) or now - tips[gid] < GAMELOG_SETTLE:
                continue
            box = x.get("stats") or {}
            mins = box.get(MIN) or 0
            if mins > 0:
                mine[str(gid)] = [round(x.get("appliedTotal") or 0, 1), round(mins, 1)] + [box.get(k) or 0 for k in SHOTS + (TO,)]
            else:
                mine.pop(str(gid), None)
        if not mine:
            del log["players"][str(pl["id"])]
    return log


def update_boxes(games, now, problems):
    """Team totals for every finished game this season (pull.team_boxes), kept in cache/boxes.json; a
    finished game's box does not change, so each is fetched once, at most BOXES_MAX_NEW a refresh."""
    have = read_json(BOXES, {}) or {}
    if have.get("season") != SEASON:
        have = {"season": SEASON, "games": {}}
    want = [g["id"] for g in games if now - g["tip"] >= GAMELOG_SETTLE and str(g["id"]) not in have["games"]]
    if want:
        got = pull.team_boxes(want[-BOXES_MAX_NEW:])
        have["games"].update({str(k): v for k, v in got.items()})
        if len(got) < len(want):
            problems.append("team boxes: %d finished games still to fetch (usage rates leave them out)" % (len(want) - len(got)))
        write_json(BOXES, have)
    return have["games"]


def plays(fga, fta, to):
    """The plays a player (or team) used: shots, free-throw trips and turnovers."""
    return fga + FT_PLAY * fta + to


def min_per_play(boxes):
    """The league's team minutes per team play this season (LEAGUE_MIN_PER_PLAY until there are games):
    turns plays a minute into a usage rate, plays / minutes x this / 5."""
    m = sum(t[0] for b in boxes.values() for t in b)
    n = sum(plays(t[1], t[2], t[3]) for b in boxes.values() for t in b)
    return m / n if n > 1000 else LEAGUE_MIN_PER_PLAY


def page_gamelog(log, players, games, calendar, own=None, boxes=None):
    """The log for the page: {player id: [[days since opening night, fantasy points, minutes, shooting
    points, shooting points his attempts were worth at his make rates, other points his minutes were
    worth at his rate, FGA, 3PA, FTA, points, plays he used, plays his team used while he was on the
    floor], ...]} for players in the pool. The three "worth" columns need his own rates (own_rates) and
    are None without them; the team's plays need the game's team box (update_boxes) and are None
    without it. Usage rate = plays he used / plays his team used while he was on (his minutes x the
    team's plays a fifth of the team's minutes)."""
    own, boxes = own or {}, boxes or {}
    day = {g["id"]: (model.et_date(g["tip"]) - calendar.start).days for g in games}

    def row(pid, gid, g):
        out = [day[int(gid)], g[0], g[1]]
        if len(g) < 2 + len(SHOTS) + 1:
            return out
        fga, fgm, tpa, tpm, fta, ftm, to = g[2:9]
        rates = own.get(int(pid))
        sf = shooting_points(fga, fgm, tpa, tpm, fta, ftm)
        if rates:
            two = fga - tpa
            sx = two * expected_shot(rates["p2"], 2) + tpa * expected_shot(rates["p3"], 3) + fta * expected_shot(rates["pft"], 1)
            out += [round(sf, 1), round(sx, 1), round(g[1] * rates["other_pm"], 1)]
        else:
            out += [round(sf, 1), None, None]
        team = next((t for t in boxes.get(str(gid)) or [] if int(pid) in t[4]), None)
        on = round(g[1] * plays(*team[1:4]) / (team[0] / 5), 1) if team else None
        return out + [fga, tpa, fta, 2 * fgm + tpm + ftm, round(plays(fga, fta, to), 1), on]
    return {pid: sorted(row(pid, gid, g) for gid, g in gs.items() if int(gid) in day)
            for pid, gs in log["players"].items() if int(pid) in players}


def page_usage(own, boxes, players):
    """Each player's standards for the Stats tab's usage view: {"players": {player id: [usage rate, true
    shooting]}}, in percent, from the same record as his make
    rates (own_rates). Last season's usage is his plays a minute against the league's team pace this
    season (min_per_play), since his team's plays that season are not on hand: within a couple of points
    of the usual team-based number."""
    mpp = min_per_play(boxes)
    return {"players": {str(pid): [round(100 * r["plays_pm"] * mpp / 5, 1), round(100 * r["ts"], 1)]
                        for pid, r in own.items() if pid in players and r.get("ts") is not None}}


def shooting_points(fga, fgm, tpa, tpm, fta, ftm, scoring=DEFAULT_SCORING):
    """Fantasy points from shooting alone: points scored and the made/attempted shot lines."""
    w = lambda k: scoring.get(k, 0)
    pts = 2 * fgm + tpm + ftm
    return w(PTS) * pts + w(FGM) * fgm + w(FGA) * fga + w(TPM) * tpm + w(TPA) * tpa + w(FTM) * ftm + w(FTA) * fta


def expected_shot(rate, value):
    """What one attempt is worth on average at a make rate: a two (value 2), a three (3) or a free throw (1)."""
    if value == 1:
        return rate * shooting_points(0, 0, 0, 0, 1, 1) + (1 - rate) * shooting_points(0, 0, 0, 0, 1, 0)
    three = 1 if value == 3 else 0
    return rate * shooting_points(1, 1, three, three, 0, 0) + (1 - rate) * shooting_points(1, 0, three, 0, 0, 0)


def own_rates(raw_pool, ids, scoring=DEFAULT_SCORING):
    """Each player's own standards, for the Stats tab's shots view: {player id: {"p2", "p3", "pft": make
    rates, "other_pm": fantasy points a minute from everything but shooting, "plays_pm": plays used a
    minute, "ts": true shooting, None under OWN_TS_SHOTS shots}}. They come from last
    season's totals, with ESPN's projection for this one added when he played under OWN_MIN_MINUTES,
    and each make rate is pulled toward the league's by OWN_SHRINK attempts. The league scores like
    ESPN's default (DEFAULT_SCORING)."""
    shooting = {PTS, *SHOTS}
    totals = {}
    for entry in raw_pool:
        pl = entry["player"]
        split = {x.get("id"): x.get("stats") or {} for x in pl.get("stats") or []}
        last, proj = split.get(ids["last"]) or {}, split.get(ids["proj"]) or {}
        both = (last.get(MIN) or 0) < OWN_MIN_MINUTES
        box = {k: (last.get(k) or 0) + ((proj.get(k) or 0) if both else 0) for k in {MIN, TO, *SHOTS, *scoring}}
        if (box.get(MIN) or 0) > 0:
            totals[pl["id"]] = box
    lg = {k: sum(b.get(k, 0) for b in totals.values()) for k in SHOTS}
    league = {"2": (lg[FGM] - lg[TPM]) / max(1, lg[FGA] - lg[TPA]), "3": lg[TPM] / max(1, lg[TPA]),
              "ft": lg[FTM] / max(1, lg[FTA])}
    rate = lambda made, att, kind: (made + OWN_SHRINK[kind] * league[kind]) / (att + OWN_SHRINK[kind])
    out = {}
    for pid, b in totals.items():
        g = lambda k: b.get(k) or 0
        other = sum(w * g(k) for k, w in scoring.items() if k not in shooting)
        out[pid] = {"p2": rate(g(FGM) - g(TPM), g(FGA) - g(TPA), "2"), "p3": rate(g(TPM), g(TPA), "3"),
                    "pft": rate(g(FTM), g(FTA), "ft"), "other_pm": other / g(MIN),
                    "plays_pm": plays(g(FGA), g(FTA), g(TO)) / g(MIN),
                    "ts": g(PTS) / (2 * plays(g(FGA), g(FTA), 0)) if g(FGA) + g(FTA) >= OWN_TS_SHOTS else None}
    return out


def depth_roles(charts, players):
    """Each player's place on his team's depth chart, at the position where he is listed highest:
    p["depth"] = {"pos", "rank": 1 for the first string, "now": rank with teammates who are Out
    skipped, "out": names of the Out teammates listed ahead of him there}."""
    for team, chart in charts.items():
        for pos, ids in chart.items():
            out = []
            for rank, pid in enumerate(ids, 1):
                p = players.get(pid)
                if p is None:
                    continue
                if p["team"] == team and rank - len(out) < (p.get("depth") or {}).get("now", 99):
                    p["depth"] = {"pos": pos, "rank": rank, "now": rank - len(out), "out": list(out)}
                if p["status"] == "OUT":
                    out.append(p["name"])


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
    if data.get("takes"):
        print("\nanalyst takes:")
        for t in data["takes"]:
            print("  [%s until %s] %s: %s" % (t["stance"], t["until"], pl[str(t["pids"][0])]["name"], t["text"][:110]))
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
