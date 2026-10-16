"""Command line for the draft kit.

    python3 -m draftkit refresh     pull the latest ESPN projections, ADP, injuries, news and other sources
    python3 -m draftkit build       recompute the board from what is on disk (after editing data/intel.json)
    python3 -m draftkit serve       open the draft board at http://localhost:8765
    python3 -m draftkit show [N]    print the top N of the board
    python3 -m draftkit slot N      you pick Nth; the other seats stay as listed until confirmed
    python3 -m draftkit order [A B ...]   show the draft order, or set all of it by team abbreviation
    python3 -m draftkit fit         refit the calibration from past seasons
    python3 -m draftkit history     re-download past seasons (rarely needed)
"""
import sys
import time

from . import board, calibrate, espn, sources
from .config import DATA, HISTORY, RAW, SEASON, load_env, load_league, read_json, snake, write_json


def refresh(with_news=True):
    """Pull everything that changes day to day, then rebuild the board. Each source is saved as
    it arrives, so one failing does not lose the others."""
    stamp = time.strftime("%Y%m%d-%H%M")

    def step(label, fn):
        try:
            result = fn()
            print("  ok    %s" % label + (" (%s)" % result if result else ""))
        except Exception as err:
            print("  FAIL  %s: %s  (keeping the previous copy)" % (label, err))

    def players():
        data = espn.fetch_players()
        if len(data.get("players", [])) < 300:
            raise ValueError("only %d players came back" % len(data.get("players", [])))
        write_json(RAW / "espn_players.json", data)
        write_json(RAW / "snapshots" / ("espn_players_%s.json" % stamp), data)   # ADP moves daily; keep the trail
        return "%d players" % len(data["players"])

    def league():
        cookies = load_env()
        if not cookies:
            return "skipped: no cookies in .env, using data/league.json as is"
        current = read_json(DATA / "league.json") or {}
        fresh = espn.fetch_league(cookies, my_team_id=current.get("my_team_id"))
        mine = current.get("draft") or {}
        slot = mine.get("my_slot_by_hand")
        if slot and fresh["draft"]["pick_order"][slot - 1] != fresh["my_team_id"]:
            # ESPN has not caught up with the seat I was given by hand, so its order is a placeholder
            fresh["draft"].update({k: mine[k] for k in ("pick_order", "pick_teams", "order_confirmed", "my_slot_by_hand")})
            note = "ESPN still lists a different order; keeping yours (you pick %d)" % slot
        else:
            note = "draft order " + " ".join(fresh["teams"][str(t)]["abbrev"] for t in fresh["draft"]["pick_order"])
            if mine.get("pick_order") and mine["pick_order"] != fresh["draft"]["pick_order"]:
                note += "  (changed)"
        write_json(DATA / "league.json", fresh, indent=1)
        return note

    step("ESPN players, projections, ADP", players)
    step("ESPN schedule", lambda: write_json(RAW / "schedule.json", espn.fetch_schedule()))
    step("ESPN injury report", lambda: write_json(RAW / "injuries.json", espn.fetch_injuries(), indent=1))
    step("FantasyPros projections", lambda: (RAW / "fantasypros.html").write_text(sources.fetch_fantasypros()) and None)
    step("Hashtag Basketball ADP", lambda: (RAW / "hashtag_adp.html").write_text(sources.fetch_hashtag_adp()) and None)
    step("league settings", league)
    if with_news:
        step("ESPN player news", fetch_news)
    board.build()
    tune()


def tune():
    """Re-tune the room model against today's ADP (the board has to exist first)."""
    from .config import load_league
    from .engine import Draft, tune_room
    tune_room(Draft(load_league(), board.load(), calibrate.load()))


def fetch_news(top=260):
    """Latest news blurbs for the players most likely to be drafted."""
    raw = read_json(RAW / "espn_players.json")
    ranked = sorted((e["player"] for e in raw["players"]),
                    key=lambda p: (p.get("ownership") or {}).get("averageDraftPosition") or 999)
    news = read_json(RAW / "news.json", {}) or {}
    for i, pl in enumerate(ranked[:top]):
        try:
            news[str(pl["id"])] = espn.fetch_news(pl["id"])
        except Exception:
            pass
        if i % 10 == 9:
            time.sleep(0.5)
    write_json(RAW / "news.json", news)
    return "%d players" % min(top, len(ranked))


def _save_order(league, order, confirmed, slot_by_hand):
    rounds = len(league["draft"]["pick_teams"]) // league["size"]
    league["draft"].update(pick_order=order, pick_teams=snake(order, rounds),
                           order_confirmed=confirmed, my_slot_by_hand=slot_by_hand)
    espn.check_league(league)
    write_json(DATA / "league.json", league, indent=1)
    show_order(league)


def show_order(league=None):
    league = league or load_league()
    me, draft = league["my_team_id"], league["draft"]
    for i, t in enumerate(draft["pick_order"], 1):
        print("  %2d  %-5s %s%s" % (i, league["teams"][str(t)]["abbrev"], league["teams"][str(t)]["name"], "   <- you" if t == me else ""))
    print("your picks: " + ", ".join("#%d" % (i + 1) for i, t in enumerate(draft["pick_teams"]) if t == me))
    if not draft.get("order_confirmed", True):
        print("only your seat is confirmed; the others are as ESPN last listed them")


def set_slot(n):
    league = load_league()
    if not 1 <= n <= league["size"]:
        raise SystemExit("slot must be between 1 and %d" % league["size"])
    me = league["my_team_id"]
    order = [t for t in league["draft"]["pick_order"] if t != me]
    order.insert(n - 1, me)
    _save_order(league, order, confirmed=False, slot_by_hand=n)


def set_order(tokens):
    league = load_league()
    by_abbrev = {v["abbrev"].upper(): int(k) for k, v in league["teams"].items()}
    try:
        order = [int(t) if t.isdigit() else by_abbrev[t.upper()] for t in tokens]
    except KeyError as err:
        raise SystemExit("unknown team %s; use one of: %s" % (err, " ".join(sorted(by_abbrev))))
    if sorted(order) != sorted(by_abbrev.values()):
        raise SystemExit("list every team exactly once, in draft order: %s" % " ".join(sorted(by_abbrev)))
    _save_order(league, order, confirmed=True, slot_by_hand=None)


def history():
    for year in range(2018, SEASON):
        write_json(HISTORY / ("espn_%d.json" % year), espn.fetch_players(year))
        print("espn", year)
        time.sleep(1)
    sources.fetch_bref(range(2018, SEASON))
    calibrate.fit()
    tune()


def main(argv):
    cmd = argv[0] if argv else ""
    if cmd == "refresh":
        refresh(with_news="--no-news" not in argv)
    elif cmd == "build":
        board.build()
    elif cmd == "fit":
        calibrate.fit()
        tune()
    elif cmd == "show":
        board.report_board(board.load(), int(argv[1]) if len(argv) > 1 else 60)
    elif cmd == "slot" and len(argv) == 2 and argv[1].isdigit():
        set_slot(int(argv[1]))
    elif cmd == "order":
        set_order(argv[1:]) if len(argv) > 1 else show_order()
    elif cmd == "serve":
        from .server import serve
        serve(open_browser="--no-browser" not in argv)
    elif cmd == "history":
        history()
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
