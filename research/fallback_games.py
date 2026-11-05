"""Share of games for players with no ESPN preseason projection who played last season.

Compares the HQ's flat 50-game baseline, last season's games, and blends with the share of team games
he has played so far this season (weight k team games), scored as squared error on the share of the
team's later games he played. 2024-25 and 2025-26, checkpoints 10-45 days in.

Result (2026-11-05): last season's games alone did worse than 50 everywhere; 50 blended with this
season's share at k 10 was never worse than 50 and cut the error by half or more for players averaging
15+. Shipped as model.FALLBACK_SHARE_GAMES 10. Samples are small (7-31 players a checkpoint).

    python3 research/fallback_games.py
"""
import json, sys
from datetime import timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'research'))
from minutes_backtest import Season, d
from draftkit import calibrate
cal = calibrate.load()
H = str(ROOT / 'data' / 'history') + '/'

def last_gp(year):
    p = H + 'gamelogs_%d.json' % (year - 1)
    try:
        logs = json.load(open(p))
        return {pid: sum(1 for r in rows if r[3] > 0) for pid, rows in logs.items()}
    except FileNotFoundError:
        return None

import seasonkit.model as M
def share(gp):
    return M.durability(cal, gp, [])

res = {}
for year in (2025, 2026):
    S = Season(year); lg = last_gp(year)
    for days in (10, 16, 23, 30, 45):
        cut = S.start + timedelta(days=days)
        for pid, g in S.games.items():
            if pid in S.prior or pid not in lg:  # only players with no ESPN projection who played last season
                continue
            before = [x for x in g if x[0] < cut]
            if len(before) < 3:
                continue
            team = before[-1][2]
            tg_before = [x for x in S.team_games[team] if x[0] < cut]
            tg_after = [x for x in S.team_games[team] if x[0] >= cut]
            if not tg_after:
                continue
            after = sum(1 for x in g if x[0] >= cut)
            fppg = sum(x[4] for x in before) / len(before)
            y = min(1.0, after / len(tg_after))
            part = len(before) / max(1, len(tg_before))
            res.setdefault((year, days), []).append((y, lg[pid], part, len(tg_before), fppg))

def mse(rows, f):
    return sum((f(r) - r[0]) ** 2 for r in rows) / len(rows)

models = {
    '50 + season k5': lambda r: (r[3] * min(0.92, r[2]) + 5 * share(50.0)) / (r[3] + 5),
    '50 + season k10': lambda r: (r[3] * min(0.92, r[2]) + 10 * share(50.0)) / (r[3] + 10),
    'last + season k5': lambda r: (r[3] * min(0.92, r[2]) + 5 * share(float(r[1]))) / (r[3] + 5),
    'season only': lambda r: min(0.92, r[2]),
    'current 50': lambda r: share(50.0),
    'last gp': lambda r: share(float(r[1])),
    'last gp + this season k10': lambda r: (r[3] * min(0.92, r[2]) + 10 * share(float(r[1]))) / (r[3] + 10),
    'last gp + this season k20': lambda r: (r[3] * min(0.92, r[2]) + 20 * share(float(r[1]))) / (r[3] + 20),
}
for key, rows in sorted(res.items()):
    for sub, rr in (('all', rows), ('15+ fppg', [r for r in rows if r[4] >= 15])):
        if not rr: continue
        print(key, sub, 'n=%d' % len(rr), 'actual mean %.3f' % (sum(r[0] for r in rr) / len(rr)),
              '\n   '+' | '.join('%s %.4f' % (m, mse(rr, f)) for m, f in models.items()))
