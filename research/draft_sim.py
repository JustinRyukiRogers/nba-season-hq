"""Does the kit's pick logic out-draft this league?

Two tests on past seasons, scored on what the players actually did (points above a waiver pickup,
with missed games only partly made up: the board's own accounting, applied to real results).

  1. Simulated rooms: 12-team, 13-round snake drafts where the other eleven managers pick the way
     this league has historically picked relative to ESPN's list (the room model in calibrate.py).
     Many drafts, but the simulated managers reach at random, which flatters every strategy.
  2. Real drafts: the league's actual 2024-2026 drafts with one seat handed to the kit. Few
     drafts, but the opponents are the real ones.

Everything fitted is learned from the other seasons, never the one being replayed.
Run from the project root (takes a few minutes):

    python3 research/draft_sim.py
"""
import random
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from draftkit import calibrate, engine
from draftkit.board import BASELINE, player_value
from draftkit.config import BACKFILL, GP, load_league
from draftkit.espn import fantasy_points, player_slots, stat_split
from draftkit.history import SEASONS, build_rows, load_espn

REPS = 4
WEIGHTED_GAMES = 76.0        # a team's games in the fantasy season, playoff weeks weighted up
engine.SIMS = 80             # fewer look-ahead runs than on draft night, to keep this quick


def replacement_actual(season, rank=162):
    fppg = sorted((fantasy_points(a) / a[GP] for pl in load_espn(season).values()
                   if (a := stat_split(pl, "00%d" % season)) and a.get(GP, 0) >= 41), reverse=True)
    return fppg[min(rank, len(fppg)) - 1]


def realized(row, replacement):
    """What the player turned out to be worth, by the board's own accounting."""
    share = row["agp"] / 82.0
    per_team_game = share * (row["afppg"] or 0) + BACKFILL * replacement * (1 - share) - replacement * BASELINE
    return WEIGHTED_GAMES * max(0.0, per_team_game)      # a player below the bar gets dropped


def historical_board(season, rows, league, room):
    """The board as it would have been built before `season`, fitted on the other seasons."""
    train = [r for r in rows if r["y"] != season]
    cal = {"per_game": calibrate.fit_per_game(train), "games": calibrate.fit_games(train), "room": room}
    pool = [r for r in rows if r["y"] == season]
    players = []
    for r in pool:
        flags = calibrate.player_flags(r["age"], r["vgp"], r["changed"], r["rookie"])
        players.append({
            "id": r["id"], "name": r["name"], "team": r["team"], "market": r["prank"], "flags": [],
            "slots": player_slots(load_espn(season)[r["id"]], league["slots"]),
            "espn": {"adp": float(r["prank"])}, "out_until": None, "absent_games": 0,
            "fppg": calibrate.apply_per_game(cal, r["pfppg"], r["vfppg"], r["vgp"], r["v2fppg"], r["v2gp"], flags, r["crowd"]),
            "sigma": calibrate.apply_sigma(cal, flags),
            "availability": calibrate.apply_games(cal, r["pgp"], flags) / 82.0,
        })
    healthy = sorted((p["fppg"] for p in players), reverse=True)
    replacement = healthy[min(len(healthy), 162) - 1]
    for p in players:
        p["value"], p["bar"] = player_value(p["fppg"], p["sigma"], p["availability"], WEIGHTED_GAMES, replacement)
    for i, p in enumerate(sorted(players, key=lambda p: -p["value"]), 1):
        p["rank"] = i
    return {"players": players, "replacement": replacement, "generated": "", "weeks": [], "regular_weeks": 18}, cal


def run(season, board, cal, league, slot, strategy, seed, actual):
    lg = dict(league, my_team_id=slot + 1,
              draft=dict(league["draft"], pick_teams=[t + 1 for rnd in range(13)
                                                      for t in (range(12) if rnd % 2 == 0 else reversed(range(12)))]))
    d = engine.Draft(lg, board, cal, seed=seed)
    d.owned, d.fan, d.overdue = {}, {}, None        # no manager histories or ADP for a past season
    while not d.done():
        if d.team_at(d.next_overall()) != d.my_team:
            d.auto_pick()
            continue
        left = [pid for pid in d.market if pid not in d.taken()]
        roster = d.roster(d.my_team)
        if strategy == "list":
            pid = left[0]
        elif strategy == "value":
            pid = d.my_pick(left, roster)
        else:
            pid = d.recommend()["candidates"][0]["id"]
        d.add_pick(pid)
    totals = {}
    for k in d.picks:
        totals[k["team"]] = totals.get(k["team"], 0.0) + actual[k["player_id"]]
    mine = totals[d.my_team]
    others = [v for t, v in totals.items() if t != d.my_team]
    return mine / st.mean(others) - 1, 1 + sum(v > mine for v in others)


def replay_league(rows, league, room):
    """The stricter test: this league's real drafts, with one seat handed to the kit.

    The other eleven managers make the picks they really made (if we have already taken their
    player, they take the top name left on ESPN's list). Each seat is replayed in turn."""
    picks = calibrate.league_picks()
    strategies = [("list", "ESPN's list, top name left"), ("value", "our board, top value left"),
                  ("lookahead", "our board with look-ahead (what the app recommends)")]
    out = {key: {"edge": [], "vs_seat": [], "season": {}} for key, _ in strategies}
    for season in sorted(picks):
        if season not in SEASONS:
            continue
        board, cal = historical_board(season, rows, league, room)
        r_act = replacement_actual(season)
        espn = load_espn(season)

        def worth(pid):
            a = stat_split(espn.get(pid), "00%d" % season)
            games = a.get(GP, 0) if a else 0
            return realized({"agp": games, "afppg": fantasy_points(a) / games if games else 0.0}, r_act)

        real = picks[season]
        seat_of = lambda i: i % 12 if (i // 12) % 2 == 0 else 11 - i % 12
        real_totals = [sum(worth(pid) for i, (_, pid) in enumerate(real) if seat_of(i) == seat) for seat in range(12)]
        for seat in range(12):
            for key, _ in strategies:
                lg = dict(league, my_team_id=seat + 1,
                          draft=dict(league["draft"], pick_teams=[seat_of(i) + 1 for i in range(len(real))]))
                d = engine.Draft(lg, board, cal, seed=season + seat)
                d.owned, d.fan, d.overdue = {}, {}, None
                for i, (_, real_pid) in enumerate(real):
                    left = [pid for pid in d.market if pid not in d.taken()]
                    if seat_of(i) != seat:
                        pid = real_pid if real_pid not in d.taken() else left[0]
                    elif key == "list":
                        pid = left[0]
                    elif key == "value":
                        pid = d.my_pick(left, d.roster(d.my_team))
                    else:
                        pid = d.recommend()["candidates"][0]["id"]
                    d.add_pick(pid, name=(espn.get(pid) or {}).get("fullName"))
                totals = {}
                for k in d.picks:
                    totals[k["team"]] = totals.get(k["team"], 0.0) + worth(k["player_id"])
                mine = totals[seat + 1]
                others = [v for t, v in totals.items() if t != seat + 1]
                res = out[key]
                res["edge"].append(mine / st.mean(others) - 1)
                res["vs_seat"].append(mine / real_totals[seat] - 1 if real_totals[seat] else 0.0)
                res["season"].setdefault(season, []).append(mine / st.mean(others) - 1)
        print("  replayed the league's real %d draft from all 12 seats" % season, flush=True)
    n = len(out["list"]["edge"])
    print("\nThis league's real drafts, one seat at a time handed to each strategy (%d replays each)" % n)
    print("   %-52s %8s %14s   %s" % ("strategy", "vs avg", "vs real seat", "by season"))
    for key, label in strategies:
        res = out[key]
        print("   %-52s %+7.1f%% %+13.1f%%   %s" % (
            label, 100 * st.mean(res["edge"]), 100 * st.mean(res["vs_seat"]),
            "  ".join("%d %+.0f%%" % (s, 100 * st.mean(v)) for s, v in sorted(res["season"].items()))))
    print("   (vs avg: against the other eleven real teams. vs real seat: against what that seat's manager actually drafted.)")


def main():
    league = load_league()
    room = calibrate.load()["room"]
    rows = build_rows(pool_size=200)
    strategies = [("list", "ESPN's list, top name left"),
                  ("value", "our board, top value left"),
                  ("lookahead", "our board with look-ahead (what the app recommends)")]
    results = {key: {"edge": [], "finish": [], "season": {}} for key, _ in strategies}
    for season in SEASONS:
        board, cal = historical_board(season, rows, league, room)
        r_act = replacement_actual(season)
        actual = {r["id"]: realized(r, r_act) for r in rows if r["y"] == season}
        for slot in range(12):
            for rep in range(REPS):
                for key, _ in strategies:
                    edge, finish = run(season, board, cal, league, slot, key, 1000 * season + 10 * slot + rep, actual)
                    res = results[key]
                    res["edge"].append(edge)
                    res["finish"].append(finish)
                    res["season"].setdefault(season, []).append(edge)
        print("  replayed %d (replacement level that year: %.1f FPPG)" % (season, r_act), flush=True)
    n = len(results["list"]["edge"])
    print("\n12-team snake, 13 rounds; every draft slot x %d drafts x %d seasons = %d drafts per strategy" % (REPS, len(SEASONS), n))
    print("   %-52s %8s %7s %6s   %s" % ("strategy", "vs avg", "finish", "best", "by season"))
    for key, label in strategies:
        res = results[key]
        print("   %-52s %+7.1f%% %7.2f %5.0f%%   %s" % (
            label, 100 * st.mean(res["edge"]), st.mean(res["finish"]),
            100 * sum(f == 1 for f in res["finish"]) / n,
            "  ".join("%d %+.0f%%" % (s, 100 * st.mean(v)) for s, v in sorted(res["season"].items()))))
    print("   (an average drafter finishes 6.5 and is best 8% of the time)")
    print("   Caution: the simulated room reaches at random, so anyone who simply follows a sensible list beats it.")
    print("   Read the gaps BETWEEN strategies here, and the replay of real drafts below for the level.\n")
    replay_league(rows, league, room)


if __name__ == "__main__":
    main()
