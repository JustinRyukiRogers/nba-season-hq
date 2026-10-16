"""Fit everything that is learned from history and store it in data/calibration.json.

  per_game   systematic corrections to ESPN's per-game projection
  games      what a projected games total really turns into
  sigma      how uncertain a per-game projection is, by player type
  room       how this league's managers pick relative to ESPN's list

    python3 -m draftkit fit
"""
import math
import statistics as st

from .config import DATA, read_json, write_json
from .history import SEASONS, build_rows, espn_list, load_bref, load_espn
from .sources import norm_name
from .stats import ols, predict, r_squared, rmse, spearman

PER_GAME_TERMS = ["const", "espn_projection", "prior_minus_projection", "age_28_plus", "age_24_or_under",
                  "new_team", "under_50_games_last_year", "rookie", "team_crowding_z"]
# Tried and left out: softening the three risk switches (age 28+, new team, short last season) for
# stars who carry two or more. Eleven such players beat the plain sum by 2.7 points a game, but
# adding a term for them changed neither out-of-sample accuracy (RMSE 5.20 either way) nor the
# replayed drafts (-0.8 +/- 1.1 points). A view on one star belongs in data/intel.json instead.
GAMES_TERMS = ["const", "projected_games", "new_team", "under_50_games_last_year", "projected_games_below_66"]
LOW_GAMES = 66    # below this, a projection signals a known problem, and those run long
# stages of the draft by overall pick; the room hugs ESPN's list at the very top and loosens from there
PICK_BUCKETS = [(1, 6), (7, 14), (15, 24), (25, 60), (61, 108), (109, 999)]
MAX_K = 80


# ---------- per-game ----------

def prior_gap(pfppg, vfppg, vgp, v2fppg, v2gp):
    """How far a games-weighted two-year track record sits from the projection, shrunk when the
    record is thin (two full seasons = full weight; last year counts double per game)."""
    num = den = 0.0
    if vfppg is not None and vgp:
        num, den = num + 2 * vgp * vfppg, den + 2 * vgp
    if v2fppg is not None and v2gp:
        num, den = num + v2gp * v2fppg, den + v2gp
    if den <= 0:
        return 0.0
    return min(1.0, den / 164.0) * (num / den - pfppg)


def player_flags(age, vgp, changed, rookie):
    """The player-type switches every model here shares."""
    return {"old": bool(age is not None and age >= 28),
            "young": bool(rookie or (age is not None and age <= 24)),
            "new_team": bool(changed) and not rookie,
            "inj_return": (not rookie) and (vgp or 0) < 50,
            "rookie": bool(rookie)}


def per_game_features(pfppg, vfppg, vgp, v2fppg, v2gp, flags, crowd=0.0):
    return [1.0, pfppg, prior_gap(pfppg, vfppg, vgp, v2fppg, v2gp), float(flags["old"]), float(flags["young"]),
            float(flags["new_team"]), float(flags["inj_return"]), float(flags["rookie"]), crowd]


def _fit_coef(rows):
    return ols([_row_features(r) for r in rows], [r["afppg"] for r in rows])


def games_features(pgp, flags):
    return [1.0, pgp, float(flags["new_team"]), float(flags["inj_return"]), max(0.0, LOW_GAMES - pgp)]


def sigma_features(flags):
    return [1.0, float(flags["rookie"]), float(flags["new_team"]), float(flags["young"] and not flags["rookie"])]


def _row_flags(r):
    return player_flags(r["age"], r["vgp"], r["changed"], r["rookie"])


def _row_features(r):
    return per_game_features(r["pfppg"], r["vfppg"], r["vgp"], r["v2fppg"], r["v2gp"], _row_flags(r), r["crowd"])


def fit_per_game(rows):
    played = [r for r in rows if r["agp"] >= 20]
    coef = _fit_coef(played)
    # honest accuracy: predict each season from the other four
    pred, corr_raw, corr_cal = {}, [], []
    for test in sorted({r["y"] for r in played}):
        train = [r for r in played if r["y"] != test]
        c = _fit_coef(train)
        held = [r for r in played if r["y"] == test]
        for r in held:
            pred[id(r)] = predict(c, _row_features(r))
        corr_raw.append(spearman([r["pfppg"] for r in held], [r["afppg"] for r in held]))
        corr_cal.append(spearman([pred[id(r)] for r in held], [r["afppg"] for r in held]))
    resid = [(r, r["afppg"] - pred[id(r)]) for r in played]
    # spread of the misses by player type: regress squared out-of-sample error on the type switches
    var = ols([sigma_features(_row_flags(r)) for r, _ in resid], [e * e for _, e in resid])
    var = [var[0]] + [max(0.0, v) for v in var[1:]]
    return {
        "terms": PER_GAME_TERMS, "coef": coef, "n": len(played),
        "sigma_terms": ["base", "rookie", "new_team", "young_non_rookie"], "sigma_var": var,
        "out_of_sample": {"rmse_espn": rmse(r["afppg"] - r["pfppg"] for r in played),
                          "rmse_calibrated": rmse(e for _, e in resid),
                          "rank_corr_espn": st.mean(corr_raw), "rank_corr_calibrated": st.mean(corr_cal)},
    }


def fit_games(rows):
    X = [games_features(r["pgp"], _row_flags(r)) for r in rows]
    y = [r["agp"] for r in rows]
    coef = ols(X, y)
    under50 = lambda rs: sum(r["agp"] < 50 for r in rs) / max(1, len(rs))
    return {
        "terms": GAMES_TERMS, "coef": coef, "n": len(rows),
        "r2": r_squared([predict(coef, x) for x in X], y),
        "mean_projected": st.mean(r["pgp"] for r in rows), "mean_actual": st.mean(y),
        "p_under_50": {"inj_return": under50([r for r in rows if _row_flags(r)["inj_return"]]),
                       "other": under50([r for r in rows if not _row_flags(r)["inj_return"]])},
    }


# ---------- the room ----------

def league_picks():
    """Past drafts of this league -> {season: [(manager's current team id or 0, player id), ...]}."""
    hist = read_json(DATA / "league_history.json", {})
    return {int(y): [tuple(int(v) for v in p.split(".")) for p in s.split(",")]
            for y, s in hist.items() if y.isdigit()}


def bucket_of(overall, buckets=PICK_BUCKETS):
    for i, (lo, hi) in enumerate(buckets):
        if lo <= overall <= hi:
            return i
    return len(buckets) - 1


def overdue(overall, adp, params):
    """Extra pull toward a player who has slid past where the room expected him to go.

    The pick distributions are measured against ESPN's list, which understates how firmly a room
    agrees at the top of a draft: left alone, the model lets a top-three player last into the
    second round. The strength of the pull is tuned so that simulated drafts reproduce ESPN's real
    average draft positions (engine.tune_room), and it fades out as the draft goes on."""
    sigma0, sigma1, fade = params[:3]
    late = overall - adp - 1
    taper = 1 - (overall - 1) / fade
    if late <= 0 or taper <= 0:
        return 1.0
    return math.exp(min(6.0, late / (sigma0 + sigma1 * adp)) * taper)


def sharpness(overall, params):
    """Exponent on the pick distribution: above 1 early on, when the room sticks closer to the top
    of the list than the league's small sample of first-round picks suggests; 1 once it has faded."""
    fade, extra = params[2], (params[3] if len(params) > 3 else 0.0)
    return 1.0 + extra * max(0.0, 1 - (overall - 1) / fade)


def _smooth(ks):
    """Observed 'k-th best name left on ESPN's list' picks -> a smooth pmf over k = 1..MAX_K."""
    pmf = [0.0] * (MAX_K + 1)
    for obs in ks:
        width = 0.5 + 0.25 * obs
        for k in range(1, MAX_K + 1):
            pmf[k] += math.exp(-abs(k - obs) / width) / width
    total = sum(pmf)
    return [p / total for p in pmf]


def fit_room():
    """How far down ESPN's list this league reaches, by stage of the draft, and how much more
    likely a manager is to take a player they have drafted before."""
    picks = league_picks()
    if not picks:
        return None
    ks = [[] for _ in PICK_BUCKETS]
    events = []    # (season, team, bucket, [remaining ids in list order], chosen index)
    for season, plist in sorted(picks.items()):
        if not load_espn(season):
            continue
        remaining = espn_list(season)
        for i, (team, pid) in enumerate(plist):
            if pid not in remaining:
                continue
            idx = remaining.index(pid)
            b = bucket_of(i + 1)
            ks[b].append(idx + 1)
            events.append((season, team, b, remaining[:MAX_K], idx))
            remaining.pop(idx)
    pmfs = [_smooth(k) for k in ks]

    # which players and NBA teams each current manager has taken before
    owned = {}
    for season, plist in picks.items():
        for team, pid in plist:
            if team:
                owned.setdefault(team, {}).setdefault(pid, []).append(season)

    def loglik(boost):
        total = 0.0
        for season, team, b, window, idx in events:
            before = {pid for pid, seasons in owned.get(team, {}).items() if min(seasons) < season}
            if not team or not before or idx >= len(window):
                continue
            w = [pmfs[b][k + 1] * (boost if pid in before else 1.0) for k, pid in enumerate(window)]
            total += math.log(w[idx] / sum(w))
        return total

    grid = [1 + 0.25 * i for i in range(0, 37)]
    boost = max(grid, key=loglik)

    bref = load_bref()
    fan = {}
    for team, players in owned.items():
        counts = {}
        for pid, seasons in players.items():
            for season in seasons:
                pl = load_espn(season).get(pid)
                rec = bref.get(str(season), {}).get(norm_name(pl["fullName"])) if pl else None
                if rec and rec["teams"]:
                    counts[rec["teams"][0]["team"]] = counts.get(rec["teams"][0]["team"], 0) + 1
        total = sum(counts.values())
        if total:
            nba, n = max(counts.items(), key=lambda kv: kv[1])
            if n >= 4 and n / total >= 0.10:   # a league has 30 teams, so 10%+ is three times chance
                fan[str(team)] = {"team": nba, "picks": n, "of": total}

    return {
        "seasons": sorted(picks), "buckets": PICK_BUCKETS, "pmf": pmfs,
        "median_k": [st.median(k) for k in ks], "n": [len(k) for k in ks],
        "overdue": (read_json(DATA / "calibration.json", {}).get("room") or {}).get("overdue"),   # set by engine.tune_room
        "repeat_boost": boost,
        "repeat_boost_loglik_gain": loglik(boost) - loglik(1.0),
        "owned": {str(t): sorted(p) for t, p in owned.items()},
        "fan": fan,
    }


def fit(verbose=True):
    rows = build_rows(pool_size=200)
    cal = {"seasons": SEASONS, "per_game": fit_per_game(rows), "games": fit_games(rows),
           "room": fit_room()}
    write_json(DATA / "calibration.json", cal, indent=1)
    if verbose:
        pg, gm, room = cal["per_game"], cal["games"], cal["room"]
        print("per-game model (n=%d)" % pg["n"])
        for term, c in zip(pg["terms"], pg["coef"]):
            print("   %-28s %+.3f" % (term, c))
        o = pg["out_of_sample"]
        print("   out of sample: RMSE %.2f -> %.2f, rank corr %.3f -> %.3f" % (
            o["rmse_espn"], o["rmse_calibrated"], o["rank_corr_espn"], o["rank_corr_calibrated"]))
        print("   per-game sd: " + ", ".join("%s %.1f" % (t, math.sqrt(sum(pg["sigma_var"][:1]) + (v if i else 0)))
                                             for i, (t, v) in enumerate(zip(pg["sigma_terms"], pg["sigma_var"]))))
        print("games model (n=%d, R2 %.3f): projected %.1f -> actual %.1f" % (
            gm["n"], gm["r2"], gm["mean_projected"], gm["mean_actual"]))
        for term, c in zip(gm["terms"], gm["coef"]):
            print("   %-28s %+.3f" % (term, c))
        print("   chance of under 50 games: %.0f%% after an under-50 season, %.0f%% otherwise" % (
            100 * gm["p_under_50"]["inj_return"], 100 * gm["p_under_50"]["other"]))
        if room:
            print("room model from drafts %s" % room["seasons"])
            for (lo, hi), med, n in zip(room["buckets"], room["median_k"], room["n"]):
                print("   picks %d-%s: median pick is the %s-best name left on ESPN's list (n=%d)" % (
                    lo, hi if hi < 999 else "end", ("%g" % med), n))
            print("   a manager is %.2fx as likely to take a player they drafted before (log-lik gain %.1f)" % (
                room["repeat_boost"], room["repeat_boost_loglik_gain"]))
            print("   fan leanings: %s" % (room["fan"] or "none clear"))
    return cal


def load():
    cal = read_json(DATA / "calibration.json")
    if cal is None:
        cal = fit(verbose=False)
    return cal


def apply_per_game(cal, pfppg, vfppg, vgp, v2fppg, v2gp, flags, crowd=0.0):
    return predict(cal["per_game"]["coef"], per_game_features(pfppg, vfppg, vgp, v2fppg, v2gp, flags, crowd))


def per_game_steps(cal, pfppg, vfppg, vgp, v2fppg, v2gp, flags, crowd=0.0):
    """The same prediction as apply_per_game, itemised: [(what, change in points per game)]."""
    coef = cal["per_game"]["coef"]
    x = per_game_features(pfppg, vfppg, vgp, v2fppg, v2gp, flags, crowd)
    labels = [None, None, "track record vs. projection", "age 28 or older", "age 24 or younger", "new team",
              "under 50 games last season", "rookie", "crowded team" if crowd > 0 else "thin team"]
    steps = [("projections run high across the board", coef[0] + (coef[1] - 1) * pfppg)]
    steps += [(label, c * v) for label, c, v in zip(labels[2:], coef[2:], x[2:]) if abs(c * v) >= 0.05]
    return steps


def apply_games(cal, pgp, flags):
    """Expected games out of 82 for a player ESPN projects for `pgp`."""
    return max(5.0, min(80.0, predict(cal["games"]["coef"], games_features(pgp, flags))))


def apply_sigma(cal, flags):
    return math.sqrt(max(4.0, predict(cal["per_game"]["sigma_var"], sigma_features(flags))))
