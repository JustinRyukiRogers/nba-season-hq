"""Checks on the in-season tools: lineups, IR rules, absences, and the encryption round trip.
Uses made-up leagues and players in the shapes build.py produces, so it runs without ESPN.

    python3 tests/test_season.py
"""
import json
import shutil
import subprocess
import sys
import tempfile
import types
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from seasonkit import advice, model, pull

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


def test_minutes_rate():
    assert model.minutes_rate(25.0, 1.0, [], None) == (25.0, 1.0, 0.0), "no games: the preseason guess"
    games = [(15.0, 30.0)] * 4
    mins, rate, w = model.minutes_rate(40.0, 1.0, games, [4, 30.0, 15.0])
    assert abs(mins - 35.0) < 1e-9 and w == 0.5, "after MINUTES_PRIOR_GAMES games the two count equally"
    assert abs(rate - (400 + 60) / (400 + 120)) < 1e-9, "points a minute weigh 120 of his minutes against 400"
    mins, _, w = model.minutes_rate(20.0, 1.0, games, [4, 30.0, 15.0])
    assert abs(w - 4 / 6) < 1e-9 and abs(mins - (20 + 10 * 4 / 6)) < 1e-9, "a jump of MINUTES_BUMP or more is trusted faster"
    mins_new, _, _ = model.minutes_rate(20.0, 1.0, [(0, 10.0)] * 3 + [(0, 30.0)], [4, 15.0, 0.0])
    mins_old, _, _ = model.minutes_rate(20.0, 1.0, [(0, 30.0)] + [(0, 10.0)] * 3, [4, 15.0, 0.0])
    assert mins_new > mins_old, "recent games count more"


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
    # the official report's tag for a game beats ESPN's day-to-day, and a returning Out player's tag counts
    tags = {"2026-11-02": "Probable", "2026-11-04": "Doubtful"}
    odds = model.game_odds({"status": "DAY_TO_DAY", "tags": tags}, games, None, {}, today, date(2027, 3, 28), 0.8, 0.8, 1.0)
    assert odds[0][1] == model.TAG_PLAY["Probable"] and odds[1][1] == model.TAG_PLAY["Doubtful"]
    odds = model.game_odds({"status": "DAY_TO_DAY", "tags": {"2026-11-02": "Available"}}, games, None, {}, today,
                           date(2027, 3, 28), 0.8, 0.8, 1.0)
    assert odds[0][1] == model.NEAR_TERM_PLAY, "cleared on the official report while ESPN still says day-to-day"
    odds = model.game_odds({"status": "OUT", "tags": {"2026-11-02": "Questionable"}}, games, {"return": "2026-11-05"}, {},
                           today, date(2027, 3, 28), 0.8, 0.85, 0.9)
    assert odds[0][1] == 0.5 and not odds[0][2] and odds[1][2], "questionable to return: half a game, not an absence"


def test_parse_report():
    text = ("10/28/2026   07:00 (ET)   CHA@DET   Detroit Pistons     Thompson, Ausar      Probable         Injury/Illness\n"
            "                                                         McConnell, T.J.      Out              Injury/Illness\n"
            "                                                         Udeh, Jr., Ernest    Out              G League - Two-Way\n"
            "10/29/2026   07:30 (ET)   GSW@MIN   Golden State        Lendeborg, Yaxel     Questionable\n")
    r = pull.parse_report(text)
    assert r == {"Ausar Thompson": {"2026-10-28": "Probable"}, "T.J. McConnell": {"2026-10-28": "Out"},
                 "Ernest Udeh Jr.": {"2026-10-28": "Out"}, "Yaxel Lendeborg": {"2026-10-29": "Questionable"}}


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
    st = {"free_ir": 2, "drop": 1, "drop_is_spot": True, "refill": 11, "list": [{"id": 10, "back": "2027-01-02", "games_after": 15.8}]}
    a = advice.stash_action(lg, players, 3, st, {"my_rank": 8}, cal)
    assert a["urgency"] == "soon" and a["by"] == deadline, a
    assert "7 teams claim before you" in a["why"]
    assert "a day of streaming" in a["detail"], "dropping the streaming spot for a stash costs only a day of streaming"
    a = advice.stash_action(lg, players, 3, dict(st, free_ir=1), {"my_rank": 8}, cal)
    assert a["urgency"] == "optional", "filling the last IR slot needs a bigger gain to be urgent"

    waivers = {"all_on_waivers": True, "clears_label": "Sat", "my_rank": 8}
    acts = advice.actions(lg, players, 3, cal, [{"kind": "ir_blocked", "urgency": "now", "title": "x"}], {"state": "preseason"},
                          {"drop": 1, "list": []}, st, {"days": [], "list": []}, waivers)
    assert [x["urgency"] for x in acts] == ["now", "soon", "fyi"], "most urgent first"

    players[10].update(own=3.0, own_chg=0.0, fa="FREEAGENT", waiver_until=None)
    players[10]["ros"]["value"] = 40.0
    a = advice.stash_action(lg, players, 3, st, {"my_rank": 8}, cal)
    assert a["urgency"] == "optional" and a["by"] is None, "a small gain no one is chasing can wait"
    players[10]["ros"]["value"] = 10.0
    a = advice.stash_action(lg, players, 3, dict(st, drop_is_spot=False), {"my_rank": 8}, cal)
    assert a["net"] == 10.0 - (120.0 - 105.0) and "Not worth it" in a["steps"][0], "a real player dropped costs his value"

    lg["slots"] = {"BE": 1}
    st = advice.stash(lg, players, 3, 2)
    assert st["open"] and st["drop"] is None, "with an open roster spot the stash needs no drop"
    lg["starting"] = ["UT", "UT"]
    lg["teams"]["3"]["roster"].append({"id": 5, "slot": "BE"})
    players[5] = player(5, value=60.0)
    st = advice.stash(lg, players, 3, 2)
    assert not st["open"] and st["drop"] == 5 and st["drop_is_spot"], "a full roster drops the streaming spot"


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


def test_gamelog():
    from draftkit.config import SEASON
    from seasonkit import build
    now = 1_800_000_000
    games = [{"id": 1, "tip": now - 86400}, {"id": 2, "tip": now - 3600}, {"id": 3, "tip": now - 2 * 86400}]
    g = lambda gid, fp, mins, season=SEASON: {"seasonId": season, "statSourceId": 0, "statSplitTypeId": 5,
                                             "externalId": str(gid), "appliedTotal": fp, "stats": {"40": mins} if mins else None}
    pool = [{"player": {"id": 9, "stats": [g(1, 30.0, 28), g(2, 4.0, 6), g(3, 0.0, 0), g(4, 50.0, 30, SEASON - 1)]}}]
    log = {"season": SEASON, "period": 0, "players": {"9": {"3": [12.0, 20.0]}}}
    build.update_gamelog(log, pool, games, now)
    assert log["players"] == {"9": {"1": [30.0, 28, 0, 0, 0, 0, 0, 0, 0]}}, \
        "finished games only: not one still being played, one he sat out, or last season's: %s" % log
    cal = type("C", (), {"start": model.et_date(now - 2 * 86400)})()
    assert build.page_gamelog(log, {9: {}}, games, cal)["9"][0][:3] == [1, 30.0, 28]


def test_shot_xfp():
    from draftkit.config import FGA, FGM, FTA, FTM, MIN, REB, TPA, TPM
    from seasonkit import build
    # default scoring: a made two is +3, a made three +5, a made free throw +1, any miss -1
    assert build.shooting_points(10, 5, 4, 2, 4, 3) == 2 * 5 + 2 + 3 + 2 * 5 - 10 + 2 + 3 - 4
    assert abs(build.expected_shot(0.5, 2) - 1.0) < 1e-9 and abs(build.expected_shot(0.5, 3) - 2.0) < 1e-9
    ids = {"last": "002026", "proj": "102027"}
    vet = {MIN: 2000, FGA: 1000, FGM: 500, TPA: 400, TPM: 160, FTA: 200, FTM: 160, REB: 400}
    rookie = {MIN: 100, FGA: 50, FGM: 20, TPA: 20, TPM: 6, FTA: 10, FTM: 7, REB: 30}
    pool = [{"player": {"id": 1, "stats": [{"id": "002026", "stats": vet}, {"id": "102027", "stats": {MIN: 9999}}]}},
            {"player": {"id": 2, "stats": [{"id": "002026", "stats": rookie}, {"id": "102027", "stats": rookie}]}}]
    own = build.own_rates(pool, ids)
    assert abs(own[1]["other_pm"] - 0.2) < 1e-9, "a veteran's own rate is last season's, not the projection"
    assert own[1]["p3"] < 0.4 + 1e-9 and own[2]["p3"] > 0.3, "make rates are pulled toward the league's"
    log = {"players": {"1": {"7": [40.0, 30, 10, 6, 4, 2, 2, 2, 3]}}}
    cal = type("C", (), {"start": model.et_date(1_800_000_000)})()
    # his team: 240 minutes, 90 FGA, 25 FTA, 14 turnovers = 115 plays, 23 a fifth of its minutes
    boxes = {"7": [[240.0, 90.0, 25.0, 14.0, [1, 5]], [240.0, 80.0, 20.0, 10.0, [6]]]}
    row = build.page_gamelog(log, {1: {}}, [{"id": 7, "tip": 1_800_000_000}], cal, own, boxes)["1"][0]
    assert row[:3] == [0, 40.0, 30] and row[3] == build.shooting_points(10, 6, 4, 2, 2, 2) and row[6:9] == [10, 4, 2]
    assert abs(row[5] - 6.0) < 0.05, "30 minutes at 0.2 other points a minute"
    assert row[9] == 2 * 6 + 2 + 2 and row[10] == round(10 + 0.44 * 2 + 3, 1)
    assert abs(row[11] - 30 * 115 / 48) < 0.05, "his team's plays while he was on: 30 of its 48 minutes a fifth"
    bare = build.page_gamelog(log, {1: {}}, [{"id": 7, "tip": 1_800_000_000}], cal)["1"][0]
    assert bare[4] is None and bare[11] is None, "no own rates or team box: those columns are empty, not left out"
    assert abs(own[1]["plays_pm"] - (1000 + 0.44 * 200) / 2000) < 1e-9, "plays a minute: shots, free-throw trips, turnovers"


def test_takes():
    from seasonkit import build
    tmp = Path(tempfile.mkdtemp()) / "takes.json"
    tmp.write_text(json.dumps({"takes": [
        {"player": "Al One", "also": ["Bo Two", "Nobody"], "stance": "against", "text": "a", "written": "2026-10-25"},
        {"player": "Al One", "stance": "maybe", "text": "b", "written": "2026-10-20", "until": "2026-11-01"},
        {"player": "Bo Two", "stance": "for", "text": "stale", "written": "2026-10-01"},
        {"player": "Cy Gone", "stance": "for", "text": "c", "written": "2026-10-25"}]}))
    players = {1: {"name": "Al One"}, 2: {"name": "Bo Two"}}
    problems, old = [], build.TAKES
    build.TAKES = tmp
    try:
        takes = build.load_takes(players, date(2026, 10, 26), problems)
    finally:
        build.TAKES = old
    assert [t["text"] for t in takes] == ["a", "b"], "a take lapses after its end date (default a week)"
    assert takes[0]["pids"] == [1, 2] and takes[0]["until"] == "2026-11-01", "others it concerns come along"
    assert takes[1]["stance"] == "info", "an unknown stance is shown as a plain note"
    assert problems and "Cy Gone" in problems[0], "a take on no known player is reported, not dropped silently"


def test_depth_roles():
    from seasonkit import build
    mk = lambda pid, team, status="ACTIVE": {"id": pid, "name": "P%d" % pid, "team": team, "status": status}
    players = {1: mk(1, "ORL", "OUT"), 2: mk(2, "ORL"), 3: mk(3, "ORL"), 4: mk(4, "UTA")}
    charts = {"ORL": {"PF": [3, 2], "C": [1, 2, 3, 4]}}
    build.depth_roles(charts, players)
    assert players[2]["depth"] == {"pos": "C", "rank": 2, "now": 1, "out": ["P1"]}, "starts at C while the starter is out"
    assert players[3]["depth"]["pos"] == "PF" and players[3]["depth"]["now"] == 1, "his best listing wins"
    assert "depth" not in players[4], "a stale listing on another team is ignored"


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


class FakeCal:
    """A small fantasy calendar: one matchup week of `days` days from `start`, games given per team."""

    def __init__(self, start, days, games, now=0):
        self.start, self.today, self.now = start, start, now
        self.end = start + timedelta(days=days - 1)
        self.weeks, self.regular = [(start, self.end)], 1
        self.by_team = {t: [{"date": start + timedelta(days=i), "tip": 10_000 + 86400 * i, "opp": "XXX", "home": True}
                            for i in idx] for t, idx in games.items()}

    def game_on(self, team, day):
        return next((g for g in self.by_team.get(team, []) if g["date"] == day), None)

    def week_of(self, day):
        return 0

    def weight(self, week):
        return 1.0

    def current_week(self):
        return 0

    def days_left_in_week(self, week=None):
        return [self.start + timedelta(days=i) for i in range((self.end - self.start).days + 1)]

    def days_left(self):
        return self.days_left_in_week()

    def first_tip(self, day):
        tips = [g["tip"] for gs in self.by_team.values() for g in gs if g["date"] == day]
        return min(tips) if tips else None


def test_lineup_day():
    slots = ["PG", "C"]
    pts, starts = model.lineup_day([(1, 0.5, 30.0, ["PG"]), (2, 1.0, 20.0, ["C"])], slots, "2026-11-01")
    assert pts == 35.0 and starts == {1: 0.5, 2: 1.0}, "when everyone fits, whoever plays starts"
    # three point guards for one PG slot: the backup starts only when the first choice sits
    cands = [(1, 0.5, 30.0, ["PG"]), (2, 1.0, 20.0, ["PG"]), (3, 1.0, 10.0, ["PG"])]
    pts, starts = model.lineup_day(cands, ["PG"], "2026-11-01", samples=2000)
    assert abs(starts[1] - 0.5) < 0.04 and abs(starts[2] - 0.5) < 0.04 and 3 not in starts, starts
    assert abs(pts - 25.0) < 1.0, "a bench player fills in when the starter sits: %.1f" % pts
    ev, _ = model.lineup_day(cands, ["PG"], "2026-11-01", samples=0)
    assert ev == 20.0, "picked on expected points, the sure 20 beats a coin flip at 30, with no fill-in"
    again, _ = model.lineup_day(cands, ["PG"], "2026-11-01", samples=2000)
    assert again == pts, "the same draws on every run"


def test_week_plan_and_move_days():
    start = date(2026, 11, 2)
    cal = FakeCal(start, 4, {"BOS": [0, 1, 2, 3], "LAL": [1, 3], "MIA": [0, 2], "NYK": [1]}, now=10_000 + 60)   # day 0 has tipped
    players = {1: player(1, team="LAL", value=50.0, fppg=20.0), 2: player(2, team="MIA", value=300.0, fppg=40.0),
               9: player(9, team="BOS", owner=0, fa="FREEAGENT", value=45.0, fppg=30.0)}
    for p in players.values():
        p["odds"] = {str(g["date"]): 1.0 for g in cal.by_team[p["team"]]}
    lg = league_with([(1, "UT"), (2, "UT")])
    lg["starting"], lg["acq"] = ["UT", "UT"], {"per_day": True}
    lg["teams"]["3"]["adds"] = {}
    assert advice.move_days(lg, cal, 3, []) == [start + timedelta(days=i) for i in (1, 2, 3)], "today's first game has tipped"
    plan = advice.week_plan(lg, players, 3, cal, [])
    m = plan["moves"][0]
    assert (m["id"], m["drop"], m["day"]) == (9, 1, str(start + timedelta(days=1))), m
    assert m["spot"] and m["ros_cost"] == 2.5 and m["net"] == 47.5, "the streaming spot costs half the value given up: %s" % m
    players[1]["ros"]["value"] = 160.0
    held = advice.week_plan(lg, players, 3, cal, [])
    assert not any(x["drop"] == 1 and x["day"] == m["day"] for x in held["moves"]), \
        "a spot holder worth more for the season is not dropped for the week's +50"
    players[1]["ros"]["value"] = 50.0
    assert m["gain"] == 3 * 30.0 - 2 * 20.0, "judged on the rest of the week from the day it counts: %s" % m
    players[8] = dict(player(8, team="NYK", owner=0, fa="FREEAGENT", value=200.0, fppg=25.0), odds={str(start + timedelta(days=1)): 1.0})
    players[9]["owner"] = 5
    later = advice.week_plan(lg, players, 3, cal, [])
    assert not later["moves"], "worth more for the season, but he plays once and the drop plays twice: it waits: %s" % later["moves"]
    players[9]["owner"] = 0
    del players[8]
    day1 = plan["lineups"][1]
    assert {r[1] for r in day1["slots"]} == {9, None} and day1["idle"] == [2], day1
    assert plan["points"] == plan["points_now"] + 3 * 30.0 - 2 * 20.0
    lg["slots"] = {"BE": 1}
    open_plan = advice.week_plan(lg, players, 3, cal, [])
    assert open_plan["moves"][0]["drop"] is None and open_plan["open"] == 1, \
        "an empty roster spot is the streaming spot: fill it before dropping anyone"
    lg["slots"] = {}
    plan = advice.week_plan(lg, players, 3, cal, [], stash={"id": 7, "drop": 1})
    first, second = plan["moves"][:2]
    assert first["kind"] == "stash" and first["day"] == str(start + timedelta(days=1)), "the stash takes the first add"
    assert (second["id"], second["drop"], second["day"]) == (9, None, str(start + timedelta(days=2))), \
        "streaming starts the next day, in the spot the stash freed: %s" % second
    assert all(7 not in {r[1] for r in day["slots"]} for day in plan["lineups"]), "a stashed player sits in IR"
    players[1]["ros"]["out"] = True
    assert advice.stream_spots(players, [1, 2]) == {2}, "an Out player goes to IR, not the streaming spot"
    cal.now = 0
    txs = [{"team": 3, "adds": [5], "at": int(datetime(2026, 11, 2, 9, tzinfo=model.ET).timestamp())}]
    assert advice.move_days(lg, cal, 3, txs)[0] == start + timedelta(days=1), "today's one add is used up"


def test_tomorrows_rosters():
    from seasonkit import build
    lg = {"teams": {"3": {"id": 3, "roster": [{"id": 1, "slot": "UT"}, {"id": 2, "slot": "BE"}]}}}
    nxt = [{"id": 3, "roster": {"entries": [{"playerId": 1, "lineupSlotId": 13}, {"playerId": 2, "lineupSlotId": 11},
                                            {"playerId": 5, "lineupSlotId": 12}]}}]
    build.roster_from_tomorrow(lg, nxt)
    t = lg["teams"]["3"]
    assert [(e["id"], e["slot"], e["today"]) for e in t["roster"]] == [(1, "IR", "UT"), (2, "BE", "BE"), (5, "BE", None)], \
        "an IR move or add made after today's lock counts from tomorrow; tomorrow's lineup slots are not: %s" % t["roster"]
    assert [e["slot"] for e in advice.roster(lg, 3, today=True)] == ["UT", "BE"], "today's lineup check keeps today's"
    assert [e["id"] for e in advice.roster(lg, 3, include_ir=False)] == [2, 5]


def test_moves_roll_into_next_week():
    start = date(2026, 11, 2)
    cal = FakeCal(start, 1, {"BOS": [0]}, now=10_000 + 60)        # the week's last day, after its first tip
    cal.weeks = [(start, start), (start + timedelta(days=1), start + timedelta(days=7))]
    lg = league_with([])
    lg["acq"] = {"per_day": True}
    assert advice.move_days(lg, cal, 3, []) == [start + timedelta(days=i) for i in range(1, 8)], \
        "after the last day's lock, an add counts from next week's first day"

def test_trade_helpers():
    players = {i: player(i, value=v) for i, v in ((1, 10.0), (2, 50.0), (3, 80.0), (8, 5.0))}
    val = lambda pid: players[pid]["ros"]["value"]
    assert advice.after_trade([1, 2, 3], [3], [10, 11], players | {10: player(10), 11: player(11)}, val, 8) == [2, 10, 11], \
        "two for one: the lowest-valued player you keep is dropped"
    assert advice.after_trade([1, 2, 3], [2, 3], [10], players | {10: player(10)}, val, 8) == [1, 10, 8], \
        "one for two: the best free agent fills the open spot"
    star, mid = player(20, fppg=55.0), player(21, fppg=34.0)
    star["stats"], mid["stats"] = {"proj": [70, 35, 60.0]}, {}
    star["ros"]["games"] = mid["ros"]["games"] = 60.0
    assert advice.espn_value(star, 25.0) == 35.0 * 60 and advice.espn_value(mid, 25.0) == 9.0 * 60, \
        "ESPN's per-game number when it has one, above a waiver pickup"
    assert advice.espn_value(star, 25.0) > 2 * advice.espn_value(mid, 25.0), "two middling players are not a star"


def test_trade_pitch():
    hot = player(1, fppg=30.0)
    hot["stats"] = {"season": [8, 34, 40.0], "last": [70, 34, 30.0]}
    assert advice.gut_per_game(hot) == 35.0, "this season's scoring shrunk halfway to last season's after 8 games"
    rookie = player(2)
    rookie["stats"] = {"season": [0, 0, 0], "proj": [70, 30, 22.0]}
    assert advice.gut_per_game(rookie) == 22.0, "no record: where he was drafted (ESPN's preseason number)"
    cold = player(3, fppg=40.0)
    cold["stats"] = {"season": [4, 30, 28.0]}
    hurt = player(4, fppg=32.0, out=True, back="2026-12-20")
    hurt["pos"], hot["pos"] = ["SG"], ["SG", "SF"]
    players = {1: hot, 3: cold, 4: hurt}
    cal = type("C", (), {"today": date(2026, 11, 1)})()
    kinds = [a["kind"] for a in advice.trade_args(players, [3, 4], [1], [3], 40.0, cal)]
    assert kinds == ["sell_high", "buy_low", "cover"], "hot player sent, cold one got, their SG out and yours plays SG: %s" % kinds
    assert [a["kind"] for a in advice.trade_args(players, [3], [1], [3], 0.0, cal)] == ["sell_high", "buy_low"], \
        "no lineup gain for them now: no cover argument"



def test_league_moves():
    start = date(2026, 11, 2)
    cal = FakeCal(start, 4, {"BOS": [0, 1, 2, 3], "MIA": [0, 1, 2, 3]}, now=0)
    players = {1: player(1, owner=7, fppg=20.0, value=40.0), 2: player(2, owner=5, team="MIA", fppg=40.0, value=300.0),
               6: player(6, owner=5, fppg=10.0, value=5.0), 8: player(8, owner=7, fppg=15.0, value=20.0),
               9: player(9, owner=0, fa="FREEAGENT", fppg=12.0, value=0.0)}
    for q in players.values():
        q["odds"] = {str(g["date"]): 1.0 for g in cal.by_team[q["team"]]}
        q["stats"] = {}
    lg = {"starting": ["UT"], "matchups": [{"period": 1, "home": 5, "away": 3}],
          "teams": {"3": {"roster": []}, "5": {"roster": [{"id": 2, "slot": "UT"}, {"id": 6, "slot": "BE"}]},
                    "7": {"roster": [{"id": 1, "slot": "UT"}, {"id": 8, "slot": "BE"}]}}}
    feed = [{"at": 0, "kind": "trade", "moves": [[2, 7, 5], [1, 5, None]]}]   # the feed left out who got player 1
    out = advice.league_moves(lg, players, 3, cal, feed)
    side = {x["team"]: x for x in out["list"][0]["sides"]}
    assert side[5]["got"] == [2] and side[5]["gave"] == [1] and side[7]["got"] == [1], "receiver filled from the other side"
    assert side[5]["gain"] == 4 * (40 - 20) and side[7]["gain"] == 4 * (20 - 40), \
        "the lineups now against the same rosters with the trade undone: %s" % side
    assert side[5]["value"] == 260 and side[5]["meets_me"] == 1 and side[7]["meets_me"] is None
    assert out["unmatched"] == [], "both players are on their new rosters"
    lg["teams"]["5"]["roster"] = [{"id": 6, "slot": "BE"}]
    players[2]["owner"] = 7
    assert advice.league_moves(lg, players, 3, cal, feed)["unmatched"] == [2], \
        "a fresh trade whose player is not on his new roster is flagged"
    lg["teams"]["5"]["roster"] = [{"id": 6, "slot": "BE"}, {"id": 9, "slot": "BE"}]
    players[9]["owner"] = 5
    add = advice.league_moves(lg, players, 3, cal, [{"at": 0, "kind": "add", "moves": [[9, 0, 5], [2, 5, 0]]}])
    side = add["list"][0]["sides"]
    assert [x["team"] for x in side] == [5] and side[0]["got"] == [9] and side[0]["gave"] == [2], "free agency is no side: %s" % side
    assert add["unmatched"] == [], "a dropped player may be anywhere"

def test_h2h_calls():
    players = {1: {"name": "A", "ros": {"fppg": 30.0}}, 2: {"name": "B", "ros": {"fppg": 20.0}}}
    lu = lambda date, starts, crunch=0.0: {"date": date, "starts": starts, "crunch": crunch}
    h = {"reach": 20.0, "threat": [["2026-10-27", 2, 20.0]], "adds": {"me": 0, "them": 1},
         "views": {"me": {"as_is": {"open": [1.0, 6.0], "form": 1000.0, "lineups": [lu("2026-10-26", 9.0), lu("2026-10-27", 4.0, 12.0)]}},
                   "them": {"as_is": {"open": [1.0, 2.0], "form": 1000.0}}},
         "doubt": {"them": [{"id": 1, "games": 3, "odds": 0.5, "at_stake": 45.0}]}}
    cal = types.SimpleNamespace(now=0)
    plan = {"moves": [{"kind": "add", "id": 2, "day": "2026-10-27", "by": 3600}]}
    titles = lambda calls: [c["title"] for c in calls]
    calls = advice.h2h_calls(players, {"ahead": 0}, h, cal, 1000.0, 1000.0, 1050.0, plan)
    assert calls[0]["title"].startswith("Toss-up") and calls[0]["urgency"] == "soon"
    assert any(t.startswith("Race: B") for t in titles(calls))             # their best stream is our planned add
    race = next(c for c in calls if c["title"].startswith("Race"))
    assert race["urgency"] == "now"                                          # its tip-off is within a day
    assert any(t.startswith("Your thin days: Tue") for t in titles(calls))   # 6 empty vs 2
    assert any(t.startswith("Benched points: Tue") for t in titles(calls))
    assert any(t.startswith("Watch A") for t in titles(calls))
    far = advice.h2h_calls(players, {"ahead": 1}, h, cal, 1300.0, 1000.0, None)
    assert far[0]["title"].startswith("Comfortable")                         # +300 beats band 100 + reach 20
    behind = advice.h2h_calls(players, {"ahead": 1}, h, cal, 800.0, 1000.0, None)
    assert behind[0]["title"].startswith("Long shot")
    h["views"]["me"]["as_is"]["form"] = 850.0                                   # even on projections, far behind on form
    slow = advice.h2h_calls(players, {"ahead": 0}, h, cal, 1000.0, 1000.0, None)
    assert slow[0]["title"].startswith("Underdog on current form") and slow[0]["urgency"] == "optional"


def test_hunches():
    start = date(2026, 11, 2)
    day = start + timedelta(days=1)
    cal = FakeCal(start, 3, {"BOS": [1], "LAL": [1], "MIA": [0, 2]}, now=10_000 + 60)
    stamp = lambda days_ago: datetime.fromtimestamp(cal.now - days_ago * 86400, model.ET).strftime("%Y-%m-%dT%H:%MZ")
    players = {1: player(1, team="MIA", value=50.0, fppg=20.0), 2: player(2, team="MIA", value=300.0, fppg=40.0),
               10: player(10, team="BOS", owner=0, fa="FREEAGENT", value=20.0, fppg=22.0),   # minutes jump
               11: player(11, team="BOS", owner=0, fa="FREEAGENT", value=20.0, fppg=22.0),   # nothing new
               12: player(12, team="LAL", owner=0, fa="FREEAGENT", value=20.0, fppg=22.0),   # starter newly out
               20: player(20, team="LAL", owner=5, out=True, back=str(start + timedelta(days=9))),
               21: player(21, team="BOS", owner=5, out=True, back=str(day))}                 # back by that day
    players[10]["stats"], players[10]["recent"] = {"proj": [70, 20.0, 22.0]}, [[1, 35.0, 31], [0, 30.0, 29]]
    players[11]["stats"], players[11]["recent"] = {"proj": [70, 28.0, 22.0]}, [[1, 22.0, 29]]
    players[20]["stats"], players[20]["inj"] = {"proj": [70, 33.0, 40.0]}, {"type": "Knee", "updated": stamp(1)}
    players[21]["stats"], players[21]["inj"] = {"proj": [70, 33.0, 40.0]}, {"type": "Ankle", "updated": stamp(1)}
    for p in players.values():
        p["odds"], p["own_chg"] = {str(g["date"]): 1.0 for g in cal.by_team[p["team"]]}, 0.0
    lg = league_with([(1, "UT"), (2, "UT")])
    lg["starting"] = ["UT", "UT"]
    h = advice.hunches(lg, players, 3, cal, {"days": [str(day)], "list": [{"net": 25.0}]})
    signs = {r["id"]: r["signs"] for r in h["list"]}
    assert set(signs) == {10, 12}, "a minutes jump and a starter newly out are hunches; a teammate back that day is not: %s" % signs
    assert "30 min a game over his last 2 games" in signs[10][0] and "P20 (knee, out" in signs[12][0], signs
    r = next(r for r in h["list"] if r["id"] == 12)
    assert r["drop"] == 1 and r["net"] == 22.0 - 0.5 * (50.0 - 20.0) and r["vs_top"] == r["net"] - 25.0, r
    players[20]["inj"]["updated"] = stamp(advice.HUNCH_NEWS_DAYS + 1)
    assert {r["id"] for r in advice.hunches(lg, players, 3, cal, {"days": [str(day)], "list": []})["list"]} == {10}, \
        "an injury from last week is already in the projections"


def test_upgrades_skip_streaming_spot():
    players = {1: player(1, value=40.0), 2: player(2, value=160.0), 3: player(3, value=900.0),
               9: player(9, value=120.0, owner=0, fa="WAIVERS")}
    lg = league_with([(1, "BE"), (2, "BE"), (3, "UT")])
    lg["starting"], lg["slots"] = ["UT"], {"BE": 2}
    ups = advice.upgrades(lg, players, 3)
    assert ups["drop"] == 2 and not ups["list"], "a free agent worth less than your weakest kept player is no upgrade: %s" % ups


def test_ceiling_and_upside():
    assert abs(model.ceiling(20.0, []) - 20.0 * (1 + 2.9 / 20 ** 0.5)) < 1e-9, "no games: the typical shape"
    steady, spiky = model.ceiling(20.0, [20.0] * 10), model.ceiling(20.0, [5.0, 35.0] * 5)
    assert steady < model.ceiling(20.0, []) < spiky, (steady, spiky)
    flat = {"fppg": 20.0, "role": 20.0, "ceiling": model.ceiling(20.0, [])}
    assert abs(model.upside(flat) - 20.0) < 1e-9, "same role, typical spread: no tilt"
    grown = dict(flat, role=26.0)
    assert abs(model.upside(grown) - (20.0 + model.UPSIDE_RISK * 6.0)) < 1e-9, "half the role gain"
    p = player(1)
    p["ros"].update(fppg=20.0, upside=23.0, value=100.0, value_up=140.0)
    v = advice.upside_view({1: p})[1]["ros"]
    assert (v["fppg"], v["value"]) == (23.0, 140.0) and p["ros"]["fppg"] == 20.0, "the view leaves the real numbers alone"


def test_locked_left():
    # a starter whose game has tipped: what is still to come, and whether he counts as a start
    out = player(1, status="OUT", out=True)
    assert advice.locked_left(out) == (0.0, 0.0), "ruled out, no live line: no start"
    assert advice.locked_left(player(2)) == (0.0, 1.0), "no live line: a full start, nothing more to add"
    dnp = dict(player(3), live={"pts": 0.0, "min": None, "state": "post", "left": 0.0})
    assert advice.locked_left(dnp) == (0.0, 0.0), "a finished game he never got into"
    mid = dict(player(4, fppg=30.0), live={"pts": 12.0, "min": 18.0, "state": "in", "left": 0.5})
    assert advice.locked_left(mid) == (15.0, 1.0), "half the game left: half his points a game to come"
    sat = dict(out, live={"pts": 0.0, "min": None, "state": "in", "left": 0.5})
    assert advice.locked_left(sat) == (0.0, 0.0), "ruled out and not on the floor"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
