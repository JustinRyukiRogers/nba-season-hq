"""Does roster fit matter in this league? Positions, players from one NBA team, the playoff schedule.

Each roster is played out day by day over the real 2026-27 schedule: ten starting slots (PG SG SF
PF C G F and three UT) set fresh every day, each player missing games at his projected rate. A
game is lost when a player plays but every slot he can fill is already taken.

  1. Mock drafts with the kit making my picks: how much of a roster's points is lost that way.
  2. Lopsided rosters: those rosters with their cheapest players swapped for near-equal-value
     players of one kind (only C, only guards, one NBA team), to find where losses start.
  3. The schedule: how far apart teams' game counts are, and how far the board's order moves if
     the fantasy playoff weeks (the last three, from the league file) count for more than they do now.

Assumes lineups are set daily (ESPN's default), that missed games fall at random, and no streaming.
Run from the project root (takes a few minutes):

    python3 research/roster_fit.py
"""
import random
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from draftkit import engine, season
from draftkit.board import load as load_board, player_value
from draftkit.config import PLAYOFF_WEEK_WEIGHT, RAW, load_league, read_json

MOCKS = 25
REPS = 40                  # simulated seasons per roster
engine.SIMS = 120          # fewer look-ahead runs than on draft night, to keep this quick
STARTERS = ["PG", "SG", "SF", "PF", "C", "G", "F", "UT"]


class Calendar:
    """Game days of the fantasy season: which teams play, and how much the day counts."""

    def __init__(self, board, schedule, slots):
        weeks = [(season.parse(a), season.parse(b)) for a, b in board["weeks"]]
        self.weeks, self.n_regular = weeks, board["regular_weeks"]
        teams_on = defaultdict(set)
        for team, days in schedule.items():
            for d in map(season.parse, days):
                if weeks[0][0] <= d <= weeks[-1][1]:
                    teams_on[d].add(team)
        self.days = sorted(teams_on)
        self.teams_on = [teams_on[d] for d in self.days]
        week_of = [next(i for i, (a, b) in enumerate(weeks) if a <= d <= b) for d in self.days]
        self.weight = [PLAYOFF_WEEK_WEIGHT if w >= self.n_regular else 1.0 for w in week_of]
        self.slots = [s for s in STARTERS for _ in range(slots.get(s, 0))]


class Lineups:
    """Day-by-day lineups for any roster. A player's 'did he play' draws are the same in every
    roster he is on, so two rosters differ only by who is on them."""

    def __init__(self, cal, players):
        self.cal, self.P = cal, players
        self._plays, self._draws = {}, {}

    def plays(self, pid):
        if pid not in self._plays:
            p = self.P[pid]
            back = season.parse(p["out_until"]) if p.get("out_until") else None
            self._plays[pid] = [p["team"] in teams and (back is None or d >= back)
                                for d, teams in zip(self.cal.days, self.cal.teams_on)]
            rng = random.Random(pid)
            self._draws[pid] = [[rng.random() for _ in self.cal.days] for _ in range(REPS)]
        return self._plays[pid]

    def season(self, roster):
        """-> (points started, points played, {pid: games started}, {pid: games played}), averaged
        over REPS seasons. Each day the best players are fitted in first, moving others between
        slots when that makes room, which gives the best lineup the slots allow."""
        P, slots = self.P, self.cal.slots
        order = sorted(roster, key=lambda pid: -P[pid]["fppg"])
        plays = {pid: self.plays(pid) for pid in roster}
        started = played = 0.0
        g_started, g_played = Counter(), Counter()
        for rep in range(REPS):
            for day, w in enumerate(self.cal.weight):
                owner = {}

                def place(pid, seen):
                    for i, s in enumerate(slots):
                        if s in P[pid]["slots"] and i not in seen:
                            seen.add(i)
                            if i not in owner or place(owner[i], seen):
                                owner[i] = pid
                                return True
                    return False

                for pid in order:
                    if plays[pid][day] and self._draws[pid][rep][day] < P[pid]["availability"]:
                        played += w * P[pid]["fppg"]
                        g_played[pid] += 1
                        if place(pid, set()):
                            started += w * P[pid]["fppg"]
                            g_started[pid] += 1
        return (started / REPS, played / REPS, {k: v / REPS for k, v in g_started.items()},
                {k: v / REPS for k, v in g_played.items()})

    def lost(self, roster):
        """Share of the points a roster's players score that never reach a starting slot."""
        started, played, _, _ = self.season(roster)
        return 1 - started / played


def lopsided(roster, kind, target, pool, P):
    """Swap a roster's cheapest players for near-equal-value players of `kind` until `target` fit it."""
    r = list(roster)
    for pid in sorted(r, key=lambda pid: P[pid]["value"]):
        if sum(kind(P[x]) for x in r) >= target:
            break
        if kind(P[pid]):
            continue
        alts = [p for p in pool if kind(p) and p["id"] not in r]
        r[r.index(pid)] = min(alts, key=lambda p: abs(p["value"] - P[pid]["value"]))["id"]
    return r


def main():
    league = load_league()
    board = load_board()
    schedule = read_json(RAW / "schedule.json")
    P = {p["id"]: p for p in board["players"]}
    cal = Calendar(board, schedule, league["slots"])
    lineups = Lineups(cal, P)

    # 1. the kit's own rosters
    mine, others, use = [], [], defaultdict(list)
    for m in range(MOCKS):
        d = engine.Draft(league, board, seed=1000 + m)
        while not d.done():
            d.auto_pick()
        for t in set(d.pick_teams):
            roster = d.roster(t)
            if t != d.my_team:
                others.append(lineups.lost(roster))
                continue
            started, played, g_started, g_played = lineups.season(roster)
            mine.append((1 - started / played, started, roster))
            for rnd, pid in enumerate(roster, 1):
                use[rnd].append(g_started.get(pid, 0) / g_played[pid] if g_played.get(pid) else 1.0)
        print("  mock draft %d of %d done" % (m + 1, MOCKS), flush=True)
    weekly = st.mean(s for _, s, _ in mine) / (cal.n_regular + PLAYOFF_WEEK_WEIGHT * (len(cal.weeks) - cal.n_regular))
    print("\nPoints lost to the bench: a player played, but every slot he can fill was taken")
    print("   the kit's rosters (%d mock drafts)    %.1f%% (%.1f%% to %.1f%%), about %.0f points in a %.0f-point week" % (
        MOCKS, 100 * st.mean(x for x, _, _ in mine), 100 * min(x for x, _, _ in mine), 100 * max(x for x, _, _ in mine),
        weekly * st.mean(x for x, _, _ in mine), weekly))
    print("   rosters drafted with no eye on position  %.1f%% (%.1f%% to %.1f%%)  (the room model's)" % (
        100 * st.mean(others), 100 * min(others), 100 * max(others)))
    print("   share of each of my picks' games that make the lineup:")
    print("     " + "  ".join("R%d %.0f%%" % (r, 100 * st.mean(v)) for r, v in sorted(use.items())))

    # 2. lopsided rosters
    pool = sorted((p for p in board["players"] if not p.get("out_until")), key=lambda p: -p["value"])[:200]
    deepest = [t for t, _ in Counter(p["team"] for p in pool[:156]).most_common(2)]
    kinds = [("only C", lambda p: p["pos"] == ["C"]), ("only PG/SG", lambda p: set(p["pos"]) <= {"PG", "SG"})]
    kinds += [("from %s (deepest team)" % t, lambda p, t=t: p["team"] == t) for t in deepest]
    rosters = [r for _, _, r in mine[:15]]
    counts = range(3, 8)
    print("\nPoints lost to the bench on lopsided rosters, by how many of the 13 players are of that kind")
    print("   %-28s %s" % ("players who are...", "  ".join("%5d" % n for n in counts)))
    for label, kind in kinds:
        print("   %-28s %s" % (label, "  ".join("%4.1f%%" % (100 * st.mean(lineups.lost(lopsided(r, kind, n, pool, P)) for r in rosters))
                                                for n in counts)))

    # 3. the schedule
    games = {t: season.games_by_week(schedule, cal.weeks, t) for t in schedule}
    total = [sum(g) for g in games.values()]
    playoffs = {t: sum(g[cal.n_regular:]) for t, g in games.items()}
    print("\nSchedule: %d to %d team games in the fantasy season; %d to %d in the playoff weeks (most: %s; fewest: %s)" % (
        min(total), max(total), min(playoffs.values()), max(playoffs.values()),
        ", ".join(sorted(t for t, n in playoffs.items() if n == max(playoffs.values()))),
        ", ".join(sorted(t for t, n in playoffs.items() if n == min(playoffs.values())))))

    def ranks(weight):
        value = {}
        for p in board["players"]:
            by_week = season.games_by_week(schedule, cal.weeks, p["team"], p["out_until"]) if p["team"] in schedule else [0] * len(cal.weeks)
            weighted = sum((1.0 if i < cal.n_regular else weight) * g for i, g in enumerate(by_week))
            value[p["id"]] = player_value(p["fppg"], p["sigma"], p["availability"], weighted, board["replacement"])[0]
        return {pid: i for i, pid in enumerate(sorted(value, key=lambda k: -value[k]), 1)}

    now = ranks(PLAYOFF_WEEK_WEIGHT)
    top = [pid for pid, r in now.items() if r <= 156]
    for weight in (2.5, 4.0):
        new = ranks(weight)
        moved = [new[pid] - now[pid] for pid in top]
        print("   playoff weeks counted %.1fx instead of %.1fx: %d of the top 156 move 5+ spots (largest move %d)" % (
            weight, PLAYOFF_WEEK_WEIGHT, sum(abs(m) >= 5 for m in moved), max(abs(m) for m in moved)))


if __name__ == "__main__":
    main()
