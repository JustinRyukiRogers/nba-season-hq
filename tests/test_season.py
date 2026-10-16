"""Checks on the in-season tools: lineups, IR rules, absences, and the encryption round trip.
Uses made-up leagues and players in the shapes build.py produces, so it runs without ESPN.

    python3 tests/test_season.py
"""
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from seasonkit import advice, model

STARTING = ["PG", "SG", "SF", "PF", "C", "G", "F", "UT", "UT", "UT"]


def player(pid, status="ACTIVE", value=100.0, out=False, back=None, absent=0.0, owner=3, slots=("UT",), team="BOS",
           fppg=30.0, season_over=False, fa="ONTEAM"):
    return {"id": pid, "name": "P%d" % pid, "status": status, "owner": owner, "fa": fa, "team": team, "pos": ["SF"],
            "slots": list(slots), "droppable": True, "odds": {},
            "ros": {"value": value, "out": out, "back": back, "absent": absent, "season_over": season_over,
                    "fppg": fppg, "games": 50.0}}


def league_with(entries, ir_slots=2):
    return {"size": 12, "ir_slots": ir_slots, "starting": STARTING,
            "teams": {"3": {"roster": [{"id": pid, "slot": slot} for pid, slot in entries]}}}


def test_best_lineup_is_optimal():
    # greedy-by-points alone would put A at PG and leave B nowhere; the matching moves A to G
    cands = [(1, 30.0, ["PG", "G", "UT"]), (2, 25.0, ["PG"]), (3, 20.0, ["C"]), (4, 5.0, ["C"])]
    owner, total = model.best_lineup(cands, ["PG", "G", "C"])
    assert total == 75.0 and set(owner.values()) == {1, 2, 3}, (owner, total)
    owner, total = model.best_lineup([(1, 10.0, ["C"]), (2, 0.0, ["PG"])], ["PG", "C"])
    assert set(owner.values()) == {1}, "a player expected to score nothing is not started"


def test_comeback_and_blend():
    assert model.comeback(None) == 1.0 and model.comeback(5) == 1.0
    assert model.comeback(20) == model.SHORT_ABSENCE_FACTOR and model.comeback(90) == model.LONG_ABSENCE_FACTOR
    assert model.blend(30.0, 5.0, 0, None, 15) == (30.0, 5.0)
    fppg, sd = model.blend(30.0, 5.0, 15, 40.0, 15)
    assert abs(fppg - 35.0) < 1e-9 and sd < 5.0, "after PRIOR_GAMES games the two count equally"


def test_absence():
    today, end = date(2026, 11, 1), date(2027, 3, 28)
    out, back, over = model.absence({"status": "OUT"}, None, {}, today, end)
    assert out and back == today + timedelta(days=model.OUT_NO_DATE_DAYS) and not over
    out, back, over = model.absence({"status": "ACTIVE"}, {"status": "Out", "return": "2026-12-01"}, {}, today, end)
    assert out and back == date(2026, 12, 1), "the injury report's Out counts before ESPN's fantasy tag catches up"
    assert model.absence({"status": "OUT"}, {"fantasy": "OFS", "return": "2027-07-01"}, {}, today, end)[2]
    out, back, _ = model.absence({"status": "OUT"}, {"return": "2026-11-20"}, {"out_until": "2026-12-15"}, today, end)
    assert back == date(2026, 12, 15), "a season note's date beats ESPN's estimate"
    assert model.absence({"status": "DAY_TO_DAY"}, None, {}, today, end) == (False, None, False)


def test_game_odds():
    today = date(2026, 11, 1)
    games = [{"date": today + timedelta(days=d)} for d in (1, 3, 10, 30)]
    odds = model.game_odds({"status": "OUT"}, games, {"return": "2026-11-05"}, {}, today, date(2027, 3, 28), 0.8, 0.85, 0.9)
    assert [o[2] for o in odds] == [True, True, False, False], "games before the return are absences"
    assert abs(odds[2][1] - 0.85 * 0.9) < 1e-9 and odds[2][3] == 0.9
    odds = model.game_odds({"status": "DAY_TO_DAY"}, games, None, {}, today, date(2027, 3, 28), 0.8, 0.8, 1.0)
    assert odds[0][1] == model.DTD_PLAY and odds[1][1] == model.NEAR_TERM_PLAY and odds[3][1] == 0.8


def test_ir_rules():
    players = {1: player(1, "DAY_TO_DAY"), 2: player(2, value=10.0), 3: player(3), 4: player(4, "OUT", out=True, back="2026-12-01", absent=20.0)}
    acts, free = advice.ir_actions(league_with([(1, "IR"), (2, "BE"), (3, "UT"), (4, "BE")]), players, 3)
    kinds = {a["kind"]: a for a in acts}
    assert kinds["ir_blocked"]["player"] == 1 and kinds["ir_blocked"]["drop"] == 2, "a healthy player in IR blocks adds"
    assert kinds["to_ir"]["player"] == 4 and free == 1

    # IR full with Out players: the one due back soonest should give his slot up
    players = {1: player(1, "OUT", out=True, back="2026-11-03", absent=1.0), 2: player(2, "OUT", out=True, back="2027-01-01", absent=30.0),
               3: player(3, "OUT", out=True, back="2026-12-20", absent=25.0), 5: player(5)}
    acts, free = advice.ir_actions(league_with([(1, "IR"), (2, "IR"), (3, "BE"), (5, "UT")]), players, 3)
    assert free == 0 and acts[0]["kind"] == "ir_swap" and acts[0]["other"] == 1 and acts[0]["player"] == 3


def test_lineup_check():
    class Cal:
        pass
    cal = Cal()
    cal.today, cal.start, cal.end, cal.now = date(2026, 11, 2), date(2026, 10, 20), date(2027, 3, 28), 1000
    day = str(cal.today)
    cal.by_team = {"BOS": [{"date": cal.today, "tip": 5000, "opp": "NYK", "home": True}],
                   "MIA": [{"date": cal.today, "tip": 500, "opp": "ORL", "home": False}]}   # MIA tipped already
    players = {1: player(1, team="BOS"), 2: player(2, team="LAL"), 3: player(3, team="MIA"), 4: player(4, "OUT", team="BOS", out=True)}
    for pid in (1, 4):
        players[pid]["odds"] = {day: 0.93 if pid == 1 else 0.0}
    players[3]["odds"] = {day: 0.93}
    lg = league_with([(1, "BE"), (2, "UT"), (3, "BE"), (4, "UT")])
    res = advice.lineup_check(lg, players, 3, cal)
    assert res["start"] == [1], "the benched player with a game should start: %s" % res
    assert 3 not in res["start"], "a player whose game has tipped off is locked"
    assert any(w["player"] == 4 for w in res["warnings"]), "an Out starter with a game is flagged"


def test_urgency():
    class Cal:
        pass
    cal = Cal()
    cal.now, cal.today, cal.start = 1_000_000, date(2026, 10, 16), date(2026, 10, 20)
    lg = league_with([(1, "BE"), (2, "UT")])
    lg["acq"] = {"per_day": True}
    deadline = cal.now + 16 * 3600
    players = {1: player(1, value=120.0), 2: player(2, value=500.0),
               10: player(10, "OUT", value=130.0, out=True, back="2027-01-02", owner=0, fa="WAIVERS"),
               11: player(11, value=105.0, owner=0, fa="WAIVERS")}
    for pid, own, chg in ((10, 45.0, -0.7), (11, 30.0, 0.1)):
        players[pid].update(own=own, own_chg=chg, waiver_until=deadline)
    st = {"free_ir": 2, "drop": 1, "refill": 11, "list": [{"id": 10, "back": "2027-01-02", "games_after": 15.8}]}
    a = advice.stash_action(lg, players, 3, st, {"my_rank": 8}, cal)
    assert a["urgency"] == "soon" and a["by"] == deadline, a
    assert "7 teams claim before you" in a["why"]
    assert "Wed Oct 21" in a["steps"][-1], "before the opener the refill may have to wait for the second scoring day"

    waivers = {"all_on_waivers": True, "clears_label": "Sat", "my_rank": 8}
    acts = advice.actions(lg, players, 3, cal, [{"kind": "ir_blocked", "urgency": "now", "title": "x"}], {"state": "preseason"},
                          {"drop": 1, "list": []}, st, {"days": [], "list": []}, waivers)
    assert [x["urgency"] for x in acts] == ["now", "soon", "fyi"], "most urgent first"

    players[10].update(own=3.0, own_chg=0.0, fa="FREEAGENT", waiver_until=None)
    players[10]["ros"]["value"] = 40.0
    a = advice.stash_action(lg, players, 3, st, {"my_rank": 8}, cal)
    assert a["urgency"] == "optional" and a["by"] is None, "a small gain no one is chasing can wait"
    players[10]["ros"]["value"] = 10.0
    a = advice.stash_action(lg, players, 3, st, {"my_rank": 8}, cal)
    assert "only if ESPN lets you" in a["steps"][0], "a stash worth less than the swap it may force is not worth a drop"


def test_team_report():
    class Cal:
        pass
    cal = Cal()
    cal.start, cal.now = date(2026, 10, 20), 10_000
    cal.by_team = {"BOS": [{"date": cal.start + timedelta(days=i), "tip": 1000 * i} for i in range(12)]}   # 11 tipped off
    hot, cold, idle = player(1), player(2), player(3)
    hot["stats"], cold["stats"], idle["stats"] = {"season": [10, 36.0, 40.0]}, {"season": [4, 25.0, 20.0]}, {}
    for p, prior, weight in ((hot, 32.0, 0.6), (cold, 30.0, 0.79), (idle, 25.0, 1.0)):
        p["ros"].update(prior=prior, prior_from="draft board", prior_weight=weight)
    priors = {"players": {"1": {"fppg": 32.0, "mpg": 30.0, "espn_fppg": 38.0}, "2": {"fppg": 30.0, "mpg": 31.0, "espn_fppg": 28.0}}}
    rep = advice.team_report(league_with([(1, "UT"), (2, "BE"), (3, "BE")]), {1: hot, 2: cold, 3: idle}, 3, priors, cal)
    rows = {r["id"]: r for r in rep["rows"]}
    assert rows[1]["flags"] == ["hot", "min_up"], rows[1]
    assert rows[2]["flags"] == ["min_down"], "4 games is too few to call him cold, but 6 fewer minutes is a role change"
    assert rows[3]["gp"] == 0 and rows[3]["gap"] is None and rows[3]["team_gp"] == 11
    t = rep["team"]
    assert (t["games"], t["points"], t["projected"]) == (14, 480, 440), t
    chk = rep["league"]
    assert chk["players"] == 2 and abs(chk["board_miss"] - 120 / 14) < 0.06 and abs(chk["espn_miss"] - 52 / 14) < 0.06, chk


def test_recent_games_this_season_only():
    from draftkit.config import SEASON
    from seasonkit import build
    games = [{"seasonId": SEASON - 1, "statSourceId": 0, "statSplitTypeId": 5, "scoringPeriodId": 172, "appliedTotal": 21.0, "stats": {"40": 25}},
             {"seasonId": SEASON, "statSourceId": 0, "statSplitTypeId": 5, "scoringPeriodId": 2, "appliedTotal": 33.0, "stats": {"40": 31}}]
    entry = {"onTeamId": 3, "status": "ONTEAM", "player": {"id": 9, "fullName": "Test Player", "proTeamId": 2, "eligibleSlots": [], "stats": games}}
    assert build.normalize_player(entry, ["PG"], {})["recent"] == [[2, 33.0, 31]], "last season's final games are not this season's"


def test_encryption_round_trip():
    node = shutil.which("node")
    if not node:
        print("skip  test_encryption_round_trip (no node)")
        return
    tmp = Path(tempfile.mkdtemp())
    src, enc = tmp / "in.json", tmp / "data.enc"
    src.write_text(json.dumps({"hello": "world", "n": list(range(100))}))
    env = {"SITE_PASSPHRASE": "a-test-passphrase", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"}
    subprocess.run([node, str(ROOT / "seasonkit" / "crypt.mjs"), "encrypt", str(src), str(enc)], check=True, env=env,
                   capture_output=True)
    check = ("const fs=require('fs');const {hqDecrypt}=require(%r);const b=fs.readFileSync(%r);"
             "hqDecrypt(b.buffer.slice(b.byteOffset,b.byteOffset+b.length),'a-test-passphrase')"
             ".then(t=>{process.stdout.write(t)}).catch(e=>{console.error(e);process.exit(1)})"
             % (str(ROOT / "site" / "crypto.js"), str(enc)))
    out = subprocess.run([node, "-e", check], check=True, capture_output=True, text=True).stdout
    assert json.loads(out) == json.loads(src.read_text()), "the browser's decryption reads what the encrypter wrote"
    shutil.rmtree(tmp)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
