"""Checks on the draft engine and the live-sync merge, using the real board and made-up ESPN feeds.

    python3 tests/test_engine.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from draftkit import board, engine, espn
from draftkit.config import load_league, snake

league, brd = load_league(), board.load()
market = [p["id"] for p in sorted(brd["players"], key=lambda p: p["market"])]


def feed(draft, n, extra=()):
    """ESPN's draft feed after n picks, taking players in market order."""
    return {"in_progress": True, "drafted": False, "pick_teams": draft.pick_teams,
            "picks": [{"overall": i + 1, "team": draft.pick_teams[i], "player_id": market[i]} for i in range(n)] + list(extra)}


def test_live_sync():
    d = engine.Draft(league, brd, seed=1)
    assert d.merge_live(feed(d, 3)) and [p["source"] for p in d.picks] == ["espn"] * 3
    assert not d.merge_live(feed(d, 3)), "an unchanged feed reports no change"

    d.add_pick(market[10])
    d.add_pick(market[4])                      # two picks typed by hand, ahead of the feed
    d.merge_live(feed(d, 3))
    assert len(d.picks) == 5, "hand entries survive while the feed is behind"

    d.merge_live(feed(d, 4))                   # ESPN reports a different player at pick 4
    assert [p["player_id"] for p in d.picks] == market[:4] + [market[4]], "ESPN wins; the later hand entry stays"
    d.merge_live(feed(d, 5))
    assert d.picks[4]["source"] == "espn", "ESPN confirming a hand entry takes it over"

    gap = {"in_progress": True, "drafted": False, "pick_teams": d.pick_teams,
           "picks": [{"overall": 2, "team": 5, "player_id": market[1]}]}
    assert not d.merge_live(gap) and len(d.picks) == 5, "a feed with a gap is ignored"

    d.merge_live(feed(d, 5, [{"overall": 6, "team": d.pick_teams[5], "player_id": 99999999}]))
    assert d.picks[5]["name"].startswith("Player") and d.next_overall() == 7, "off-board picks still fill a slot"
    assert d.recommend()["candidates"]
    d.undo()
    assert d.next_overall() == 6



def espn_raw(draft, made, made_only=False):
    """ESPN's mDraftDetail response: every slot is listed before the draft, playerId -1 until made."""
    slots = [{"id": i + 1, "overallPickNumber": i + 1, "roundId": i // draft.size + 1, "roundPickNumber": i % draft.size + 1,
              "teamId": t, "playerId": market[i] if i < made else -1, "keeper": False, "reservedForKeeper": False}
             for i, t in enumerate(draft.pick_teams)]
    return {"draftDetail": {"drafted": False, "inProgress": True, "picks": (slots[:made] if made_only else slots)[::-1]}}


def test_espn_response_shape():
    """fetch_draft_picks on ESPN's own shape, and on a mid-draft list of only the picks made so far."""
    real = espn.get_json
    try:
        for made_only in (False, True):
            d = engine.Draft(league, brd, seed=1)
            espn.get_json = lambda *a, **k: espn_raw(d, 7, made_only)
            live = espn.fetch_draft_picks({})
            assert [p["player_id"] for p in live["picks"]] == market[:7]
            d.merge_live(live)
            assert d.pick_teams == league["draft"]["pick_teams"], "a list of the picks so far must not replace the schedule"
            assert len(d.picks) == 7 and not d.done() and d.recommend()["candidates"]
    finally:
        espn.get_json = real


def test_practice_cleared_by_first_espn_pick():
    d = engine.Draft(league, brd, seed=3)
    for _ in range(6):
        d.auto_pick()
    d.add_pick(next(pid for pid in d.by_value if pid not in d.taken()))     # my own practice pick, typed
    waiting = {"in_progress": False, "drafted": False, "pick_teams": d.pick_teams, "picks": []}
    assert not d.merge_live(waiting) and len(d.picks) == 7, "practice is kept until ESPN's draft has picks"
    opened = dict(waiting, in_progress=True)
    assert not d.merge_live(opened) and len(d.picks) == 7, "and while the draft is open but nobody has picked"
    assert d.merge_live(feed(d, 1)) and [p["source"] for p in d.picks] == ["espn"], "ESPN's first pick clears it"

def test_draft_order_rules():
    """A seat set by hand survives a placeholder order on ESPN, yields to an order that agrees
    with it, and always yields once ESPN's draft is actually running."""
    me, size = league["my_team_id"], league["size"]
    rounds = len(league["draft"]["pick_teams"]) // size
    others = [t for t in league["draft"]["pick_order"] if t != me]
    seat = lambda n: others[:n - 1] + [me] + others[n - 1:]
    by_hand = dict(league, draft=dict(league["draft"], pick_order=seat(5), pick_teams=snake(seat(5), rounds),
                                      order_confirmed=False, my_slot_by_hand=5))

    d = engine.Draft(by_hand, brd, seed=1)
    assert not d.order_confirmed and d._boost(others[0]) == {}, "no manager habits pinned to unconfirmed seats"
    placeholder = {"in_progress": False, "drafted": False, "pick_teams": snake(seat(11), rounds), "picks": []}
    assert not d.merge_live(placeholder) and d.my_picks_after(1)[0] == 5, "a placeholder order must not move me"
    agreeing = snake(others[3::-1] + [me] + others[4:], rounds)
    assert d.merge_live({"in_progress": False, "drafted": False, "pick_teams": agreeing, "picks": []})
    assert d.order_confirmed and d.pick_teams == agreeing and d.my_picks_after(1)[0] == 5

    d = engine.Draft(by_hand, brd, seed=1)
    d.add_pick(market[0])                      # typed by hand under my order
    running = {"in_progress": True, "drafted": False, "pick_teams": snake(seat(11), rounds),
               "picks": [{"overall": 1, "team": seat(11)[0], "player_id": market[0]}]}
    assert d.merge_live(running) and d.my_picks_after(1)[0] == 11, "once the draft is running, ESPN's order is the order"


def test_full_mock():
    d = engine.Draft(league, brd, seed=4)
    while not d.done():
        d.auto_pick()
    mine = d.roster(d.my_team)
    assert len(d.picks) == len(d.pick_teams) and len(mine) == 13
    assert not d.needs(mine), "my roster can fill every position slot: %s" % d.needs(mine)
    assert len(set(p["player_id"] for p in d.picks)) == len(d.picks), "nobody drafted twice"


def test_recommendation_shape():
    d = engine.Draft(league, brd, seed=2)
    while d.team_at(d.next_overall()) != d.my_team:
        d.auto_pick()
    rec = d.recommend()
    top = rec["candidates"][0]
    assert rec["horizons"] == d.my_picks_after(d.next_overall() + 1)[:2]
    assert all(0 <= s <= 1 for c in rec["candidates"] for s in c["survive"])
    assert top["score"] >= rec["candidates"][-1]["score"] and top["reason"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
