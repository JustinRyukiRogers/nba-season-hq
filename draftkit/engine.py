"""The draft itself: who has been taken, who is likely to go before your next pick, and which
pick is best given both."""
import random
import threading

from . import calibrate
from .board import BASELINE
from .config import DATA, read_json, write_json

FAN_BOOST = 1.5      # a manager's lean toward players on the NBA team they keep drafting from
WINDOW = 60          # how far down the market list an opponent's pick is allowed to reach
SIMS = 400
SPECIFIC_SLOTS = ["PG", "SG", "SF", "PF", "C", "G", "F"]   # UT takes anyone, so only these can go unfilled


class Draft:
    """Draft state plus the model of the room. Not thread-safe on its own; the server holds `lock`."""

    def __init__(self, league, board, cal=None, state_path=None, seed=None):
        self.league = league
        self.board = board
        self.cal = cal or calibrate.load()
        self.players = {p["id"]: p for p in board["players"]}
        self.market = sorted(self.players, key=lambda pid: self.players[pid]["market"])
        self.by_value = sorted(self.players, key=lambda pid: -self.players[pid]["value"])
        self.pick_teams = list(league["draft"]["pick_teams"])
        # False while only my own seat is known: the other managers' habits are then not pinned to seats
        self.order_confirmed = league["draft"].get("order_confirmed", True)
        self.my_slot_by_hand = league["draft"].get("my_slot_by_hand")
        self.my_team = league["my_team_id"]
        self.size = league["size"]
        self.replacement = board["replacement"]
        self.state_path = state_path
        self.rng = random.Random(seed)
        self.lock = threading.RLock()
        self.picks = []          # [{"overall", "team", "player_id", "source"}], in pick order
        self._cache = {}
        room = self.cal.get("room") or {}
        self.pmf = room.get("pmf") or [[0.0] + [0.5 ** k for k in range(1, 81)]]
        self.buckets = room.get("buckets") or [[1, 999]]
        self.repeat_boost = room.get("repeat_boost", 1.0)
        self.overdue = room.get("overdue")
        self.adp = {pid: (p["espn"]["adp"] or p["market"]) for pid, p in self.players.items()}
        self.owned = {int(t): set(ids) for t, ids in (room.get("owned") or {}).items()}
        self.fan = {int(t): f["team"] for t, f in (room.get("fan") or {}).items()}
        if state_path:
            saved = read_json(state_path)
            if saved and saved.get("pick_teams") == self.pick_teams:
                self.picks = [p for p in saved.get("picks", []) if p["player_id"] in self.players or p.get("name")]

    # ---------- state ----------

    def save(self):
        if self.state_path:
            write_json(self.state_path, {"pick_teams": self.pick_teams, "picks": self.picks})

    def taken(self):
        return {p["player_id"] for p in self.picks}

    def roster(self, team):
        return [p["player_id"] for p in self.picks if p["team"] == team]

    def next_overall(self):
        return len(self.picks) + 1

    def team_at(self, overall):
        return self.pick_teams[overall - 1] if 1 <= overall <= len(self.pick_teams) else None

    def done(self):
        return len(self.picks) >= len(self.pick_teams)

    def my_picks_after(self, overall):
        """My pick numbers at or after `overall`."""
        return [i + 1 for i, t in enumerate(self.pick_teams) if t == self.my_team and i + 1 >= overall]

    def add_pick(self, player_id, source="manual", name=None):
        if self.done():
            raise ValueError("the draft is over")
        if player_id in self.taken():
            raise ValueError("%s is already drafted" % self.name(player_id))
        overall = self.next_overall()
        pick = {"overall": overall, "team": self.team_at(overall), "player_id": player_id, "source": source}
        if player_id not in self.players:
            pick["name"] = name or "Player %s" % player_id    # someone off the board; still fills a slot
        self.picks.append(pick)
        self._cache.clear()
        self.save()
        return pick

    def undo(self):
        if self.picks:
            self.picks.pop()
            self._cache.clear()
            self.save()

    def reset(self):
        self.picks = []
        self._cache.clear()
        self.save()

    def merge_live(self, live):
        """Fold in ESPN's pick list. ESPN is the authority for every pick it reports; picks entered
        by hand ahead of the feed are kept until the feed catches up. Picks on the board before ESPN
        reported any were practice, and are cleared. -> True if anything changed."""
        order_changed = self._adopt_order(live)
        feed = [{"overall": p["overall"], "team": p["team"], "player_id": p["player_id"], "source": "espn"}
                for p in live["picks"]]
        if [p["overall"] for p in feed] != list(range(1, len(feed) + 1)):
            return False                                       # a gap in the feed; wait for the next poll
        if feed and not any(p["source"] == "espn" for p in self.picks):
            self.picks = []                                    # ESPN's first picks: what was here before was practice
        seen = {p["player_id"] for p in feed}
        ahead = [p for p in self.picks[len(feed):] if p["player_id"] not in seen and p["source"] != "espn"]
        merged = feed + [dict(p, overall=len(feed) + i + 1, team=self.team_at(len(feed) + i + 1))
                         for i, p in enumerate(ahead)]
        for p in merged:
            if p["player_id"] not in self.players and "name" not in p:
                p["name"] = "Player %s" % p["player_id"]
        if [(p["player_id"], p["team"]) for p in merged] == [(p["player_id"], p["team"]) for p in self.picks]:
            self.picks = merged            # same draft; only which picks ESPN has confirmed may differ
            return order_changed
        self.picks = merged
        self._cache.clear()
        self.save()
        return True

    def _adopt_order(self, live):
        """Take ESPN's pick schedule when it should be believed. Once the draft is running it is
        the schedule, full stop. Before that it may be a placeholder the commissioner has not
        updated, so it is only taken if it agrees with a seat I was told by hand. -> True if changed"""
        espn_order = live.get("pick_teams")
        if not espn_order or espn_order == self.pick_teams:
            return False
        if len(espn_order) < len(self.pick_teams):
            return False          # only the picks made so far, not the whole schedule
        started = bool(live.get("in_progress") or live.get("picks"))
        slot = self.my_slot_by_hand
        agrees = not slot or espn_order[slot - 1] == self.my_team
        if not (started or (agrees and not self.picks)):
            return False
        self.pick_teams = list(espn_order)
        self.order_confirmed, self.my_slot_by_hand = True, None
        self._cache.clear()
        return True

    def name(self, player_id):
        p = self.players.get(player_id)
        return p["name"] if p else str(player_id)

    # ---------- the room ----------

    def _bucket(self, overall):
        return calibrate.bucket_of(overall, self.buckets)

    def _boost(self, team):
        """Per-player multipliers for one manager: players they have drafted before, and their NBA team."""
        if not self.order_confirmed:
            return {}
        owned, fan = self.owned.get(team, ()), self.fan.get(team)
        out = {}
        for pid in owned:
            out[pid] = self.repeat_boost
        if fan:
            for pid, p in self.players.items():
                if p["team"] == fan:
                    out[pid] = out.get(pid, 1.0) * FAN_BOOST
        return out

    def opponent_pick(self, remaining, overall, boosts, rng):
        """Sample the pick an opponent makes from `remaining` (ids in market order)."""
        boost = boosts.get(self.team_at(overall), {})
        window = remaining[:WINDOW]
        return rng.choices(window, self._weights(window, overall, self.overdue, boost))[0]

    def _weights(self, window, overall, tuning, boost=None):
        """Chance weights for the next pick over `window`, the top of what is left in market order."""
        pmf = self.pmf[self._bucket(overall)]
        gamma = calibrate.sharpness(overall, tuning) if tuning else 1.0
        weights = [pmf[k + 1] ** gamma if gamma != 1.0 else pmf[k + 1] for k in range(len(window))]
        if boost:
            weights = [w * boost.get(pid, 1.0) for w, pid in zip(weights, window)]
        if tuning:
            # only the first few names can have slid far enough for this to matter
            for k in range(min(8, len(window))):
                weights[k] *= calibrate.overdue(overall, self.adp[window[k]], tuning)
        return weights

    def my_pick(self, remaining, roster):
        """What I would take with no look-ahead: best value left, respecting roster needs."""
        need_only = self._must_fill(roster, len(roster))
        pool = sorted(remaining, key=lambda pid: -self.players[pid]["value"])
        for pid in pool:
            if not need_only or self._fills_need(roster, pid):
                return pid
        return pool[0]

    def simulate(self, sims=SIMS):
        """Play out the picks between now and my next two turns many times.
        -> {"horizons": [q1, q2], "survive": {pid: [p at q1, p at q2]}, "next_best": [...]}"""
        key = ("sim", len(self.picks), sims)
        if key in self._cache:
            return self._cache[key]
        start = self.next_overall()
        mine = self.my_picks_after(start + (1 if self.team_at(start) == self.my_team else 0))
        horizons = mine[:2]
        taken = self.taken()
        remaining0 = [pid for pid in self.market if pid not in taken]
        candidates = [pid for pid in self.by_value if pid not in taken][:70]
        counts = {pid: [0, 0] for pid in candidates}
        best_at_q1 = []            # per sim: the two most valuable survivors at my next pick
        boosts = {t: self._boost(t) for t in set(self.pick_teams)}
        my_roster0 = self.roster(self.my_team)
        value = {pid: self.players[pid]["value"] for pid in remaining0}
        end = horizons[-1] if horizons else start
        for _ in range(sims if horizons else 0):
            remaining = list(remaining0)
            roster = list(my_roster0)
            for overall in range(start, end + 1):
                at_horizon = overall in horizons
                if at_horizon:
                    h = horizons.index(overall)
                    alive = set(remaining)
                    for pid in candidates:
                        if pid in alive:
                            counts[pid][h] += 1
                    if h == 0:
                        top = sorted((pid for pid in candidates if pid in alive), key=lambda p: -value[p])[:2]
                        best_at_q1.append(top)
                if overall == start and self.team_at(start) == self.my_team:
                    continue                      # the pick being decided now: leave everyone on the board
                if self.team_at(overall) == self.my_team:
                    pid = self.my_pick(remaining, roster)
                    roster.append(pid)
                else:
                    pid = self.opponent_pick(remaining, overall, boosts, self.rng)
                remaining.remove(pid)
        n = float(sims)
        out = {"horizons": horizons,
               "survive": {pid: [c[0] / n, c[1] / n] for pid, c in counts.items()} if horizons else {},
               "best_at_q1": best_at_q1}
        self._cache[key] = out
        return out

    # ---------- my roster ----------

    def _matching(self, roster):
        """Most of the position-specific starting slots this roster can fill. -> (count, open slots)"""
        slots = []
        for s in SPECIFIC_SLOTS:
            slots += [s] * self.league["slots"].get(s, 0)
        owner = {}

        def place(pid, seen):
            for i, s in enumerate(slots):
                if s in self.players[pid]["slots"] and i not in seen:
                    seen.add(i)
                    if i not in owner or place(owner[i], seen):
                        owner[i] = pid
                        return True
            return False

        for pid in roster:
            if pid in self.players:
                place(pid, set())
        return len(owner), [s for i, s in enumerate(slots) if i not in owner]

    def needs(self, roster=None):
        return self._matching(self.roster(self.my_team) if roster is None else roster)[1]

    def _fills_need(self, roster, pid):
        return self._matching(roster + [pid])[0] > self._matching(roster)[0]

    def _must_fill(self, roster, picks_made):
        """True once every pick I have left is needed to field a legal lineup."""
        total = sum(1 for t in self.pick_teams if t == self.my_team)
        return (total - picks_made) <= len(self._matching(roster)[1])

    def roster_value(self, pid, roster):
        """A player's value on this particular roster: a third long-term absentee has no IR slot to
        sit in, so his missed weeks cost a roster spot."""
        p = self.players[pid]
        value = p["value"]
        if p.get("absent_games"):
            stashed = sum(1 for r in roster if r in self.players and self.players[r].get("absent_games", 0) > 6)
            if stashed >= self.league["slots"].get("IR", 0):
                value -= self.replacement * BASELINE * p["absent_games"]
        return value

    # ---------- the recommendation ----------

    def recommend(self, top=6):
        """Rank the candidates for my pick. A candidate's score is his value now plus the value I
        should expect from the best player still there at my next pick if I take him."""
        key = ("rec", len(self.picks))
        if key in self._cache:
            return self._cache[key]
        taken = self.taken()
        roster = self.roster(self.my_team)
        sim = self.simulate()
        survive, horizons = sim["survive"], sim["horizons"]
        need_only = self._must_fill(roster, len(roster))
        cands = []
        for pid in [p for p in self.by_value if p not in taken][:40]:
            if need_only and not self._fills_need(roster, pid):
                continue
            value = self.roster_value(pid, roster)
            # what I'd still get next time if I take this player now
            later = [(self.players[b[0]]["value"] if b[0] != pid else (self.players[b[1]]["value"] if len(b) > 1 else 0.0))
                     for b in sim["best_at_q1"] if b]
            nxt = sum(later) / len(later) if later else 0.0
            cands.append({"id": pid, "value": round(value, 1), "next": round(nxt, 1), "score": round(value + nxt, 1),
                          "survive": survive.get(pid, [0.0, 0.0])})
        cands.sort(key=lambda c: -c["score"])
        best = cands[0] if cands else None
        for c in cands:
            c["gap"] = round(best["score"] - c["score"], 1)
            c["reason"] = self._reason(c, cands, horizons, need_only)
        out = {"candidates": cands[:top], "horizons": horizons, "need_only": need_only, "needs": self.needs(roster)}
        self._cache[key] = out
        return out

    def _reason(self, c, cands, horizons, need_only):
        p = self.players[c["id"]]
        bits = []
        by_value = sorted(cands, key=lambda x: -x["value"])
        place = by_value.index(c)
        if place == 0:
            lead = c["value"] - by_value[1]["value"] if len(by_value) > 1 else 0
            bits.append("Highest value left" + (", %d pts clear of the next" % lead if lead >= 15 else ""))
        else:
            bits.append("%d pts behind %s on value" % (by_value[0]["value"] - c["value"], self.players[by_value[0]["id"]]["name"]))
        if horizons:
            s = c["survive"][0]
            when = "pick %d" % horizons[0]
            if s <= 0.15:
                bits.append("almost certainly gone by %s" % when)
            elif s >= 0.75 and c is not cands[0]:
                bits.append("%d%% to still be there at %s, so he can wait" % (round(100 * s), when))
            else:
                bits.append("%d%% to last until %s" % (round(100 * s), when))
        if p["market"] - p["rank"] >= 12:
            bits.append("the room has him %d spots later than we do" % (p["market"] - p["rank"]))
        if need_only:
            bits.append("fills an open %s slot you must fill" % "/".join(sorted(set(p["slots"]) & set(self.needs()))))
        if p.get("out_until"):
            bits.append("out until about %s" % p["out_until"])
        return "; ".join(bits)

    # ---------- mock drafting ----------

    def auto_pick(self):
        """Make the pick for whoever is on the clock, the way the room model expects. -> pick"""
        overall = self.next_overall()
        remaining = [pid for pid in self.market if pid not in self.taken()]
        if self.team_at(overall) == self.my_team:
            rec = self.recommend()
            pid = rec["candidates"][0]["id"] if rec["candidates"] else remaining[0]
        else:
            pid = self.opponent_pick(remaining, overall, {self.team_at(overall): self._boost(self.team_at(overall))}, self.rng)
        return self.add_pick(pid, source="mock")


def tune_room(draft, sims=300, picks=36, top=16, verbose=True):
    """Set the strength of the slide pull so simulated drafts put the top players where ESPN's
    real average draft positions say they go. Writes the result into data/calibration.json."""
    def miss(params):
        rng = random.Random(5)                      # same draws for every setting
        total = {pid: 0.0 for pid in draft.market[:top]}
        for _ in range(sims):
            remaining = draft.market[:150]
            seen = set()
            for overall in range(1, picks + 1):
                window = remaining[:WINDOW]
                pid = rng.choices(window, draft._weights(window, overall, params))[0]
                remaining.remove(pid)
                if pid in total:
                    total[pid] += overall
                    seen.add(pid)
            for pid in total:
                if pid not in seen:
                    total[pid] += picks + 6       # still on the board: count as going shortly after
        return sum((total[pid] / sims - draft.adp[pid]) ** 2 for pid in total) / top

    grid = [(s0, s1, fade, sharp) for s0 in (0.3, 1.0, 2.0) for s1 in (0.05, 0.1, 0.2)
            for fade in (18, 30, 48) for sharp in (0.0, 0.75, 1.5, 3.0)]
    before = miss(None)
    best = min(grid, key=miss)
    after = miss(best)
    cal = read_json(DATA / "calibration.json")
    cal["room"]["overdue"] = list(best) if after < before else None
    cal["room"]["overdue_fit"] = {"mean_sq_error_before": round(before, 2), "mean_sq_error_after": round(after, 2), "top": top}
    write_json(DATA / "calibration.json", cal, indent=1)
    draft.overdue = cal["room"]["overdue"]
    if verbose:
        print("room model tuned to ESPN ADP: top-%d mean squared miss %.2f -> %.2f picks^2 (pull %s)" % (top, before, after, cal["room"]["overdue"]))
    return cal["room"]["overdue"]


def load_draft(seed=None):
    from . import board as board_mod
    from .config import load_league
    return Draft(load_league(), board_mod.load(), state_path=DATA / "draft_state.json", seed=seed)
