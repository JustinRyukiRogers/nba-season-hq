---
name: judgement-call
description: Sweep every outstanding Season HQ suggestion (to-dos, upgrades, IR stashes, streams, hunches, week-plan moves, trade ideas, the league's trades) and give an analyst's judgment on each, using the context the stats model can't see (minutes and role, who a player is covering for and when they return, injury news, depth charts, ownership), then record the calls as analyst takes on the HQ. Use when the user says "judgement call", "judgment call", or asks for a sweep or analyst view of the HQ's suggestions.
---

# Judgement call

The HQ's numbers are the tested model. A judgement call is the analyst layer beside it: for each thing the
HQ suggests, say whether you agree and why, using what the model cannot see. Takes are shown next to the
numbers on the site and never change them.

## 1. Refresh and gather

```
python3 -m seasonkit refresh          # ~10 s; prints the to-dos
python3 .claude/skills/judgement-call/sweep.py
```

`sweep.py` lists every outstanding suggestion with, for each player involved: the HQ's number and how it is
built (minutes x rate, preseason prior), last games' minutes and points, depth-chart spot, teammates not
active (with return dates and minutes), the injury report, ownership trend, latest news, and any take
already on him. It ends with the existing takes, marked live or expired.

## 2. Check what the model can't see

For each suggestion, look for:

- **Role behind the minutes.** Is the player's recent run (or the projection the HQ leans on) caused by a
  teammate being out? Who returns when, and what did he play with that teammate healthy? Coach's-decision
  DNPs are not in the game log, so a player who sat looks like he has no games yet.
- **Who is taking minutes.** A player ESPN never projected (no proj_mpg) can be the reason a rostered
  player's minutes fell; the board cannot see him.
- **News since the last refresh.** Use WebSearch for anything decision-relevant that is fresher than the
  cached news (lineup announcements, injury updates, trades). Verify before relying on it.
- **Timing.** Waiver run times, tip-offs, the claim order, ownership moving.

Judge candidly, including against the user's own players and against the user's own earlier views. Don't
over-correct a tested number on one game of evidence: say how strong the evidence is. Two tests showed
that teammate context and team minutes budgets don't help the model *on average*
(research/minutes_backtest.py, research/minutes_budget.py), so a take is about a specific case with
specific evidence, not a general rule.

## 3. Record the takes

Edit `data/takes.json` (private, gitignored). One take per player or decision:

```json
{"player": "Reed Sheppard", "also": ["Andrew Wiggins"], "stance": "against",
 "on": "Claiming Sheppard for Wiggins", "text": "The call and its evidence, with numbers.",
 "written": "YYYY-MM-DD", "until": "YYYY-MM-DD"}
```

- stance: `for` (agree with the HQ or back the move), `against`, `caution` (agree, with a risk to
  watch), `info`.
- `until`: when it should be rechecked, tied to an event (a waiver run, a teammate's return date). Keep it
  short; a stale take is worse than none.
- Update or replace earlier takes that the sweep shows are wrong or stale; delete expired ones.
- League trades (sweep's LEAGUE TRADES section): a take on any player in a trade shows under that trade on the
  Trades tab; say who won it and what it means for you (an opponent you meet soon got stronger, a team now
  short at a position may want what you have).
- Put things the HQ does not suggest at all (a player to hold, a drop it isn't flagging) in takes too.

Then seal, commit and push (the site refresh decrypts it):

```
python3 -m seasonkit takes
git add data/takes.enc && git commit -m "Analyst takes: <short summary>" && git push
```

Commit only `data/takes.enc`, never `data/takes.json`. Check that the pushed run's "Decrypt analyst
takes" step succeeded if anything about the workflow changed.

## 4. Report in chat

A table, most urgent first: the HQ's suggestion, your call (agree / against / caution), the reason in
one line, and urgency with deadline (do now / soon / optional, by when, and why). Then list the takes
added, changed and removed. Keep the full reasoning in the takes and in the chat, not only in one place.
If the sweep exposes a model gap (a pattern, not a one-off), say so and offer to test a fix; don't change
the model without a backtest.
