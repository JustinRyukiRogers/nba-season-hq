"""After a short absence, how often does a player play the following week?

The HQ (seasonkit/model.game_odds) gives a player tagged Out with a return date his season-long
durability share (`back_share`, e.g. Luka 0.765) for every game after the return, while a healthy
player gets NEAR_TERM_PLAY 0.93 for his next 7 days. Question (2026-11-09, Luka out one game with
back tightness): after a short absence, which number fits better?

Events: a regular (played 60%+ of his team's games so far, 20+ minutes a game, 10+ team games in)
tagged Out on the NBA's official 5 PM ET report for a game he then missed, having missed only 1-3
games in a row (counting that one) after playing before. The outcome is the share of his team's games
in the 7 days after that game that he played. Nothing here knows ESPN's return date, so absences that
turn out long are included: this is the uncertainty a return date carries too. A healthy baseline uses
the same players on days they played and were untagged.

Scored as squared error against the outcome for: the HQ's back_share (model.durability with
after_layoff, ESPN's preseason games, no calibration flags), the flat 0.93, and max(back_share, 0.93).

Result (2026-11-09): 0.93 is wrong; not shipped. Healthy regulars play 88-91% of the next week. After a
1-3 game Out stretch they play 48% (2024-25, n 718) and 39% (2025-26, n 299); after missing just one,
57% / 53%; front end of a back-to-back, 53% / 60% (n 70 / 27). MSE, HQ share vs 0.93: all 0.25 vs 0.37
and 0.32 vs 0.47, worse for 0.93 in every slice. A third play every game, a third to a half none. The
HQ's ~0.77 is itself high against these rates, but the events don't know ESPN's return date (a dated
return should play more often), and game-day report tags (TAG_PLAY) override it, so it stays.

    python3 research/short_absence.py
"""
import json, re, sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "research"))
from minutes_backtest import load_espn, stat_split, GP  # noqa: E402
from draftkit import calibrate  # noqa: E402
import seasonkit.model as M  # noqa: E402

H = ROOT / "data" / "history"
cal = calibrate.load()


def norm(name):
    name = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", name.lower())
    return re.sub(r"[^a-z]", "", name)


def run(year):
    logs = json.loads((H / ("gamelogs_%d.json" % year)).read_text())
    reps = json.loads((H / ("injury_reports_%d.json" % year)).read_text())
    espn = load_espn(year)
    gp = {}
    for pid, pl in espn.items():
        pr = stat_split(pl, "10%d" % year)
        if pr and pr.get(GP, 0) > 0:
            gp[str(pid)] = pr[GP]
    data = json.loads((H / ("espn_%d.json" % year)).read_text())
    ids = {norm(p["player"]["fullName"]): str(p["id"]) for p in data["players"]}
    played = {}          # pid -> {date: minutes}
    team_of = {}         # pid -> {date: team}
    team_days = defaultdict(set)
    for pid, rows in logs.items():
        for r in rows:
            dd = date.fromisoformat(r[0])
            team_days[r[2]].add(dd)
            team_of.setdefault(pid, {})[dd] = r[2]
            if r[3] > 0:
                played.setdefault(pid, {})[dd] = r[3]
    team_days = {t: sorted(v) for t, v in team_days.items()}
    tagged = defaultdict(dict)   # date -> {pid: tag} (5 PM report)
    for key, rep in reps.items():
        ds, h = key.split()
        if h != "5" or not rep:
            continue
        for nm, tag in rep.items():
            if nm in ids:
                tagged[date.fromisoformat(ds)][ids[nm]] = tag
    events, healthy = [], []
    for pid, pmap in played.items():
        if pid not in gp:
            continue
        teams = team_of[pid]
        share_hq = M.durability(cal, gp[pid], [], after_layoff=True)
        for team in set(teams.values()):
            tdays = team_days[team]
            for i, dd in enumerate(tdays):
                if dd not in tagged or i < 10:
                    continue
                before = tdays[:i]
                n_played = sum(1 for x in before if x in pmap)
                if n_played < 0.6 * len(before) or n_played == 0:
                    continue
                recent = [pmap[x] for x in before if x in pmap][-10:]
                if sum(recent) / len(recent) < 20:
                    continue
                if dd > max(teams) or dd < min(teams):
                    continue
                after = [x for x in tdays[i + 1:] if x <= dd + timedelta(days=7)]
                if not after:
                    continue
                y = sum(1 for x in after if x in pmap) / len(after)
                tag = tagged[dd].get(pid)
                if tag == "Out" and dd not in pmap:
                    streak = 1
                    for x in reversed(before):
                        if x in pmap:
                            break
                        streak += 1
                    if streak <= 3 and before[-streak:] and any(x in pmap for x in before):
                        events.append((y, share_hq, streak, len(after), pid, dd))
                elif tag is None and dd in pmap:
                    healthy.append((y, share_hq))
    return events, healthy


def mse(rows, f):
    return sum((f(r) - r[0]) ** 2 for r in rows) / len(rows)


def main():
    for year in (2025, 2026):
        ev, hl = run(year)
        print("\n%d-%02d season: %d short-absence events, %d healthy player-days" % (year - 1, year % 100, len(ev), len(hl)))
        print("  healthy: played %.1f%% of next-7-day games (HQ share avg %.3f)" %
              (100 * sum(r[0] for r in hl) / len(hl), sum(r[1] for r in hl) / len(hl)))
        for label, rows in (("all 1-3", ev), ("missed 1", [r for r in ev if r[2] == 1]),
                            ("missed 2-3", [r for r in ev if r[2] > 1])):
            if not rows:
                continue
            print("  %-10s n %4d  played %.1f%% next 7 days | MSE: HQ share %.4f (avg %.3f)  0.93 %.4f  max %.4f  best const %.3f" % (
                label, len(rows), 100 * sum(r[0] for r in rows) / len(rows),
                mse(rows, lambda r: r[1]), sum(r[1] for r in rows) / len(rows),
                mse(rows, lambda r: 0.93), mse(rows, lambda r: max(r[1], 0.93)),
                sum(r[0] for r in rows) / len(rows)))
        # how the outcome spreads: returned fully / partly / not at all
        full = sum(1 for r in ev if r[0] == 1); none = sum(1 for r in ev if r[0] == 0)
        print("  spread: played every game %d%%, none %d%%, some %d%%" % (
            100 * full / len(ev), 100 * none / len(ev), 100 * (len(ev) - full - none) / len(ev)))


if __name__ == "__main__":
    main()
