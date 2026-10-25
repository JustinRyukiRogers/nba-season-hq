"""Does expected playing time (recent minutes, teammates out) beat Season HQ's current projection?

Replays four past seasons (2021-22, 2023-24, 2024-25, 2025-26) from their game logs
(research/fetch_gamelogs.py). Every CHECK_EVERY days, each player is projected the way the HQ
would have projected him that morning, and scored against what followed:

    week   points per game he played over the next 7 days (streams, the week plan, lineups)
    ros    points per game he played over the rest of the season (values, upgrades)

Models:
    current   seasonkit.model today: ESPN's preseason points per game blended with this season's
              average, PRIOR_GAMES 15 (FALLBACK_PRIOR_GAMES 6 and 0.92x ESPN for players outside
              the board's top 200)
    tuned     the same blend with the best PRIOR_GAMES found here (is any gain just a better k?)
    minutes   minutes x points per minute:
                minutes  each past game's minutes, less his share of the minutes teammates left
                         open that night ("adjusted"), averaged with recent games weighted more,
                         blended with ESPN's preseason minutes; then plus his share of minutes
                         open now (teammates Out today)
                rate     ESPN's preseason points per minute blended with this season's

A teammate is out for a game when he is on the team (his last game before it and his next game are
for that team) and does not play. The minutes he leaves open are his recent minutes. Out "now" means
he missed the team's last game and the first game of the window: the injury report's Out tag.

Parameters are fitted on three seasons and scored on the fourth, each season in turn. A pick test
also takes, at each checkpoint, the 3 best by each model from a waiver-level pool (preseason
projection under 24) and scores their next week.

Results (2026-10-25), error in points per game, mean of the four held-out seasons:
    week   current 8.76, tuned k 8.72, minutes 8.51-8.52; better in all four seasons
    ros    current 5.84, minutes 5.72; better in all four seasons
    picks  current 94.2 points a week per pick, minutes 96.6 (+2.5 +5.2 +1.5 +0.4 by season); with
           km 2 it was 97.9, but ros got worse
Who a player is covering for did not help: weighting his past games by how alike the teammates out
were (rho < 1) made picks worse in every form tried, and a share of open minutes (s) was fitted at
0-0.05 and was mixed by season. Shipped in seasonkit/model.py: s 0, rho 1, minutes weight km 4
(best for ros, within 0.01 for week), half-life 5 games, rate weight 400 minutes.

    python3 research/minutes_backtest.py
"""
import bisect
import itertools
import json
import math
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from draftkit.config import GP, HISTORY, MIN
from draftkit.espn import fantasy_points, stat_split
from draftkit.history import load_espn

SEASONS = [2022, 2024, 2025, 2026]
CHECK_EVERY = 3
WEEK = 7
BOARD_POOL = 200          # players the board valued (the rest use the HQ's fallback prior)
RELEVANT = 15.0           # scored players: preseason projection or average so far at least this
ROLE_GAMES = 5            # a teammate's minutes = his last ROLE_GAMES games
ABSENCE_MAX_DAYS = 45     # longer gaps are a player gone (waived, out for the season), not out
SIGNIFICANT = 15.0        # teammates whose absence counts as a different lineup context


def d(s):
    return date(*map(int, s.split("-")))


class Season:
    def __init__(self, year):
        self.year = year
        logs = json.loads((HISTORY / ("gamelogs_%d.json" % year)).read_text())
        espn = load_espn(year)
        self.games = {}                         # pid -> [(date, gid, team, min, fpts)]
        for pid, rows in logs.items():
            g = [(d(r[0]), r[1], r[2], r[3], r[4]) for r in rows if r[3] > 0]
            if g:
                self.games[pid] = g
        self.pos, self.prior = {}, {}
        ranked = []
        for pid, pl in espn.items():
            self.pos[str(pid)] = pl.get("defaultPositionId") or 3
            pr = stat_split(pl, "10%d" % year)
            if pr and pr.get(GP, 0) > 0:
                ranked.append((fantasy_points(pr), str(pid), fantasy_points(pr) / pr[GP], pr.get(MIN, 0) / pr[GP]))
        ranked.sort(reverse=True)
        for rank, (_, pid, fppg, mpg) in enumerate(ranked, 1):
            self.prior[pid] = (fppg, mpg, rank <= BOARD_POOL)
        # team schedules and who played each game
        self.team_games = defaultdict(set)
        self.played = defaultdict(dict)         # gid -> {pid: min}
        for pid, g in self.games.items():
            for day, gid, team, mins, _ in g:
                self.team_games[team].add((day, gid))
                self.played[gid][pid] = mins
        self.team_games = {t: sorted(v) for t, v in self.team_games.items()}
        self.dates = {pid: [x[0] for x in g] for pid, g in self.games.items()}
        self.start = min(x[0] for g in self.games.values() for x in g)
        self.end = max(x[0] for g in self.games.values() for x in g)
        self._vac = {}

    def role(self, pid, day):
        """A player's recent minutes before `day` (his ESPN projection when he has not played)."""
        g = self.games[pid]
        i = bisect.bisect_left(self.dates[pid], day)
        recent = [x[3] for x in g[max(0, i - ROLE_GAMES):i]]
        pm = self.prior.get(pid, (0, 0, 0))[1]
        if not recent:
            return pm
        return (pm * 2 + sum(recent)) / (2 + len(recent)) if pm else sum(recent) / len(recent)

    def on_team(self, pid, team, day):
        """On the team that day: his last game before it and his next game on or after it are for
        the team (or the season has not reached him yet / is over for him), within ABSENCE_MAX_DAYS."""
        g, ds = self.games[pid], self.dates[pid]
        i = bisect.bisect_left(ds, day)
        before = g[i - 1] if i else None
        after = g[i] if i < len(g) else None
        if before and before[2] != team:
            return False
        if after and after[2] != team:
            return False
        if before is None and after is None:
            return False
        gap_from = before[0] if before else self.start
        gap_to = after[0] if after else self.end
        return (gap_to - gap_from).days <= ABSENCE_MAX_DAYS

    def roster(self, team):
        r = self.__dict__.setdefault("_rosters", {})
        if team not in r:
            r[team] = [p for p, g in self.games.items() if any(x[2] == team for x in g)]
        return r[team]

    def vacated(self, team, day, gid):
        """Minutes left open in one team game, by the absent players' position (1..5), and the
        absent players who matter (SIGNIFICANT minutes)."""
        key = (team, gid)
        if key not in self._vac:
            v, who = [0.0] * 6, set()
            here = self.played[gid]
            for pid in self.roster(team):
                if pid not in here and self.on_team(pid, team, day):
                    m = self.role(pid, day)
                    v[self.pos.get(pid, 3)] += m
                    if m >= SIGNIFICANT:
                        who.add(pid)
            self._vac[key] = (v, frozenset(who))
        return self._vac[key]

    def out_now(self, team, day):
        """Minutes open now: teammates who missed the team's last game and its next one."""
        tg = self.team_games[team]
        i = bisect.bisect_left(tg, (day, ""))
        if i == 0 or i >= len(tg):
            return None
        last, nxt = tg[i - 1], tg[i]
        a, b = self.played[last[1]], self.played[nxt[1]]
        v, who = [0.0] * 6, set()
        for pid in self.roster(team):
            if pid not in a and pid not in b and self.on_team(pid, team, last[0]) and self.on_team(pid, team, nxt[0]):
                m = self.role(pid, day)
                v[self.pos.get(pid, 3)] += m
                if m >= SIGNIFICANT:
                    who.add(pid)
        return v, frozenset(who)


def rows_for(season):
    """One row per (checkpoint, player) with what the models need and what happened next."""
    out = []
    day = season.start + timedelta(days=CHECK_EVERY)
    while day < season.end - timedelta(days=WEEK):
        for pid, g in season.games.items():
            i = bisect.bisect_left(season.dates[pid], day)
            past, future = g[:i], g[i:]
            if not past or not future:
                continue
            team = past[-1][2]
            if future[0][2] != team:
                continue                            # traded: skip the switch
            week = [x for x in future if x[0] < day + timedelta(days=WEEK)]
            pfppg, pmpg, on_board = season.prior.get(pid, (None, None, False))
            avg = sum(x[4] for x in past) / len(past)
            if max(pfppg or 0, avg) < RELEVANT:
                continue
            now = season.out_now(team, day)
            if now is None:
                continue
            now, now_who = now
            vac = [season.vacated(team, x[0], x[1]) for x in past[-12:]]
            past_vac, past_who = [v for v, _ in vac], [w for _, w in vac]
            out.append({
                "y": season.year, "day": day, "pid": pid, "pos": season.pos.get(pid, 3),
                "pfppg": pfppg, "pmpg": pmpg, "board": on_board,
                "past": [(x[3], x[4]) for x in past], "past_vac": past_vac, "now": now,
                "ctx": [len(w ^ now_who) for w in past_who], "now_who": len(now_who),
                "week": sum(x[4] for x in week) / len(week) if week else None, "nweek": len(week),
                "week_min": sum(x[3] for x in week) / len(week) if week else None,
                "ros": sum(x[4] for x in future) / len(future), "nros": len(future),
            })
        day += timedelta(days=CHECK_EVERY)
    return out


# ---- models ----

def current(r, k=15.0, k_fallback=6.0):
    gp = len(r["past"])
    avg = sum(f for _, f in r["past"]) / gp
    if r["pfppg"] is not None and r["board"]:
        prior, kk = r["pfppg"], k
    elif r["pfppg"] is not None:
        prior, kk = 0.92 * r["pfppg"], k_fallback
    else:
        w = 0  # no ESPN projection: the HQ's last-season fallback is not in these logs; use his average
        return avg
    return prior + gp / (gp + kk) * (avg - prior)


def share(pos, v, adj):
    """Minutes open at his position, plus `adj` of those one position away."""
    return sum(v[q] * (1.0 if q == pos else adj if abs(q - pos) == 1 else 0.0) for q in range(1, 6))


def prepare(r):
    """Per-row pieces of the minutes model that do not depend on the fitted knobs, so the grid
    search is plain arithmetic."""
    past, vac = r["past"], r["past_vac"]
    tail = past[-len(vac):]
    pre = {"gp": len(vac), "now": {a: share(r["pos"], r["now"], a) for a in GRID["adj"]}}
    for hl, rho in itertools.product(GRID["hl"], GRID["rho"]):
        ws = [0.5 ** ((len(vac) - 1 - j) / hl) * rho ** c for j, c in enumerate(r["ctx"])]
        tw = sum(ws)
        pre[("m", hl, rho)] = sum(w * m for w, (m, _) in zip(ws, tail)) / tw
        pre[("f", hl, rho)] = sum(w * f for w, (_, f) in zip(ws, tail)) / tw
        for a in GRID["adj"]:
            pre[("v", hl, rho, a)] = sum(w * share(r["pos"], v, a) for w, v in zip(ws, vac)) / tw
    tot_min = sum(m for m, _ in past)
    tot_pts = sum(f for _, f in past)
    prate = (r["pfppg"] / r["pmpg"]) if r["pfppg"] and r["pmpg"] else (tot_pts / tot_min if tot_min else 0.8)
    if r["pfppg"] and not r["board"]:
        prate *= 0.92
    pre.update(tot_min=tot_min, tot_pts=tot_pts, prate=prate)
    r["pre"] = pre


def minutes_model(r, p):
    """-> (expected minutes, points per minute, points per game)."""
    s, adj, km, hl, kr, rho = p["s"], p["adj"], p["km"], p["hl"], p["kr"], p["rho"]
    q = r["pre"]
    recent = q[("m", hl, rho)] - s * q[("v", hl, rho, adj)]
    pm = r["pmpg"] if r["pmpg"] else recent
    gp = q["gp"]
    base = pm + gp / (gp + km) * (recent - pm)
    mins = max(0.0, min(40.0, base + s * q["now"][adj]))
    rate = (q["prate"] * kr + q["tot_pts"]) / (kr + q["tot_min"])
    return mins, rate, mins * rate


def score(rows, pred, target):
    """RMSE weighted by games in the target window."""
    se = w = 0.0
    for r in rows:
        y = r[target]
        if y is None:
            continue
        n = r["nweek"] if target == "week" else 1
        se += n * (pred(r) - y) ** 2
        w += n
    return math.sqrt(se / w) if w else float("nan")


GRID = {"s": [0.0, 0.05, 0.1, 0.2], "adj": [0.0, 0.5], "km": [1.0, 2.0, 4.0],
        "hl": [3.0, 5.0, 10.0], "kr": [250.0, 400.0, 700.0], "rho": [1.0, 0.5, 0.25, 0.1]}


def fit(rows, target):
    best = None
    for vals in itertools.product(*GRID.values()):
        p = dict(zip(GRID, vals))
        e = score(rows, lambda r: minutes_model(r, p)[2], target)
        if best is None or e < best[0]:
            best = (e, p)
    return best[1]


def fit_k(rows, target):
    return min([4.0, 8.0, 15.0, 25.0, 40.0], key=lambda k: score(rows, lambda r: current(r, k), target))


SHIPPED = {"s": 0.0, "adj": 0.0, "km": 4.0, "hl": 5.0, "kr": 400.0, "rho": 1.0}


def picks(rows, models, pool_below=24.0, n=3):
    """At each checkpoint, each model's n best from a waiver-level pool: their points over the next week."""
    by = defaultdict(list)
    for r in rows:
        if r["week"] is not None and (r["pfppg"] or 0) < pool_below:
            by[(r["y"], r["day"])].append(r)
    print("pick test: %d checkpoints, %d players in the pool on average" % (len(by), sum(map(len, by.values())) / len(by)))
    for name, f in models.items():
        season = defaultdict(list)
        for (y, _), pool in by.items():
            best = sorted(pool, key=lambda r: -f(r))[:n]
            season[y].append(sum(r["week"] * r["nweek"] for r in best) / n)
        allv = [v for vs in season.values() for v in vs]
        print("  %-8s %.1f points a week per pick; by season %s" % (
            name, sum(allv) / len(allv), " ".join("%d %.1f" % (y, sum(v) / len(v)) for y, v in sorted(season.items()))))


def main():
    rows = []
    for y in SEASONS:
        s = Season(y)
        rr = rows_for(s)
        print(y, len(rr), "player-checkpoints", file=sys.stderr)
        rows += rr
    for r in rows:
        prepare(r)
    # the grid search runs on a sample to keep it quick; scoring uses every row
    import random
    rng = random.Random(1)
    report = defaultdict(list)
    for target in ("week", "ros"):
        for test in SEASONS:
            train = [r for r in rows if r["y"] != test]
            sample = rng.sample(train, min(len(train), 25000))
            p = fit(sample, target)
            k = fit_k(sample, target)
            te = [r for r in rows if r["y"] == test]
            for label, sub in (("all", te), ("first 30 days", [r for r in te if len(r["past"]) <= 12]),
                               ("teammate out now", [r for r in te if sum(r["now"]) >= 15]),
                               ("lineup changed since his last game", [r for r in te if r["ctx"][-1] > 0])):
                report[(target, label)].append((test, len(sub), score(sub, current, target),
                                                score(sub, lambda r: current(r, k), target),
                                                score(sub, lambda r: minutes_model(r, p)[2], target), k, p))
    picks(rows, {"current": current, "minutes": lambda r: minutes_model(r, SHIPPED)[2]})
    for (target, label), res in report.items():
        print("\n%s, %s" % (target, label))
        print("  season   rows  current  tuned(k)  minutes   params")
        for test, n, a, b, c, k, p in res:
            print("  %d  %6d  %6.2f  %6.2f(%g)  %6.2f   %s" % (test, n, a, b, k, c, p))
        print("  mean            %6.2f  %6.2f      %6.2f" % tuple(sum(x[i] for x in res) / len(res) for i in (2, 3, 4)))
    with open(HISTORY / "minutes_backtest_rows.json", "w") as f:
        json.dump([{k: v for k, v in r.items() if k != "pre"} | {"day": str(r["day"])} for r in rows], f)


if __name__ == "__main__":
    main()
