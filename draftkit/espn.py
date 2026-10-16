"""ESPN fantasy API client. Player pools, schedule, news and injuries are public; a private
league's settings and live draft need the espn_s2 and SWID cookies (see config.load_env)."""
import gzip
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import (DEFAULT_SCORING, GP, LEAGUE_ID, SEASON, SLOT_NAMES, TEAMS, load_env, team_abbrev)

FANTASY = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba"
SITE = "https://site.api.espn.com/apis"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}


class EspnAuthError(Exception):
    """The league is private and the cookies are missing or expired."""


def http_get(url, params=None, headers=None, cookies=None, timeout=30, retries=2):
    """GET -> bytes. `params` values may be lists (repeated keys, which ESPN's `view` uses)."""
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    head = dict(UA)
    head.update(headers or {})
    if cookies:
        head["Cookie"] = "; ".join("%s=%s" % kv for kv in cookies.items())
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=head), timeout=timeout) as resp:
                body = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return body
        except urllib.error.HTTPError as err:
            if err.code in (401, 403):
                raise EspnAuthError("ESPN refused the request (HTTP %d)" % err.code)
            last = err
        except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
            last = err
        time.sleep(1 + attempt)
    raise last


def get_json(url, params=None, headers=None, cookies=None, timeout=30):
    head = {"Accept": "application/json"}
    head.update(headers or {})
    return json.loads(http_get(url, params, head, cookies, timeout))


# ---------- public data ----------

def fetch_players(year=SEASON, limit=700):
    """Player pool under ESPN's default points league, with the season projection (stat id 10YYYY)
    and season actuals (00YYYY) for this year and the one before. Raw ESPN JSON."""
    filt = {"players": {
        "limit": limit,
        "sortDraftRanks": {"sortPriority": 100, "sortAsc": True, "value": "STANDARD"},
        "filterStatsForTopScoringPeriodIds": {
            "value": 2,
            "additionalValue": ["00%d" % year, "10%d" % year, "00%d" % (year - 1), "10%d" % (year - 1)]},
    }}
    return get_json("%s/seasons/%d/segments/0/leaguedefaults/2" % (FANTASY, year),
                    {"view": "kona_player_info"}, {"x-fantasy-filter": json.dumps(filt)}, timeout=90)


def fetch_schedule(year=SEASON):
    """-> {team: [YYYY-MM-DD, ...]} of scheduled games (US Eastern dates)."""
    data = get_json("%s/seasons/%d" % (FANTASY, year), {"view": "proTeamSchedules_wl"}, timeout=60)
    games = {}
    for team in data["settings"]["proTeams"]:
        if team["id"] == 0:
            continue
        # epoch ms -> Eastern calendar date; tip-offs are never near 5am UTC, so a fixed offset is safe
        dates = {time.strftime("%Y-%m-%d", time.gmtime(g["date"] / 1000 - 5 * 3600))
                 for per_day in (team.get("proGamesByScoringPeriod") or {}).values() for g in per_day}
        games[TEAMS[team["id"]]] = sorted(dates)
    return games


def fetch_news(player_id, limit=14):
    """Latest items from a player's fantasy news feed -> [{date, kind, headline, story}].
    kind "note" is a short player update (with its fantasy take); "article" is a longer ESPN piece
    that mentions him, kept as a headline only."""
    data = get_json(SITE + "/fantasy/v3/games/fba/news/players", {"playerId": player_id, "limit": limit})
    out = []
    for item in (data.get("news") or {}).get("feed", []):
        kind = {"Rotowire": "note", "Story": "article"}.get(item.get("type"))
        if not kind:
            continue
        story = " ".join(re.sub(r"<[^>]+>", " ", item.get("story") or "").split()) if kind == "note" else ""
        out.append({"date": (item.get("published") or "")[:10], "kind": kind,
                    "headline": html.unescape((item.get("headline") or "").strip()), "story": html.unescape(story)})
    return out


def fetch_injuries():
    """League-wide injury report -> {player name: {status, type, detail, return_date, comment, date}}."""
    data = get_json(SITE + "/site/v2/sports/basketball/nba/injuries", timeout=60)
    out = {}
    for team in data.get("injuries", []):
        for item in team.get("injuries", []):
            det = item.get("details") or {}
            name = (item.get("athlete") or {}).get("displayName")
            if name:
                out[name] = {"status": item.get("status"),
                             "type": det.get("type"), "detail": det.get("detail"),
                             "return_date": det.get("returnDate"),
                             "comment": " ".join((item.get("shortComment") or "").split()),
                             "date": (item.get("date") or "")[:10]}
    return out


def fetch_depth_charts():
    """-> {team: {position: [player names in depth order]}} from ESPN's depth charts."""
    out = {}
    for team_id, abbrev in TEAMS.items():
        try:
            data = get_json("%s/site/v2/sports/basketball/nba/teams/%d/depthcharts" % (SITE, team_id))
        except Exception:
            continue
        for chart in data.get("depthchart", [])[:1]:
            out[abbrev] = {pos.upper(): [a.get("displayName") for a in slot.get("athletes", [])]
                           for pos, slot in (chart.get("positions") or {}).items()}
        time.sleep(0.1)
    return out


# ---------- the user's league (needs cookies when private) ----------

def league_url(year=SEASON, league_id=LEAGUE_ID):
    return "%s/seasons/%d/segments/0/leagues/%d" % (FANTASY, year, league_id)


def parse_league(raw, my_team_id=None):
    """Raw ESPN league JSON (views mSettings + mTeam + mDraftDetail) -> the normalized dict the
    rest of the kit reads from data/league.json."""
    s = raw["settings"]
    slots = {SLOT_NAMES[int(k)]: n for k, n in s["rosterSettings"]["lineupSlotCounts"].items()
             if n and int(k) in SLOT_NAMES}
    scoring = {str(i["statId"]): i["points"] for i in s["scoringSettings"].get("scoringItems", [])}
    picks = sorted((raw.get("draftDetail") or {}).get("picks", []), key=lambda p: p["overallPickNumber"])
    sched = s["scheduleSettings"]
    league = {
        "league_id": raw.get("id", LEAGUE_ID),
        "season": raw.get("seasonId", SEASON),
        "name": s["name"],
        "size": s["size"],
        "my_team_id": my_team_id,
        "scoring_type": s["scoringSettings"].get("scoringType"),
        "scoring": scoring or dict(DEFAULT_SCORING),
        "slots": slots,
        "draft": {
            "type": s["draftSettings"].get("type"),
            "date_ms": s["draftSettings"].get("date"),
            "seconds_per_pick": s["draftSettings"].get("timePerSelection"),
            "pick_order": s["draftSettings"].get("pickOrder") or [],
            # the full pick schedule as ESPN has it, which already reflects snake order and any traded picks
            "pick_teams": [p["teamId"] for p in picks],
            "order_confirmed": True,      # False when only my own seat was set by hand
            "my_slot_by_hand": None,
        },
        "schedule": {
            "regular_season_matchups": sched.get("matchupPeriodCount"),
            "matchup_periods": len(sched.get("matchupPeriods") or {}),
            "playoff_teams": sched.get("playoffTeamCount"),
            "final_scoring_period": (raw.get("status") or {}).get("finalScoringPeriod"),
        },
        "acquisitions_per_matchup": None,
        "teams": {str(t["id"]): {"name": (t.get("name") or "").strip(), "abbrev": t.get("abbrev")}
                  for t in raw.get("teams", [])},
    }
    acq = s.get("acquisitionSettings") or {}
    if acq.get("matchupAcquisitionLimit", -1) >= 0:
        per_day = acq.get("matchupLimitPerScoringPeriod")
        league["acquisitions_per_matchup"] = acq["matchupAcquisitionLimit"] * (7 if per_day else 1)
    check_league(league)
    return league


def check_league(league):
    """Refuse a league file that would quietly break the draft board."""
    size, order, teams = league["size"], league["draft"]["pick_order"], league["teams"]
    if len(teams) != size:
        raise ValueError("league has %d teams but size says %d" % (len(teams), size))
    if sorted(order) != sorted(int(t) for t in teams):
        raise ValueError("draft order does not list every team exactly once: %r" % (order,))
    starters = sum(n for slot, n in league["slots"].items() if slot not in ("BE", "IR"))
    if starters < 1:
        raise ValueError("no starting slots found")
    if league["draft"]["pick_teams"] and len(league["draft"]["pick_teams"]) % size:
        raise ValueError("pick schedule is not a whole number of rounds")


def fetch_league(cookies=None, year=SEASON, league_id=LEAGUE_ID, my_team_id=None):
    cookies = cookies or load_env()
    raw = get_json(league_url(year, league_id), {"view": ["mSettings", "mTeam", "mStatus", "mDraftDetail"]},
                   cookies=cookies)
    return parse_league(raw, my_team_id)


def fetch_draft_picks(cookies, year=SEASON, league_id=LEAGUE_ID):
    """Live draft state -> {"in_progress", "drafted", "picks": [{"overall", "team", "player_id"}]}
    with only the picks that have been made."""
    raw = get_json(league_url(year, league_id), {"view": "mDraftDetail"}, cookies=cookies, timeout=10)
    detail = raw.get("draftDetail") or {}
    made = [{"overall": p["overallPickNumber"], "team": p["teamId"], "player_id": p["playerId"]}
            for p in detail.get("picks", []) if p.get("playerId", -1) > 0]
    return {"in_progress": bool(detail.get("inProgress")), "drafted": bool(detail.get("drafted")),
            "picks": sorted(made, key=lambda p: p["overall"]),
            "pick_teams": [p["teamId"] for p in sorted(detail.get("picks", []), key=lambda p: p["overallPickNumber"])]}


# ---------- raw player JSON -> plain records ----------

def stat_split(player, split_id):
    """A player's stat block by ESPN split id ('102027' projection, '002026' actuals), or None."""
    for s in (player or {}).get("stats", []):
        if s["id"] == split_id and s.get("stats"):
            return s["stats"]
    return None


def fantasy_points(stats, scoring=None):
    scoring = scoring or DEFAULT_SCORING
    return sum(w * stats.get(k, 0) for k, w in scoring.items())


def per_game(stats, scoring=None):
    """-> (games, minutes per game, fantasy points per game) or None."""
    if not stats or stats.get(GP, 0) <= 0:
        return None
    g = stats[GP]
    return g, stats.get("40", 0) / g, fantasy_points(stats, scoring) / g


def player_team(player):
    return TEAMS.get(player.get("proTeamId"), "FA")


def player_slots(player, league_slots=None):
    """Lineup slots this player can fill, as names, limited to slots the league uses."""
    names = [SLOT_NAMES[s] for s in player.get("eligibleSlots", []) if s in SLOT_NAMES]
    keep = set(league_slots) if league_slots else None
    return [n for n in names if n not in ("BE", "IR") and (keep is None or n in keep)]


__all__ = ["EspnAuthError", "fetch_players", "fetch_schedule", "fetch_news", "fetch_injuries",
           "fetch_depth_charts", "fetch_league", "fetch_draft_picks", "parse_league", "check_league",
           "stat_split", "fantasy_points", "per_game", "player_team", "player_slots", "team_abbrev",
           "http_get", "get_json"]
