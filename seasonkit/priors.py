"""Freeze the draft board's preseason numbers as the in-season starting point.

    python3 -m seasonkit priors      (run once, from a machine that has data/board.json)

data/board.json holds the board as it stood on draft night, research notes included. Only the
per-game projection and what is needed to rebuild a player's durability are kept, in
data/priors.json, which the hosted refresh reads. Injury absences are left out on purpose: in
season they come from ESPN's injury report, live, so carrying the preseason ones would count them
twice.
"""
import statistics
import time

from draftkit import calibrate
from draftkit.config import DATA, read_json, write_json

CAL_FLAGS = ("old", "young", "new_team", "inj_return", "rookie")


def freeze():
    board = read_json(DATA / "board.json")
    if board is None:
        raise SystemExit("data/board.json is missing; priors can only be frozen from the draft-night board")
    players = {}
    for p in board["players"]:
        players[str(p["id"])] = {
            "name": p["name"], "team": p["team"],
            "fppg": p["fppg"], "sigma": p["sigma"], "mpg": p["mpg"],
            "espn_gp": p["espn"]["gp"], "espn_fppg": p["espn"]["fppg"],
            "flags": [f for f in p["flags"] if f in CAL_FLAGS],
            "rank": p["rank"], "value": p["value"],
        }
    # players the board never valued get ESPN's projection scaled the way the board scaled the
    # fringe of its list (ranks 150 and down), where such players would have landed
    ratios = [p["fppg"] / p["espn"]["fppg"] for p in board["players"][150:] if p["espn"]["fppg"] > 0]
    out = {
        "_about": "Draft-night board (%s), frozen %s. fppg: the board's preseason per-game projection. "
                  "espn_gp and flags rebuild durability; absences come from the live injury report."
                  % (board["generated"], time.strftime("%Y-%m-%d")),
        "board_generated": board["generated"],
        "fallback_ratio": round(statistics.median(ratios), 3),
        "default_sigma": round(statistics.median(p["sigma"] for p in board["players"]), 2),
        # the one fitted piece the season needs (games played from ESPN's games projection); the
        # rest of data/calibration.json, the room model built on this league's drafts, stays local
        "calibration": {"games": calibrate.load()["games"]},
        "players": players,
    }
    write_json(DATA / "priors.json", out, indent=0)
    print("data/priors.json: %d players, fallback ratio %.3f" % (len(players), out["fallback_ratio"]))
    return out
