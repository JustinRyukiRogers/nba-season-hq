"""Rest-of-season values and daily lineups.

    per game   expected minutes x points per minute. Minutes: the board's preseason minutes
               (data/priors.json) blended with his minutes this season, recent games weighted most;
               MINUTES_PRIOR_GAMES sets how fast. Points a minute: the board's preseason rate blended
               with this season's, RATE_PRIOR_MINUTES sets how fast. Fitted and tested on four past
               seasons (research/minutes_backtest.py); players with no preseason minutes keep the
               plain blend of points per game, PRIOR_GAMES
    games      the player's remaining team games in the fantasy season, each with the chance he
               plays it: none before an Out player's expected return, less for a day-to-day
               player's next game, his preseason durability otherwise
    value      points above a waiver pickup over the rest of the season, the draft board's formula
               (playoff weeks count 1.5x, a bust can be dropped). Games missed while Out are not
               charged, since an IR slot can hold him.
"""
import math
import random
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from draftkit import calibrate
from draftkit import season as fantasy_calendar
from draftkit.board import player_value
from draftkit.config import (LONG_ABSENCE_DAYS, LONG_ABSENCE_FACTOR, PLAYOFF_WEEK_WEIGHT, SHORT_ABSENCE_DAYS,
                             SHORT_ABSENCE_FACTOR)

ET = ZoneInfo("America/New_York")

# Weight of the preseason projection, counted in games of this season's play: after PRIOR_GAMES
# games, actual and preseason count equally. Now only for players with no preseason minutes, and
# for the uncertainty (sigma) of every player.
PRIOR_GAMES = 15.0
# The minutes model, fitted on 2021-22 and 2023-26 game logs (research/minutes_backtest.py): his own
# minutes take over fast (after 4 games they count as much as the preseason guess), the last 5
# games count twice the 5 before them, and points a minute need about 400 minutes to count half.
MINUTES_PRIOR_GAMES = 4.0
MINUTES_HALF_LIFE = 5.0      # games
MINUTES_RECENT = 12          # games of minutes looked at
RATE_PRIOR_MINUTES = 400.0
FALLBACK_PRIOR_GAMES = 6.0   # players the board never valued: their own games take over quickly
DEEP_BENCH = 12.0            # their per-game prior when ESPN does not project them
FALLBACK_LAST_GAMES = 30.0   # games of last season's average that count as much as DEEP_BENCH
DTD_PLAY = 0.6               # chance a day-to-day player plays his next game
# A healthy player's next few games: the season-long share above averages in injuries still to
# come, but in the coming week a player on no injury report rarely sits (rest days, a new knock).
NEAR_TERM_DAYS, NEAR_TERM_PLAY = 7, 0.93
OUT_NO_DATE_DAYS = 7         # an Out player with no expected return is assumed back after a week
REPLACEMENT_FA_RANK = 6      # replacement level: the 6th-best healthy free agent (the board's pad)
FULL_SEASON = 82
OUT_TAGS = {"OUT", "SUSPENSION", "INJURY_RESERVE"}
CAL_FLAGS = ("old", "young", "new_team", "inj_return", "rookie")
LINEUP_SAMPLES = 200         # simulated days of who-plays when more players have games than slots


def et_date(epoch_s):
    return datetime.fromtimestamp(epoch_s, ET).date()


def parse_day(s):
    return date(*map(int, s[:10].split("-")))


class Calendar:
    """The fantasy calendar from the NBA schedule: matchup weeks, each team's remaining games."""

    def __init__(self, games, periods, final_period, regular_weeks, now=None):
        self.now = int(now or time.time())
        self.today = et_date(self.now)
        self.by_team = defaultdict(list)
        for g in games:
            day = et_date(g["tip"])
            for team, opp, home in ((g["home"], g["away"], True), (g["away"], g["home"], False)):
                if team:
                    self.by_team[team].append({"id": g["id"], "tip": g["tip"], "date": day, "opp": opp,
                                               "home": home, "tbd": g.get("tbd", False)})
        dates = {t: sorted({str(x["date"]) for x in gs}) for t, gs in self.by_team.items()}
        self.weeks = fantasy_calendar.matchup_weeks(dates, periods, final_period)
        self.start, self.end = self.weeks[0][0], self.weeks[-1][1]
        self.regular = regular_weeks
        self.cup = fantasy_calendar.cup_week(dates, self.weeks)
        self.scheduled = {t: len(gs) for t, gs in self.by_team.items()}
        self.by_day = {(t, g["date"]): g for t, gs in self.by_team.items() for g in gs}

    def game_on(self, team, day):
        return self.by_day.get((team, day))

    def week_of(self, day):
        for i, (lo, hi) in enumerate(self.weeks):
            if lo <= day <= hi:
                return i
        return None

    def current_week(self):
        """Index of the matchup week in play (the first one before the season starts)."""
        if self.today < self.start:
            return 0
        return self.week_of(self.today)

    def weight(self, week):
        return 1.0 if week < self.regular else PLAYOFF_WEEK_WEIGHT

    def remaining(self, team):
        """A team's games not yet tipped off, inside the fantasy season."""
        return [g for g in self.by_team.get(team, []) if g["tip"] > self.now and g["date"] <= self.end]

    def unscheduled(self, team):
        """NBA Cup knockout-round games every team plays but the schedule does not list yet; they
        fall in the Cup week. -> count still to come."""
        if self.cup is None or self.today > self.weeks[self.cup][1]:
            return 0
        return max(0, FULL_SEASON - self.scheduled.get(team, FULL_SEASON))

    def days_left_in_week(self, week=None):
        week = self.current_week() if week is None else week
        if week is None:
            return []
        lo, hi = self.weeks[week]
        d = max(lo, self.today)
        out = []
        while d <= hi:
            out.append(d)
            d += timedelta(days=1)
        return out

    def days_left(self):
        """Every day from today (or opening night) to the end of the fantasy season."""
        d, out = max(self.start, self.today), []
        while d <= self.end:
            out.append(d)
            d += timedelta(days=1)
        return out

    def first_tip(self, day):
        tips = [g["tip"] for gs in self.by_team.values() for g in gs if g["date"] == day]
        return min(tips) if tips else None


def durability(cal, espn_gp, flags, after_layoff=False):
    """Share of his team's games a player plays once available, the draft board's way: ESPN's
    games projection as calibrated on past seasons. After a layoff ESPN's total has usually netted
    the absence out already, so it is floored at 72 as on the board."""
    f = {k: (k in flags) for k in CAL_FLAGS}
    healthy = max(72.0, espn_gp) if after_layoff else espn_gp
    return max(0.15, min(0.92, calibrate.apply_games(cal, healthy, f) / 82.0))


def comeback(out_days):
    """Share of the games after an expected return that a player is really back for. Players on a
    long layoff come back later than announced more often than not (the draft board's factors).
    Held in an IR slot, a late return costs only those games: once back he plays at his usual rate,
    so this scales games, not the per-game bar."""
    if out_days is None:
        return 1.0
    return (LONG_ABSENCE_FACTOR if out_days >= LONG_ABSENCE_DAYS
            else SHORT_ABSENCE_FACTOR if out_days >= SHORT_ABSENCE_DAYS else 1.0)


def blend(prior, sigma, gp, actual, k):
    """Preseason projection and this season's average, weighted by games played."""
    if gp and actual is not None:
        w = gp / (gp + k)
        return prior + w * (actual - prior), sigma * math.sqrt(k / (k + gp))
    return prior, sigma


def minutes_rate(prior_mpg, prior_rate, games, season):
    """Expected minutes and points a minute. games: [(fantasy points, minutes)] of his games this
    season, oldest first (may miss early ones); season: [games, minutes, points per game] totals.
    -> (minutes, rate, minutes weight of his own play)."""
    gp = season[0] if season and season[0] else 0
    recent = games[-MINUTES_RECENT:]
    if recent:
        ws = [0.5 ** ((len(recent) - 1 - i) / MINUTES_HALF_LIFE) for i in range(len(recent))]
        played = sum(w * m for w, (_, m) in zip(ws, recent)) / sum(ws)
    elif gp:
        played = season[1]
    else:
        return prior_mpg, prior_rate, 0.0
    n = min(MINUTES_RECENT, max(gp, len(recent)))
    w = n / (n + MINUTES_PRIOR_GAMES)
    mins = prior_mpg + w * (played - prior_mpg)
    tot_min = gp * season[1] if gp else sum(m for _, m in games)
    tot_pts = gp * season[2] if gp else sum(f for f, _ in games)
    rate = (prior_rate * RATE_PRIOR_MINUTES + tot_pts) / (RATE_PRIOR_MINUTES + tot_min)
    return mins, rate, w


def absence(player, injury, note, today, season_end):
    """-> (out: bool, back: date or None, season_over: bool). An Out tag on either ESPN's fantasy
    player or the NBA injury report counts; the note's date beats ESPN's estimate."""
    tag = player.get("status")
    inj = injury or {}
    out = tag in OUT_TAGS or inj.get("status") == "Out" or inj.get("fantasy") in ("OUT", "OFS")
    if not out:
        return False, None, False
    back = note.get("out_until") or inj.get("return")
    back = parse_day(back) if back else today + timedelta(days=OUT_NO_DATE_DAYS)
    over = inj.get("fantasy") == "OFS" or back > season_end
    return True, back, over


def game_odds(player, games, injury, note, today, season_end, base_share, back_share, back_factor, first_day=None):
    """Chance he plays each remaining game. -> [(game, p, absent, games factor)]: absent marks games
    he misses while Out (an IR slot can cover those); the factor is the comeback discount on the
    games after an expected return."""
    out, back, over = absence(player, injury, note, today, season_end)
    first_day = first_day or today
    dtd = not out and player.get("status") == "DAY_TO_DAY"
    rate = (note.get("games_rate") or 1.0)
    odds = []
    for i, g in enumerate(games):
        if out and (over or g["date"] < back):
            odds.append((g, 0.0, True, 1.0))
        elif out:
            odds.append((g, back_share * rate * back_factor, False, back_factor))
        elif dtd and i == 0:
            odds.append((g, DTD_PLAY * rate, False, 1.0))
        elif (g["date"] - max(today, first_day)).days < NEAR_TERM_DAYS:
            odds.append((g, max(base_share, NEAR_TERM_PLAY) * rate, False, 1.0))
        else:
            odds.append((g, base_share * rate, False, 1.0))
    return odds


def best_lineup(cands, slots):
    """Highest-scoring legal lineup. cands: [(pid, points, eligible slot names)]; slots: the
    starting slot names, repeated per count. Players who can fill the slots form a matroid, so
    taking the best player that still fits, in order, is optimal. -> ({slot index: pid}, points)."""
    elig = {pid: set(e) for pid, _, e in cands}
    owner = {}

    def place(pid, seen):
        for i, s in enumerate(slots):
            if s in elig[pid] and i not in seen:
                seen.add(i)
                if i not in owner or place(owner[i], seen):
                    owner[i] = pid
                    return True
        return False

    total = 0.0
    for pid, pts, _ in sorted(cands, key=lambda c: -c[1]):
        if pts > 0 and place(pid, set()):
            total += pts
    return owner, total


def value_player(p, priors, injuries, notes, calendar, cal_fit, games_played=None):
    """Fill p["ros"] (and p["odds"], used for lineups) for one normalized player. games_played:
    [(fantasy points, minutes)] of his games this season, oldest first (the game log)."""
    prior = priors["players"].get(str(p["id"]))
    note = notes.get(p["id"]) or {}
    proj, season, last = p["stats"].get("proj"), p["stats"].get("season"), p["stats"].get("last")
    pmpg = None
    if prior:
        pfppg, sigma, k = prior["fppg"], prior["sigma"], PRIOR_GAMES
        espn_gp, flags, source = prior["espn_gp"], prior["flags"], "draft board"
        pmpg = prior.get("mpg")
    else:
        # the board never valued him: ESPN's projection scaled as the board scaled its fringe, else
        # last season's average pulled toward a deep-bench level by how many games it came from
        ratio = priors.get("fallback_ratio", 0.92)
        sigma, k, flags = priors.get("default_sigma", 5.4) * 1.2, FALLBACK_PRIOR_GAMES, []
        if proj and proj[0]:
            pfppg, espn_gp, source = ratio * proj[2], proj[0], "ESPN projection"
            pmpg = proj[1]
        elif last and last[0]:
            w = last[0] / (last[0] + FALLBACK_LAST_GAMES)
            pfppg, espn_gp, source = DEEP_BENCH + w * (ratio * last[2] - DEEP_BENCH), 50.0, "last season"
        else:
            pfppg, espn_gp, source = DEEP_BENCH, 40.0, "default"
    gp, actual = (season[0], season[2]) if season and season[0] else (0, None)
    fppg, sd = blend(pfppg, sigma, gp, actual, k)
    mins, rate, own_weight = None, None, (gp / (gp + k) if gp else 0.0)
    if pmpg and pmpg > 0:
        mins, rate, own_weight = minutes_rate(pmpg, pfppg / pmpg, games_played or [], season)
        fppg = mins * rate
    fppg += note.get("fppg_delta") or 0.0

    games = calendar.remaining(p["team"]) if p["team"] in calendar.by_team else []
    out, back, over = absence(p, injuries.get(p["id"]), note, calendar.today, calendar.end)
    out_days = (back - calendar.today).days if out and back else None
    base_share = durability(cal_fit, espn_gp, flags)
    back_share = durability(cal_fit, espn_gp, flags, after_layoff=True) if out else base_share
    factor = comeback(out_days) if out else 1.0
    odds = game_odds(p, games, injuries.get(p["id"]), note, calendar.today, calendar.end, base_share, back_share, factor,
                     first_day=calendar.start)

    weighted = played = playoff = absent = team_w = 0.0
    for g, prob, missed, gf in odds:
        week = calendar.week_of(g["date"])
        w = calendar.weight(week)
        if missed:
            absent += w
            continue
        team_w += w * gf
        weighted += prob * w
        played += prob
        if week >= calendar.regular:
            playoff += prob
    extra = calendar.unscheduled(p["team"]) if not over else 0
    if extra and calendar.cup is not None:
        w = calendar.weight(calendar.cup)
        cup_back = not out or calendar.weeks[calendar.cup][0] >= back
        if cup_back:
            gf = factor if out else 1.0
            share = back_share if out else base_share
            team_w += extra * w * gf
            weighted += extra * w * share * gf
            played += extra * share * gf
        else:
            absent += extra * w

    p["odds"] = {str(g["date"]): round(prob, 3) for g, prob, _, _ in odds}
    p["ros"] = {"fppg": round(fppg, 2), "sigma": round(sd, 2), "prior": round(pfppg, 2), "prior_from": source,
                "prior_weight": round(1 - own_weight, 2),
                "mpg": round(mins, 1) if mins is not None else None,
                "rate": round(rate, 3) if rate is not None else None,
                "games": round(played, 1), "playoff_games": round(playoff, 1), "team_games": len(games) + extra,
                "absent": round(absent, 1), "share": round(weighted / team_w, 3) if team_w else 0.0,
                "_team_w": team_w, "out": out, "back": str(back) if out and back else None, "season_over": over,
                "points": round(played * fppg)}
    return p


def finish_values(players, replacement):
    """Value every player against the replacement level, now that it is known."""
    for p in players:
        r = p["ros"]
        team_w = r.pop("_team_w")
        if team_w <= 0 or r["share"] < 0.05:
            r["value"], r["bar"] = 0.0, None
            continue
        value, bar = player_value(r["fppg"], r["sigma"], r["share"], team_w, replacement)
        r["value"], r["bar"] = round(value, 1), round(bar, 1)


def replacement_level(free_agents):
    """The ROS points per game of the REPLACEMENT_FA_RANK-th best free agent who is playing."""
    healthy = sorted((p["ros"]["fppg"] for p in free_agents if not p["ros"]["out"] and p["ros"]["games"] > 0),
                     reverse=True)
    if not healthy:
        return 20.0
    return healthy[min(len(healthy) - 1, REPLACEMENT_FA_RANK - 1)]


_draws = {}


def draws(day, pid, n):
    """n fixed uniform draws for one player on one day: the same on every refresh, and shared by
    every roster he is tried on, so comparing two rosters is not swamped by sampling noise."""
    key = (day, pid)
    u = _draws.get(key)
    if u is None or len(u) < n:
        rng = random.Random("%s:%s" % (day, pid))
        u = _draws[key] = [rng.random() for _ in range(n)]
    return u


def lineup_day(cands, slots, day, samples=LINEUP_SAMPLES):
    """One day's starts. cands: [(pid, chance he plays, points per game, eligible slots)].
    The manager sets the lineup knowing who plays (lineups lock per game, after the injury news),
    so the day is simulated: who plays is drawn, the best lineup of those is started. With
    samples=0, the lineup is picked on expected points instead (quicker, for screening).
    -> (expected points, {pid: expected starts})."""
    if not cands:
        return 0.0, {}
    owner, _ = best_lineup([(c[0], 1.0, c[3]) for c in cands], slots)
    if len(owner) == len(cands):              # all of them fit: whoever plays starts
        return sum(c[1] * c[2] for c in cands), {c[0]: c[1] for c in cands}
    if not samples:
        owner, pts = best_lineup([(c[0], c[1] * c[2], c[3]) for c in cands], slots)
        on = set(owner.values())
        return pts, {c[0]: c[1] for c in cands if c[0] in on}
    order = sorted(cands, key=lambda c: -c[2])
    u = {c[0]: draws(day, c[0], samples) for c in cands}
    total, starts = 0.0, defaultdict(float)
    for i in range(samples):
        owner, pts = best_lineup([(c[0], c[2], c[3]) for c in order if u[c[0]][i] < c[1]], slots)
        total += pts
        for pid in owner.values():
            starts[pid] += 1
    return total / samples, {pid: n / samples for pid, n in starts.items()}


def project_days(pids, days, players, slots, calendar, now=None, samples=LINEUP_SAMPLES, weighted=False):
    """A roster's best daily lineups over `days`, counting only games not yet tipped off.
    -> (points, [(day, points, expected starts)], {pid: [expected starts, points from starts]}).
    weighted: playoff-week points count PLAYOFF_WEEK_WEIGHT times, as in the values."""
    now = now or calendar.now
    total, per_day, by_player = 0.0, [], defaultdict(lambda: [0.0, 0.0])
    for day in days:
        cands = []
        for pid in pids:
            p = players.get(pid)
            if not p:
                continue
            g = calendar.game_on(p["team"], day)
            if not g or g["tip"] <= now:
                continue
            prob = p["odds"].get(str(day), 0.0)
            if prob > 0:
                cands.append((pid, prob, p["ros"]["fppg"], p["slots"]))
        pts, starts = lineup_day(cands, slots, str(day), samples)
        w = calendar.weight(calendar.week_of(day)) if weighted else 1.0
        total += w * pts
        fppg = {c[0]: c[2] for c in cands}
        for pid, s in starts.items():
            by_player[pid][0] += s
            by_player[pid][1] += s * fppg[pid]
        per_day.append((str(day), round(pts, 1), round(sum(starts.values()), 1)))
    return total, per_day, dict(by_player)
