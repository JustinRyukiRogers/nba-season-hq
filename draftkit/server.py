"""Local web app for draft night.

    python3 -m draftkit serve        then open http://localhost:8765

Picks arrive from ESPN's live draft feed when the login cookies are in .env, and can always be
entered by hand. Nothing here writes to ESPN.
"""
import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import espn
from .config import DATA, PORT, STATIC, TEAM_ALIASES, load_env, write_json
from .engine import load_draft

POLL_SECONDS = 3


class LiveSync(threading.Thread):
    """Polls ESPN's draft feed and folds new picks into the draft."""

    def __init__(self, draft):
        super().__init__(daemon=True)
        self.draft = draft
        self.cookies = load_env()
        self.enabled = bool(self.cookies)
        self.status = {"available": bool(self.cookies), "enabled": self.enabled, "state": "idle",
                       "message": "" if self.cookies else "No ESPN cookies in .env, so picks are entered by hand.",
                       "last_ok": None, "espn_picks": 0}

    def run(self):
        while True:
            if self.enabled and self.cookies:
                self.poll()
            time.sleep(POLL_SECONDS)

    def poll(self):
        try:
            live = espn.fetch_draft_picks(self.cookies)
        except espn.EspnAuthError:
            self.status.update(state="error", message="ESPN rejected the cookies in .env (expired?). Enter picks by hand.")
            return
        except Exception as err:        # network blip: keep the board usable and try again
            self.status.update(state="error", message="ESPN feed unreachable (%s). Retrying." % type(err).__name__)
            return
        with self.draft.lock:
            before = list(self.draft.pick_teams)
            self.draft.merge_live(live)
            if self.draft.pick_teams != before:        # ESPN's order replaced ours: remember it across restarts
                league = self.draft.league
                league["draft"].update(pick_teams=list(self.draft.pick_teams), order_confirmed=True, my_slot_by_hand=None,
                                       pick_order=list(self.draft.pick_teams[:league["size"]]))
                write_json(DATA / "league.json", league, indent=1)
                self.draft.save()
        state = "live" if live["in_progress"] else ("finished" if live["drafted"] else "waiting")
        self.status.update(state=state, message="", last_ok=time.strftime("%H:%M:%S"), espn_picks=len(live["picks"]))


def state_payload(draft, sync):
    """Everything on screen that changes during the draft."""
    with draft.lock:
        overall = draft.next_overall()
        done = draft.done()
        team = None if done else draft.team_at(overall)
        mine = draft.my_picks_after(overall)
        rec = draft.recommend() if not done and mine else {"candidates": [], "horizons": [], "needs": draft.needs(), "need_only": False}
        sim = draft.simulate() if not done and mine else {"survive": {}, "best_at_q1": [], "horizons": []}
        likely = {}
        for top in sim["best_at_q1"]:
            if top:
                likely[top[0]] = likely.get(top[0], 0) + 1
        n = float(len(sim["best_at_q1"]) or 1)
        roster = draft.roster(draft.my_team)
        return {
            "version": len(draft.picks),
            "order": draft.pick_teams[:draft.size], "order_confirmed": draft.order_confirmed,
            "done": done,
            "pick": {"overall": overall, "round": (overall - 1) // draft.size + 1,
                     "in_round": (overall - 1) % draft.size + 1, "team": team,
                     "mine": team == draft.my_team, "total": len(draft.pick_teams)},
            "my_picks": mine,
            "horizons": rec["horizons"],
            "recommend": rec["candidates"],
            "need_only": rec["need_only"], "needs": rec["needs"],
            "likely": sorted(([pid, c / n] for pid, c in likely.items()), key=lambda kv: -kv[1])[:6],
            "survive": {str(pid): [round(v, 3) for v in s] for pid, s in sim["survive"].items()},
            "picks": draft.picks,
            "roster_values": {str(pid): round(draft.roster_value(pid, roster), 1)
                              for pid in draft.players if draft.players[pid].get("absent_games")},
            "sync": sync.status,
        }


def board_payload(draft):
    """Everything that stays fixed during the draft."""
    lg = draft.league
    return {"league": {"name": lg["name"], "size": lg["size"], "teams": lg["teams"], "my_team": lg["my_team_id"],
                       "slots": lg["slots"], "pick_teams": draft.pick_teams, "order_confirmed": draft.order_confirmed,
                       "seconds_per_pick": lg["draft"].get("seconds_per_pick"), "draft_ms": lg["draft"].get("date_ms")},
            "generated": draft.board["generated"], "replacement": draft.board["replacement"],
            "weeks": draft.board["weeks"], "regular_weeks": draft.board["regular_weeks"],
            "team_aliases": TEAM_ALIASES,       # ESPN's spellings (BKN, PHX, ...) -> the board's, for search
            "players": draft.board["players"]}


class Handler(BaseHTTPRequestHandler):
    draft = None
    sync = None

    def log_message(self, *args):
        pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/state":
            return self.send_json(state_payload(self.draft, self.sync))
        if path == "/api/board":
            return self.send_json(board_payload(self.draft))
        name = "index.html" if path == "/" else path.lstrip("/")
        target = (STATIC / name).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            return self.send_json({"error": "not found"}, 404)
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.send_json({"error": "bad request"}, 400)
        draft, path = self.draft, self.path.split("?")[0]
        try:
            with draft.lock:
                if path == "/api/pick":
                    draft.add_pick(int(data["player_id"]), name=data.get("name"))
                elif path == "/api/undo":
                    draft.undo()
                elif path == "/api/reset":
                    draft.reset()
                elif path == "/api/mock":
                    # play the room's picks: one pick, or everything up to my next turn
                    steps = 0
                    while not draft.done() and steps < 400:
                        if draft.team_at(draft.next_overall()) == draft.my_team and (steps or data.get("until_me")):
                            break
                        draft.auto_pick()
                        steps += 1
                        if not data.get("until_me"):
                            break
                elif path == "/api/sync":
                    self.sync.enabled = bool(data.get("enabled")) and bool(self.sync.cookies)
                    self.sync.status["enabled"] = self.sync.enabled
                else:
                    return self.send_json({"error": "not found"}, 404)
        except (KeyError, ValueError) as err:
            return self.send_json({"error": str(err)}, 400)
        self.send_json(state_payload(draft, self.sync))


def serve(port=PORT, open_browser=True):
    url = "http://localhost:%d" % port
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        print("The draft board is already running at %s" % url)
        print("To restart it (after a refresh, or a change to the draft order): pkill -f 'draftkit serve', then run this again.")
        if open_browser:
            import webbrowser
            webbrowser.open(url)
        return
    draft = load_draft()
    sync = LiveSync(draft)
    sync.start()
    Handler.draft, Handler.sync = draft, sync
    print("Draft board: %s   (Ctrl+C to stop)" % url)
    print("ESPN live sync: %s" % ("on" if sync.enabled else "off (no cookies in .env); enter picks by hand"))
    if open_browser:
        import webbrowser
        threading.Timer(0.6, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped; the draft so far is saved in data/draft_state.json")
