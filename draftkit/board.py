"""Turn the raw pulls into the draft board: one value per player, for this league.

    baseline   ESPN's per-game projection blended with FantasyPros' consensus
    calibrate  the corrections fitted on past seasons (calibrate.py)
    intel      researched, sourced adjustments from data/intel.json
    games      expected games inside the fantasy season, from the schedule and any known absence
    value      points above what a waiver pickup would give, with the option to cut a bust

    python3 -m draftkit build
"""
import math
import time

from . import calibrate, season
from .config import (AST, BACKFILL, BLK, DATA, ESPN_WEIGHT, FGA, FGM, FTA, FTM, GP, HISTORY,
                     INTEL_CONFIDENCE, INTEL_MAX_MINUTES, INTEL_MAX_RATE, LONG_ABSENCE_DAYS, LONG_ABSENCE_FACTOR,
                     MIN, PLAYOFF_WEEK_WEIGHT, PTS, RAW, SHORT_ABSENCE_DAYS, SHORT_ABSENCE_FACTOR,
                     REB, REPLACEMENT_AVAILABILITY, REPLACEMENT_RANK_PAD, SEASON, STL, TO, TPM, load_league,
                     read_json, write_json)
from .espn import fantasy_points, player_slots, player_team, stat_split
from .history import crowding
from .sources import norm_name, parse_fantasypros, parse_hashtag_adp
from .stats import expected_positive

NEW_COACH = {"NOP", "ORL", "CHI", "DAL", "POR", "MIL"}   # head coaching changes for 2026-27
# break-even share of team games for the baseline holding: a replacement player plus backfill
BASELINE = REPLACEMENT_AVAILABILITY + BACKFILL * (1 - REPLACEMENT_AVAILABILITY)


def _per_game_line(stats):
    g = stats[GP]
    line = {name: round(stats.get(k, 0) / g, 1) for name, k in
            (("pts", PTS), ("reb", REB), ("ast", AST), ("stl", STL), ("blk", BLK), ("tpm", TPM), ("to", TO))}
    line["fg_pct"] = round(stats.get(FGM, 0) / stats[FGA], 3) if stats.get(FGA) else None
    line["ft_pct"] = round(stats.get(FTM, 0) / stats[FTA], 3) if stats.get(FTA) else None
    return line


def fantasypros_fppg(fp, espn_stats, scoring):
    """FantasyPros lists points and shooting percentages but not makes and attempts, which this
    scoring needs. Keep ESPN's mix of field goals to free throws and scale it to FantasyPros' points."""
    g, eg = fp.get("gp"), espn_stats.get(GP)
    if not g or not eg:
        return None
    pts, tpm = fp["pts"] / g, (fp.get("tpm") or 0) / g
    fgm_e, ftm_e = espn_stats.get(FGM, 0) / eg, espn_stats.get(FTM, 0) / eg
    if 2 * fgm_e + ftm_e <= 0:
        return None
    scale = max(0.0, pts - tpm) / (2 * fgm_e + ftm_e)     # PTS = 2*FGM + 3PM + FTM
    fgm, ftm = fgm_e * scale, ftm_e * scale
    fg_pct = fp.get("fg_pct") or (espn_stats.get(FGM, 0) / espn_stats[FGA] if espn_stats.get(FGA) else 0.47)
    ft_pct = fp.get("ft_pct") or (espn_stats.get(FTM, 0) / espn_stats[FTA] if espn_stats.get(FTA) else 0.78)
    line = {PTS: pts, TPM: tpm, FGM: fgm, FGA: fgm / fg_pct if fg_pct else 0, FTM: ftm,
            FTA: ftm / ft_pct if ft_pct else 0, REB: fp["reb"] / g, AST: fp["ast"] / g,
            STL: fp["stl"] / g, BLK: fp["blk"] / g, TO: fp["to"] / g}
    return fantasy_points(line, scoring)


def _latest_bref(bref, key):
    """A player's most recent Basketball Reference season -> (season, record) or (None, None)."""
    for year in sorted(bref, reverse=True):
        if key in bref[year]:
            return int(year), bref[year][key]
    return None, None


def load_intel():
    """Research adjustments keyed by normalized player name."""
    raw = read_json(DATA / "intel.json", {})
    return {norm_name(name): dict(rec, name=name) for name, rec in (raw.get("players") or {}).items()}


def apply_intel(fppg, mpg, rec):
    """A research note moves minutes and per-minute production, never the points directly, and is
    shrunk by how confident the evidence is. -> (fppg, mpg, minutes delta applied, rate applied)."""
    conf = INTEL_CONFIDENCE.get(rec.get("confidence", "medium"), 0.7)
    d_min = max(-INTEL_MAX_MINUTES, min(INTEL_MAX_MINUTES, rec.get("minutes") or 0.0)) * conf
    d_rate = max(-INTEL_MAX_RATE, min(INTEL_MAX_RATE, rec.get("rate") or 0.0)) * conf
    if mpg <= 0:
        return fppg, mpg, 0.0, 0.0
    new_mpg = max(8.0, mpg + d_min)
    return fppg * (new_mpg / mpg) * (1 + d_rate), new_mpg, new_mpg - mpg, d_rate


def break_even(replacement, availability):
    """Per-game production at which holding this player all season matches holding a waiver
    pickup. Players who miss more games have to clear a higher bar, since only part of a missed
    game is made up by a fill-in."""
    return replacement * (BASELINE - BACKFILL * (1 - availability)) / availability


def player_value(fppg, sigma, availability, weighted_games, replacement):
    """Fantasy points above replacement over the season. The per-game projection is uncertain and
    a player who lands below the bar is simply dropped, so the downside is cut off at zero."""
    bar = break_even(replacement, availability)
    return availability * weighted_games * expected_positive(fppg - bar, sigma), bar


def build(verbose=True):
    league = load_league()
    scoring = league["scoring"]
    cal = calibrate.load()
    raw = read_json(RAW / "espn_players.json")
    if raw is None:
        raise SystemExit("data/raw/espn_players.json is missing. Run: python3 -m draftkit refresh")
    schedule = read_json(RAW / "schedule.json", {})
    injuries = {norm_name(k): v for k, v in (read_json(RAW / "injuries.json", {}) or {}).items()}
    news = read_json(RAW / "news.json", {}) or {}
    fpros = parse_fantasypros((RAW / "fantasypros.html").read_text()) if (RAW / "fantasypros.html").exists() else {}
    adp_other = parse_hashtag_adp((RAW / "hashtag_adp.html").read_text()) if (RAW / "hashtag_adp.html").exists() else {}
    bref = read_json(HISTORY / "bref.json", {})
    two_years_ago = {e["player"]["id"]: e["player"]
                     for e in (read_json(HISTORY / ("espn_%d.json" % (SEASON - 2)), {}) or {}).get("players", [])}
    intel = load_intel()

    weeks = season.matchup_weeks(schedule, league["schedule"]["matchup_periods"],
                                 league["schedule"]["final_scoring_period"])
    n_regular = league["schedule"]["regular_season_matchups"]
    week_weight = [1.0 if i < n_regular else PLAYOFF_WEEK_WEIGHT for i in range(len(weeks))]
    opening, season_end = weeks[0][0], weeks[-1][1]

    players = []
    for entry in raw["players"]:
        pl = entry["player"]
        proj = stat_split(pl, "10%d" % SEASON)
        if not proj or proj.get(GP, 0) <= 0:
            continue
        key = norm_name(pl["fullName"])
        team = player_team(pl)
        last = stat_split(pl, "00%d" % (SEASON - 1))
        last2 = stat_split(two_years_ago.get(pl["id"]), "00%d" % (SEASON - 2))
        bref_year, rec = _latest_bref(bref, key)
        age = rec["age"] + (SEASON - bref_year) if rec and rec.get("age") else None
        vgp = last[GP] if last and last.get(GP) else None
        rookie = rec is None and vgp is None
        flags = calibrate.player_flags(age, vgp, bool(rec) and team not in {t["team"] for t in rec["teams"]}, rookie)

        own = pl.get("ownership") or {}
        espn_gp, espn_mpg = proj[GP], proj.get(MIN, 0) / proj[GP]
        espn_fppg = fantasy_points(proj, scoring) / espn_gp
        fp = fpros.get(key)
        fp_fppg = fantasypros_fppg(fp, proj, scoring) if fp else None
        fp_mpg = fp["min"] / fp["gp"] if fp and fp.get("min") else None
        base_fppg = ESPN_WEIGHT * espn_fppg + (1 - ESPN_WEIGHT) * fp_fppg if fp_fppg else espn_fppg
        base_mpg = ESPN_WEIGHT * espn_mpg + (1 - ESPN_WEIGHT) * fp_mpg if fp_mpg else espn_mpg

        players.append({
            "id": pl["id"], "name": pl["fullName"], "key": key, "team": team, "age": age,
            "slots": player_slots(pl, league["slots"]),
            "pos": [s for s in player_slots(pl) if s in ("PG", "SG", "SF", "PF", "C")],
            "injury": pl.get("injuryStatus") or "ACTIVE",
            "espn": {"rank": (pl.get("draftRanksByRankType") or {}).get("STANDARD", {}).get("rank"),
                     "adp": own.get("averageDraftPosition"), "gp": espn_gp, "mpg": round(espn_mpg, 1),
                     "fppg": round(espn_fppg, 1), "total": round(fantasy_points(proj, scoring)),
                     "line": _per_game_line(proj)},
            "fpros": {"gp": fp["gp"], "mpg": round(fp_mpg, 1), "fppg": round(fp_fppg, 1)} if fp_fppg else None,
            "last": {"gp": vgp, "mpg": round(last.get(MIN, 0) / vgp, 1),
                     "fppg": round(fantasy_points(last, scoring) / vgp, 1)} if vgp else None,
            "adp_other": {k: v for k, v in (adp_other.get(key) or {}).items() if k in ("yahoo", "fantrax")} or None,
            "outlook": pl.get("seasonOutlook") or None,
            "news": [n for n in news.get(str(pl["id"])) or [] if n.get("kind") == "note"][:4],
            "articles": [[n["date"], n["headline"]] for n in news.get(str(pl["id"])) or [] if n.get("kind") == "article"][:3],
            "_flags": flags, "_base_fppg": base_fppg, "_base_mpg": base_mpg,
            "_last": (fantasy_points(last, scoring) / vgp if vgp else None, vgp),
            "_last2": ((fantasy_points(last2, scoring) / last2[GP], last2[GP]) if last2 and last2.get(GP) else (None, None)),
            "_espn_total": fantasy_points(proj, scoring),
        })

    # team crowding, defined exactly as in the history it was fitted on: the 200 biggest projections
    pool = sorted(players, key=lambda p: -p["_espn_total"])[:200]
    crowd = crowding({(SEASON, p["id"]): (p["team"] if p["team"] != "FA" else None,
                                          p["espn"]["mpg"] * p["espn"]["gp"] / 82) for p in pool})
    team_crowd = {p["team"]: crowd[(SEASON, p["id"])] for p in pool}

    for p in players:
        flags = p["_flags"]
        z = team_crowd.get(p["team"], 0.0)
        cal_fppg = calibrate.apply_per_game(cal, p["_base_fppg"], p["_last"][0], p["_last"][1],
                                            p["_last2"][0], p["_last2"][1], flags, z)
        note = intel.get(p["key"])
        fppg, mpg, d_min, d_rate = apply_intel(cal_fppg, p["_base_mpg"], note) if note else (cal_fppg, p["_base_mpg"], 0, 0)
        # how ESPN's number became ours, for the player card
        steps = []
        if abs(p["_base_fppg"] - p["espn"]["fppg"]) >= 0.05:
            steps.append(("blend with FantasyPros", p["_base_fppg"] - p["espn"]["fppg"]))
        steps += calibrate.per_game_steps(cal, p["_base_fppg"], p["_last"][0], p["_last"][1],
                                          p["_last2"][0], p["_last2"][1], flags, z)
        if abs(fppg - cal_fppg) >= 0.05:
            steps.append(("research note", fppg - cal_fppg))

        # a known absence: researched date first, else ESPN's injury report when it rules a player out
        out_until, out_source = (note or {}).get("out_until"), "research"
        report = injuries.get(p["key"])
        # a note that already prices the absence into games_rate sets ignore_report, or the date counts twice
        if (not out_until and not (note or {}).get("ignore_report")
                and report and report.get("status") == "Out" and report.get("return_date")):
            if season.parse(report["return_date"][:10]) > opening:
                out_until, out_source = report["return_date"][:10], "ESPN injury report"
        if out_until and season.parse(out_until) <= opening:
            out_until = None

        # share of his team's games he plays once available
        # ESPN's games total usually already nets out a known absence; when it plainly doesn't
        # (a stale projection), its higher number is still the better read of his durability
        healthy_gp = max(72.0, p["espn"]["gp"]) if out_until else p["espn"]["gp"]
        availability = calibrate.apply_games(cal, healthy_gp, flags) / 82.0
        if out_until:
            days_out = (season.parse(out_until) - opening).days
            availability *= (LONG_ABSENCE_FACTOR if days_out >= LONG_ABSENCE_DAYS
                             else SHORT_ABSENCE_FACTOR if days_out >= SHORT_ABSENCE_DAYS else 1.0)
        availability = max(0.15, min(0.92, availability * ((note or {}).get("games_rate") or 1.0)))

        in_schedule = p["team"] in schedule
        by_week = season.games_by_week(schedule, weeks, p["team"], out_until) if in_schedule else [0] * len(weeks)
        full = season.games_by_week(schedule, weeks, p["team"]) if in_schedule else by_week
        team_games = sum(by_week)
        p.update({
            "flags": [name for name, on in flags.items() if on],
            "crowd": round(z, 2),
            "cal_fppg": round(cal_fppg, 2), "fppg": round(fppg, 2), "mpg": round(mpg, 1),
            "steps": [[label, round(delta, 1)] for label, delta in steps],
            "sigma": round(calibrate.apply_sigma(cal, flags) * (1.15 if out_until else 1.0), 2),
            "availability": round(availability, 3),
            "out_until": out_until, "out_source": out_source if out_until else None,
            "games": round(availability * team_games, 1),
            "games_playoffs": round(availability * sum(by_week[n_regular:]), 1),
            "_weighted_games": sum(w * g for w, g in zip(week_weight, by_week)),
            # weighted team games he sits out before returning; free while an IR slot holds him
            "absent_games": round(sum(w * (f - g) for w, f, g in zip(week_weight, full, by_week)), 1),
            "intel": dict(note, applied_minutes=round(d_min, 1), applied_rate=round(d_rate, 3)) if note else None,
            "injury_report": report,
        })

    # replacement level: the best player nobody rosters, among those who are actually available
    healthy = sorted((p["fppg"] for p in players if not p["out_until"]), reverse=True)
    roster = sum(n for slot, n in league["slots"].items() if slot != "IR")
    idx = min(len(healthy) - 1, league["size"] * roster + REPLACEMENT_RANK_PAD - 1)
    replacement = healthy[idx]

    for p in players:
        value, bar = player_value(p["fppg"], p["sigma"], p["availability"], p.pop("_weighted_games"), replacement)
        p["value"], p["bar"] = round(value, 1), round(bar, 1)
        if p["team"] in NEW_COACH:
            p["flags"].append("new_coach")
        if p["out_until"]:
            p["flags"].append("out")
        if p["last"] and p["espn"]["fppg"] >= 1.10 * p["last"]["fppg"]:
            p["flags"].append("espn_jump")
        for k in [k for k in p if k.startswith("_")]:
            del p[k]

    players.sort(key=lambda p: -p["value"])
    by_adp = sorted(players, key=lambda p: (p["espn"]["adp"] or 999, p["espn"]["rank"] or 999))
    for i, p in enumerate(by_adp, 1):
        p["market"] = i                                   # the order the room is expected to follow
    for i, p in enumerate(players, 1):
        p["rank"] = i
    _tiers(players)

    board = {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "season": SEASON, "replacement": round(replacement, 2),
        "weeks": [[str(a), str(b)] for a, b in weeks], "regular_weeks": n_regular,
        "team_crowd": {t: round(z, 2) for t, z in sorted(team_crowd.items())},
        "players": players,
    }
    write_json(DATA / "board.json", board)
    if verbose:
        report_board(board)
    return board


def _tiers(players, top=170):
    """Mark tier breaks where the drop to the next player is large for that part of the board."""
    tier = 1
    for i, p in enumerate(players):
        p["tier"] = tier
        if i + 1 >= len(players) or i >= top:
            continue
        gap = p["value"] - players[i + 1]["value"]
        window = [players[j]["value"] - players[j + 1]["value"]
                  for j in range(max(0, i - 6), min(len(players) - 1, i + 7))]
        typical = sorted(window)[len(window) // 2]
        if gap > max(2.2 * typical, 0.035 * max(p["value"], 1.0)) and p["value"] > 0:
            tier += 1


def report_board(board, n=60):
    ps = board["players"]
    print("board built %s: %d players, replacement level %.1f FPPG" % (board["generated"], len(ps), board["replacement"]))
    print("%4s %-24s %-4s %5s %6s %6s %6s %5s %5s %7s %5s %5s  %s" % (
        "rank", "player", "team", "tier", "ESPN", "ours", "sd", "avail", "games", "value", "ADP", "mkt", "flags"))
    for p in ps[:n]:
        print("%4d %-24s %-4s %5d %6.1f %6.1f %6.1f %5.2f %5.1f %7.0f %5.1f %5d  %s%s" % (
            p["rank"], p["name"][:24], p["team"], p["tier"], p["espn"]["fppg"], p["fppg"], p["sigma"], p["availability"],
            p["games"], p["value"], p["espn"]["adp"] or 0, p["market"], ",".join(p["flags"]),
            "  [out until %s]" % p["out_until"] if p["out_until"] else ""))


def load():
    board = read_json(DATA / "board.json")
    if board is None:
        board = build(verbose=False)
    return board
