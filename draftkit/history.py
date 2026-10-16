"""Past seasons as (player, season) rows: what ESPN projected in the preseason and what happened.

Both the calibration fit and the research reports are built on these rows.

Which seasons are usable:
  * 2023's stored projection is a mid-season rest-of-season snapshot, and 2020/2021 were short seasons.
  * ESPN's stored ADP for past seasons keeps drifting with in-season drafts (injured players slide),
    so it is not a clean preseason price and is not used.
"""
from .config import DEFAULT_SCORING, GP, HISTORY, MIN, read_json
from .espn import fantasy_points, stat_split
from .sources import norm_name

SEASONS = [2019, 2022, 2024, 2025, 2026]
POOL = 150
_cache = {}


def load_espn(year):
    """{player id: raw ESPN player} for a past season."""
    if year not in _cache:
        data = read_json(HISTORY / ("espn_%d.json" % year))
        _cache[year] = {e["player"]["id"]: e["player"] for e in data["players"]} if data else {}
    return _cache[year]


def load_bref():
    if "bref" not in _cache:
        _cache["bref"] = read_json(HISTORY / "bref.json", {})
    return _cache["bref"]


def espn_list(year):
    """ESPN's draft list for a season: player ids ordered by projected season fantasy points.
    (For the current season this matches ESPN's own points-league rank exactly.)"""
    proj = {pid: fantasy_points(pr) for pid, pl in load_espn(year).items()
            if (pr := stat_split(pl, "10%d" % year)) and pr.get(GP, 0) > 0}
    return sorted(proj, key=lambda pid: -proj[pid])


def _rate(stats):
    if not stats or stats.get(GP, 0) <= 0:
        return None, None, None
    g = stats[GP]
    return g, fantasy_points(stats) / g, stats.get(MIN, 0) / g


def build_rows(pool_size=POOL, seasons=SEASONS, scoring=DEFAULT_SCORING):
    """One row per (season, player) for the top `pool_size` projected players of each season."""
    bref = load_bref()
    rows = []
    for y in seasons:
        espn = load_espn(y)
        pool = [(fantasy_points(pr, scoring), pid, pl, pr) for pid, pl in espn.items()
                if (pr := stat_split(pl, "10%d" % y)) and pr.get(GP, 0) > 0]
        pool.sort(key=lambda t: -t[0])
        for rank, (ptot, pid, pl, pr) in enumerate(pool[:pool_size], 1):
            act = stat_split(pl, "00%d" % y)
            prev = stat_split(load_espn(y - 1).get(pid), "00%d" % (y - 1))
            prev2 = stat_split(load_espn(y - 2).get(pid), "00%d" % (y - 2))
            key = norm_name(pl["fullName"])
            b, b_prev = bref.get(str(y), {}).get(key), bref.get(str(y - 1), {}).get(key)
            age = b["age"] if b else next(
                (bb["age"] + y - int(yy) for yy, s in bref.items() if (bb := s.get(key)) and bb["age"]), None)
            first = min((int(yy) for yy, s in bref.items() if key in s), default=None)
            agp, afppg, ampg = _rate(act)
            vgp, vfppg, vmpg = _rate(prev)
            v2gp, v2fppg, _ = _rate(prev2)
            teams, teams_prev = ([t["team"] for t in x["teams"]] if x else None for x in (b, b_prev))
            rookie = first == y and (age or 99) <= 23 and vgp is None
            rows.append({
                "y": y, "id": pid, "name": pl["fullName"], "prank": rank, "age": age,
                "pgp": pr[GP], "ptot": ptot, "pfppg": ptot / pr[GP], "pmpg": pr.get(MIN, 0) / pr[GP],
                "agp": agp or 0, "atot": fantasy_points(act, scoring) if act else 0, "afppg": afppg, "ampg": ampg,
                "vgp": vgp, "vfppg": vfppg, "vmpg": vmpg, "v2gp": v2gp, "v2fppg": v2fppg,
                # on a team he did not play for the season before; None when either season is missing
                "changed": None if not (teams and teams_prev) else not (set(teams) & set(teams_prev)),
                "traded_midseason": len(teams) > 1 if teams else None,
                "team": teams[0] if teams else None,
                "rookie": rookie,
                "yr2": first == y - 1 and (age or 99) <= 24,
                "yr3": first == y - 2 and (age or 99) <= 25,
                "pstats": pr, "astats": act,
            })
    crowd = crowding({(r["y"], r["id"]): (r["team"], r["pmpg"] * r["pgp"] / 82) for r in rows})
    for r in rows:
        r["crowd"] = crowd[(r["y"], r["id"])]
    return rows


def crowding(booked, top=9):
    """How crowded each player's team is, as a z-score within its season.

    `booked` maps (season, player) -> (team, minutes per team game ESPN projects for him). A team's
    load is the sum over its nine biggest projected players; ESPN hands some teams far more minutes
    than a team has, and their players tend to fall short. Players without a team get 0."""
    load = {}
    for (season, _), (team, minutes) in booked.items():
        if team:
            load.setdefault((season, team), []).append(minutes)
    load = {k: sum(sorted(v, reverse=True)[:top]) for k, v in load.items()}
    out = {}
    for season in {k[0] for k in load}:
        vals = [v for k, v in load.items() if k[0] == season]
        mean = sum(vals) / len(vals)
        sd = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5 or 1.0
        for (s, team), v in load.items():
            if s == season:
                out[(s, team)] = max(-2.0, min(2.0, (v - mean) / sd))
    return {key: out.get((key[0], team), 0.0) if team else 0.0 for key, (team, _) in booked.items()}
