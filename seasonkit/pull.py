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


def pool(cookies, limit=POOL_LIMIT):
    """Rostered players and the best-owned free agents, as this league sees them."""
    filt = {"players": {
        "filterStatus": {"value": ["FREEAGENT", "WAIVERS", "ONTEAM"]},
        "limit": limit,
        "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        # value 2 also returns each player's two most recent games
        "filterStatsForTopScoringPeriodIds": {"value": 2, "additionalValue": list(stat_ids().values())},
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
