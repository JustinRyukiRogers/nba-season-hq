"""The fantasy calendar: which NBA dates fall in which weekly matchup, and how many games each
team plays in each one."""
from datetime import date, timedelta


def parse(day):
    return date(*map(int, day.split("-")))


def longest_gap(played, lo, hi):
    """Longest run of dates in [lo, hi] with no games -> (first date, length in days)."""
    best, best_len, run = None, 0, None
    d = lo
    while d <= hi:
        if d not in played:
            run = run or d
            if (d - run).days + 1 > best_len:
                best, best_len = run, (d - run).days + 1
        else:
            run = None
        d += timedelta(days=1)
    return best, best_len


def matchup_weeks(schedule, periods, final_day=None):
    """-> [(first date, last date)] for each matchup period.

    Week 1 runs from opening night to the first Sunday, then Monday-Sunday weeks. The two calendar
    weeks around the All-Star break form one matchup, which is how ESPN schedules it."""
    played = {parse(d) for dates in schedule.values() for d in dates}
    start = min(played)
    break_start, break_len = longest_gap(played, date(start.year + 1, 2, 1), date(start.year + 1, 3, 1))
    weeks, first = [], start
    while len(weeks) < periods:
        last = first + timedelta(days=6 - first.weekday())                # through Sunday
        if break_start and break_len >= 4 and first <= break_start <= last:  # swallow the following week too
            last += timedelta(days=7)
        weeks.append((first, last))
        first = last + timedelta(days=1)
    if final_day and (weeks[-1][1] - start).days + 1 != final_day:
        raise ValueError("calendar ends on day %d but ESPN says the fantasy season is %d days long"
                         % ((weeks[-1][1] - start).days + 1, final_day))
    return weeks


def cup_week(schedule, weeks):
    """Index of the matchup week holding the NBA Cup knockout rounds, whose games are not on the
    schedule until the groups are decided. Every team still plays its full 82, so the unscheduled
    games are counted in this week."""
    played = {parse(d) for dates in schedule.values() for d in dates}
    start = min(played)
    gap, length = longest_gap(played, date(start.year, 12, 1), date(start.year, 12, 22))
    if not gap or length < 2:
        return None
    middle = gap + timedelta(days=length // 2)
    return next((i for i, (lo, hi) in enumerate(weeks) if lo <= middle <= hi), None)


def games_by_week(schedule, weeks, team, out_until=None, full_season=82):
    """Games in each matchup week for `team`, counting only games on or after `out_until`."""
    cutoff = parse(out_until) if out_until else None
    days = [parse(d) for d in schedule.get(team, [])]
    counts = [sum(1 for d in days if lo <= d <= hi and (cutoff is None or d >= cutoff)) for lo, hi in weeks]
    unscheduled = full_season - len(days)
    cup = cup_week(schedule, weeks)
    if unscheduled > 0 and cup is not None and (cutoff is None or cutoff <= weeks[cup][1]):
        counts[cup] += unscheduled
    return counts
