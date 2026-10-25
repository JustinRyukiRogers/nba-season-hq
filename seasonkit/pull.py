"""What the in-season refresh reads from ESPN.

Needs the login cookies (the league is private): the league itself (settings, rosters and daily
lineups, matchups, standings), the player pool with this league's ownership and waiver status, and
the transactions log. Public: the NBA injury report, the schedule with tip times, player news.
"""
import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

from draftkit import espn
from draftkit.config import SEASON, TEAMS

POOL_LIMIT = 550        # every rostered player (156) plus the free agents anyone would add
TRADED = 244            # activity-feed message type for a player moved in a trade


def _clean(text):
    return html.unescape(" ".join(re.sub(r"<[^>]+>", " ", text or "").split()))


def stat_ids(season=SEASON):
    """ESPN stat split ids: source (0 actual, 1 projected) + split (0 season, 1/2/3 last 7/15/30
    days) + season."""
    s = str(season)
    return {"season": "00" + s, "proj": "10" + s, "l7": "01" + s, "l15": "02" + s, "l30": "03" + s,
            "last": "00" + str(season - 1)}


def league(cookies):
    """Settings, status, teams with rosters and today's lineup slots, and the matchup schedule
    with live scores."""
    views = ["mSettings", "mStatus", "mTeam", "mRoster", "mMatchup", "mMatchupScore", "mSchedule"]
    return espn.get_json(espn.league_url(), {"view": views}, cookies=cookies, timeout=60)


def rosters(cookies, scoring_period):
    """Every team's roster as ESPN has it for a scoring day (mRoster): moves made after a day's rosters
    lock, IR moves and adds included, show here from the next day."""
    return espn.get_json(espn.league_url(), {"view": "mRoster", "scoringPeriodId": scoring_period},
                         cookies=cookies, timeout=30).get("teams") or []


def pool(cookies, limit=POOL_LIMIT, games=2):
    """Rostered players and the best-owned free agents, as this league sees them, each with his
    `games` most recent games (last season's too, when this one has fewer; about 500 bytes each)."""
    filt = {"players": {
        "filterStatus": {"value": ["FREEAGENT", "WAIVERS", "ONTEAM"]},
        "limit": limit,
        "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        "filterStatsForTopScoringPeriodIds": {"value": games, "additionalValue": list(stat_ids().values())},
    }}
    data = espn.get_json(espn.league_url(), {"view": "kona_player_info"},
                         {"x-fantasy-filter": json.dumps(filt)}, cookies=cookies, timeout=90)
    return data.get("players", [])


def transactions(cookies, periods):
    """Executed adds, drops and trades in the given scoring periods, newest first."""
    seen = {}
    for sp in periods:
        try:
            data = espn.get_json(espn.league_url(), {"view": "mTransactions2", "scoringPeriodId": sp},
                                 cookies=cookies, timeout=30)
        except espn.EspnAuthError:
            raise
        except Exception:
            continue
        for tx in data.get("transactions") or []:
            if tx.get("status") == "EXECUTED" and tx.get("type") in ("FREEAGENT", "WAIVER", "TRADE_ACCEPT"):
                seen[tx["id"]] = tx
    return sorted(seen.values(), key=lambda t: -(t.get("processDate") or t.get("proposedDate") or 0))


def league_trades(cookies, limit=200):
    """Every trade this season, from the league's activity feed (one request; the transactions view
    only covers a scoring period at a time) -> [{at, moves: [[player id, from team, to team], ...]}],
    newest first. Each traded player is one message (type TRADED) in the trade's topic."""
    filt = {"topics": {"filterType": {"value": ["ACTIVITY_TRANSACTIONS"]}, "limit": limit, "offset": 0,
                       "limitPerMessageSet": {"value": 25},
                       "sortMessageDate": {"sortPriority": 1, "sortAsc": False},
                       "filterIncludeMessageTypeIds": {"value": [TRADED]}}}
    data = espn.get_json(espn.league_url() + "/communication/", {"view": "kona_league_communication"},
                         headers={"x-fantasy-filter": json.dumps(filt)}, cookies=cookies, timeout=30)
    out = []
    for topic in data.get("topics") or []:
        moves = [[m["targetId"], m.get("from"), m.get("to")] for m in topic.get("messages") or []
                 if m.get("messageTypeId") == TRADED and m.get("targetId")]
        if moves:
            out.append({"at": (topic.get("date") or 0) // 1000, "moves": moves})
    return sorted(out, key=lambda t: -t["at"])


def depth_charts():
    """ESPN's depth chart for every team -> {team abbreviation: {position: [player ids, first string
    first]}}. A player can be listed at several positions."""
    def one(tid):
        data = espn.get_json(espn.SITE + "/site/v2/sports/basketball/nba/teams/%d/depthcharts" % tid, timeout=30)
        chart = (data.get("depthchart") or [{}])[0].get("positions") or {}
        return TEAMS[tid], {pos.upper(): [int(a["id"]) for a in v.get("athletes") or []] for pos, v in chart.items()}
    with ThreadPoolExecutor(8) as ex:
        return dict(ex.map(one, TEAMS))


def team_boxes(game_ids):
    """Each finished game's team totals, for usage rates -> {game id: [[minutes, FGA, FTA, turnovers,
    [player ids]], ...] one per team}. Minutes are the players' summed (240 plus 25 a period of overtime);
    turnovers are the players' (not team turnovers), as usage counts them. A game that fails is left out."""
    def one(gid):
        try:
            data = espn.get_json(espn.SITE + "/site/v2/sports/basketball/nba/summary", {"event": gid}, timeout=30)
        except Exception:
            return gid, None
        out = []
        for team in (data.get("boxscore") or {}).get("players") or []:
            block = (team.get("statistics") or [{}])[0]
            col = {k: i for i, k in enumerate(block.get("labels") or [])}
            if not {"MIN", "FG", "FT", "TO"} <= col.keys():
                return gid, None
            t, ids = [0.0, 0.0, 0.0, 0.0], []
            for a in block.get("athletes") or []:
                row = a.get("stats") or []
                try:
                    mins = float(row[col["MIN"]])
                    fga, fta = float(row[col["FG"]].split("-")[1]), float(row[col["FT"]].split("-")[1])
                    to = float(row[col["TO"]])
                except (ValueError, IndexError):
                    continue
                t = [t[0] + mins, t[1] + fga, t[2] + fta, t[3] + to]
                ids.append(int(a["athlete"]["id"]))
            out.append(t + [ids])
        return gid, out if len(out) == 2 and all(x[0] > 0 for x in out) else None
    with ThreadPoolExecutor(8) as ex:
        return {gid: box for gid, box in ex.map(one, game_ids) if box}


def injuries():
    """ESPN's NBA injury report -> {player id: {...}}, joined on ESPN's player id."""
    data = espn.get_json(espn.SITE + "/site/v2/sports/basketball/nba/injuries", timeout=60)
    out = {}
    for team in data.get("injuries", []):
        for item in team.get("injuries", []):
            ath = item.get("athlete") or {}
            pid = None
            for link in ath.get("links") or []:
                m = re.search(r"/id/(\d+)", link.get("href") or "")
                if m:
                    pid = int(m.group(1))
                    break
            if not pid:
                continue
            det = item.get("details") or {}
            out[pid] = {"name": ath.get("displayName"), "status": item.get("status"),
                        "fantasy": (det.get("fantasyStatus") or {}).get("abbreviation"),
                        "type": det.get("type"), "detail": det.get("detail"), "side": det.get("side"),
                        "return": (det.get("returnDate") or "")[:10] or None,
                        "short": _clean(item.get("shortComment")), "long": _clean(item.get("longComment")),
                        "updated": item.get("date")}
    return out


def schedule(season=SEASON):
    """Every scheduled NBA game -> [{id, tip (epoch seconds), home, away, tbd}], by tip time."""
    data = espn.get_json("%s/seasons/%d" % (espn.FANTASY, season), {"view": "proTeamSchedules_wl"}, timeout=60)
    games = {}
    for team in data["settings"]["proTeams"]:
        for per_day in (team.get("proGamesByScoringPeriod") or {}).values():
            for g in per_day:
                games[g["id"]] = {"id": g["id"], "tip": g["date"] // 1000,
                                  "home": TEAMS.get(g["homeProTeamId"]), "away": TEAMS.get(g["awayProTeamId"]),
                                  "tbd": bool(g.get("startTimeTBD"))}
    return sorted(games.values(), key=lambda g: g["tip"])


def news_for(player_id, limit=8):
    """A player's latest fantasy news -> [{at, kind, headline, story}]. kind "note" is a short
    update with its fantasy take; "article" is a longer ESPN piece that mentions him."""
    data = espn.get_json(espn.SITE + "/fantasy/v3/games/fba/news/players",
                         {"playerId": player_id, "limit": limit}, timeout=20)
    out = []
    for item in (data.get("news") or {}).get("feed", []):
        kind = {"Rotowire": "note", "Story": "article"}.get(item.get("type"))
        if kind:
            out.append({"at": item.get("published"), "kind": kind, "headline": _clean(item.get("headline")),
                        "story": _clean(item.get("story")) if kind == "note" else ""})
    return out


def refresh_news(cache, wanted, last_news, workers=6):
    """Bring the news cache up to date for `wanted` player ids, fetching only players with news
    newer than their cached copy. cache: {pid: {"fetched": epoch s, "items": [...]}};
    last_news: {pid: ESPN's lastNewsDate in ms}. -> number fetched."""
    stale = [pid for pid in wanted
             if str(pid) not in cache or (last_news.get(pid) or 0) / 1000 > cache[str(pid)]["fetched"] - 60]

    def one(pid):
        try:
            return pid, news_for(pid)
        except Exception:
            return pid, None

    with ThreadPoolExecutor(workers) as ex:
        for pid, items in ex.map(one, stale):
            if items is not None:
                cache[str(pid)] = {"fetched": int(time.time()), "items": items}
    return len(stale)
