# Basket of Balls, 2026-27: Season HQ and the draft kit

Tools for ESPN league 1997238870 (12 teams, head-to-head points, default scoring).
`seasonkit/` runs the season: injuries, IR, waivers, streams and lineups, on a page hosted by
GitHub. `draftkit/` was the draft board. Standard library Python (3.9 up); Node only encrypts
the hosted page's data.

## Season HQ

The page: https://justinryukirogers.github.io/nba-season-hq/ (asks once per device for the
passphrase, which is `SITE_PASSPHRASE` in `.env`).

- **Today**: the to-do list. A healthy player left in an IR slot (ESPN then blocks every add), an
  Out player who could go to IR, today's lineup against the best one (starting with opening
  night), the best stream, a clear upgrade, an IR stash. Each carries an urgency, with the reason
  and any deadline: **Do now** (it costs points or blocks you today), **Soon** (a deadline such as
  a waiver run, or others may get there first), **Optional** (a small gain, or no one is racing
  you), **FYI**. Then the matchup: score, projection from both sides' best daily lineups, starts left.
- **Week**: the rest of the matchup week day by day. The adds worth making and the deadline for
  each (one a day, counting from that day's first tip-off), then each day's lineup with the roster
  you will have that day: who starts in which slot, who sits and how often he gets in when a
  starter sits, who has no game.
- **Roster**: three views. *Values*: slot, status, next game, expected starts this week (of his
  games left) and for the rest of the season, rest-of-season points per game, value; above it,
  your starters' projected points and starts against games. *Actual vs projected*: each player's points per game and minutes this
  season next to the draft board's projection and ESPN's, the gap, the number the dashboard now
  uses, and HOT/COLD (8+ games, 15% off) and MIN ▲/▼ (3+ games, 4+ minutes off) flags; above it,
  the team's points against projection, and a league-wide check of whether the board or ESPN is
  tracking reality better. *News*: ESPN's fantasy news on your players, newest first.
- **Waivers**: best adds (value against your weakest player), IR stashes (free agents tagged Out,
  so IR-eligible, ranked by what they add once back), streams for the rest of the week (points
  added to your daily lineups, minus any long-term value given up), and ESPN-wide trending adds.
- **Trades**: 1-for-1, 2-for-1 and 1-for-2 trades with each team that add to your simulated
  lineups over the rest of the season and that the other team wins on ESPN's projections
  (counted above a waiver pickup, which is what stops two middling players looking like a star).
- **Injuries**: everyone hurt that a team holds or might add, with ESPN's return estimate, and
  for longer absences the free agents at his positions on his NBA team.
- **League**: standings with each roster's projected points a week from its starters, this week's scores, adds and drops of the last four days.

Tap any player for his card: injury report, how his value is built, next games with the chance
he plays each, fantasy points by period, news.

### How it stays live

`.github/workflows/refresh.yml` runs every 20 minutes. GitHub's own schedule never fired for this
repo, so an outside timer (a cron-job.org job) starts it instead, through GitHub's
`workflow_dispatch` API with a fine-grained token that can only run this repo's workflows
(Actions: read and write); the `schedule:` in the workflow stays as a backup. The page shows
the data's age and turns amber after 90 minutes. It runs the
checks, pulls from ESPN with the cookies in the repository secrets `ESPN_S2` and `SWID`,
encrypts the result with `SITE_PASSPHRASE`, and publishes `site/` to GitHub Pages. If a run
fails, GitHub emails you and the page keeps the last good data. Run it by hand from the repo's
Actions tab (refresh, Run workflow).

What is public: this code and `data/priors.json` (the draft board's per-game numbers and its
fitted games coefficients). The page itself is public too, but its data is only `data.enc`,
AES-256 under the passphrase. The cookies never leave the secrets; `.gitignore` keeps `.env`,
the draft-night board and `data/intel.json` out of the repo.

When something changes:

    gh secret set ESPN_S2 -R JustinRyukiRogers/nba-season-hq     # cookies expired (runs fail with HTTP 401)
    gh secret set SWID -R JustinRyukiRogers/nba-season-hq
    gh secret set SITE_PASSPHRASE -R JustinRyukiRogers/nba-season-hq   # new passphrase; update .env too

If cron-job.org's history shows 401s, the timer's token has expired or was revoked (it was made
to last to 2027-04-01): make a new one the same way and paste it into the job's Authorization
header (`Bearer <token>`).

GitHub switches schedules off in public repos after 60 days without activity. Each run
re-enables its own workflow to prevent that; if the page ever goes stale for a day, check
Actions > refresh and press "Enable workflow".

### Locally

    python3 -m seasonkit refresh     # pull and value everything, print the actions and roster
    python3 -m seasonkit serve       # http://localhost:8766, reads site/data.json directly
    python3 tests/test_season.py     # lineup, IR, absence and encryption checks

### How a value is computed (in season)

1. Per game: the draft board's preseason projection (`data/priors.json`, frozen on draft night),
   blended with this season's actual average as games come in. After 15 games the two count
   equally (`PRIOR_GAMES` in `seasonkit/model.py`). That 15 is a placeholder until it is fitted
   on past seasons' game logs; it changes nothing until the first games are played.
2. Games: every remaining game of his team inside the fantasy season, with the chance he plays
   it. An Out player misses everything before ESPN's return estimate; those games are free while
   an IR slot holds him. After a layoff of two weeks or more he is counted back for only part of
   the remaining games (the draft board's comeback factors: 90%, or 55% after two months).
   A day-to-day player gets a 60% chance at his next game. A healthy player gets 93% for the
   coming week, then his preseason durability.
3. Value: points above a waiver pickup over the rest of the season, the draft board's formula
   (playoff weeks 1.5x, a bust can be dropped). The replacement level is real here: the
   6th-best healthy free agent in this league.

4. Starts: only starters score, so team projections, the matchup, the week plan, streams and
   trades come from simulated daily lineups (`lineup_day` in `seasonkit/model.py`): each day,
   who plays is drawn from each player's chance of playing, and the best legal lineup of those
   starts, the way you would set it after the injury news. Days when everyone with a game fits
   need no simulation. The draws are fixed per player and day, so numbers do not jitter between
   refreshes and two rosters are compared on the same luck. Late scratches after a lineup locks
   are not modelled, so this slightly flatters deep benches.

`data/season_notes.json` overrides a return date, games share or per-game number for a player.

### Not yet checked

The blend weight, the 60% and 93% play chances and the stream thresholds are judgment, not
fitted. ESPN's rules that matter, as read on 2026-10-16: 1 add a day, waivers 24 hours with a
rolling order, lineups lock per game, only players tagged Out fit an IR slot, and a healthy
player in IR blocks adds. Whether ESPN lets you add an Out player straight into an empty IR slot
when the bench is full is not confirmed; the stash advice allows for either. Nor is whether
ESPN counts the days before the opener as one scoring day for the 1-add-a-day limit. Streams
assume an add made after the day's first tip-off counts from the next day. The HOT/COLD and
MIN flags are display rules of thumb, not fitted. The week plan is greedy (each day takes the
best add left), and trade ideas lean entirely on the gap between the dashboard's numbers and
ESPN's, which is untested in season.

# The draft kit

## Draft night (Thu Oct 15, 11:00 PM ET; done)

    python3 -m draftkit refresh     # that afternoon: latest projections, ADP, injuries, news
    python3 -m draftkit serve       # opens http://localhost:8765

The top panel says who to take when you are on the clock, and who is likely to be there when
you are not. Click any player for his card: how ESPN's number became ours, research notes,
latest news. Picks arrive from ESPN automatically if live sync is set up (below). Otherwise
type a name in the search box and press Enter, or use the button on his row. Undo is in the
top bar. The draft so far is saved after every pick, so restarting the server loses nothing.
The search box also takes an NBA team (NOP, or ESPN's spelling such as Bkn) to list its players.

Use the Practice buttons beforehand to mock draft against a model of how this league picks,
then Reset. They switch off once ESPN's real draft has picks. With live sync on, any practice
picks still on the board are cleared when ESPN's first real pick arrives.

The board loads its data when it starts. After a `refresh`, a `build`, or a change to the draft
order, restart it: `pkill -f "draftkit serve"`, then `python3 -m draftkit serve`, and reload the page.

## Draft order

    python3 -m draftkit order                 # show it
    python3 -m draftkit order SG DDT FEET ... # set all twelve seats, by team abbreviation
    python3 -m draftkit slot 5                # only your seat is known yet

The commissioner sets the order by hand, and ESPN can show a placeholder until they do. A seat
set with `slot` is kept until ESPN's order agrees with it. Once ESPN's draft is actually
running, its order always wins (live sync only).

## Live sync (optional, two minutes)

The league is private, so reading the live draft needs your ESPN login cookies.

1. In Chrome, open fantasy.espn.com while logged in.
2. Open DevTools (Option+Command+I), then Application > Cookies > https://fantasy.espn.com,
   and find the rows named `espn_s2` and `SWID` (click the Name header to sort).
3. `cp .env.example .env`, open `.env` in a text editor, and paste each value after its `=`:
   the long `espn_s2` value after `ESPN_S2=`, and the `SWID` value, braces included, after
   `SWID=`. Copy each from the panel under the table, with "Show URL-decoded" left unticked.
4. Run `python3 -m draftkit refresh`. The line "league settings" should say ok.
5. Restart the board. The terminal says "ESPN live sync: on", and within a few seconds the top
   bar shows "ESPN connected, draft not started".

During the draft, pick in ESPN's draft room as usual; the board only reads from ESPN. Each pick
appears within a few seconds and the top bar says "ESPN live". A red "ESPN sync problem"
means ESPN rejected the cookies or could not be reached (hover over it to see which): type picks
by hand meanwhile, and the feed takes over again when it recovers. If the board ever disagrees
with ESPN, press Reset, and every real pick reloads from ESPN within seconds.

The cookies stay on this machine. Without them everything works except automatic pick entry
and picking up a changed draft order.

## Changing a player's outlook

Research notes live in `data/intel.json`: minutes, per-minute rate, share of games, expected
return date, each with a confidence and sources. Edit it, then:

    python3 -m draftkit build

## How a value is computed

1. Baseline per game: ESPN's projection blended 70/30 with FantasyPros' consensus.
2. Corrections fitted on five past seasons (`python3 -m draftkit fit` prints them): projections
   run high across the board; track record pulls the number back; age 28+, new teams, and
   under-50-game seasons cost; crowded teams cost a little.
3. Research notes from `data/intel.json`.
4. Games: what a projected games total has really turned into, counted over the fantasy season
   (Oct 20 to Mar 28, playoffs Mar 8-28 weighted 1.5x; the dates come from the league file) and after any known absence.
5. Value: points above what a waiver pickup would give, allowing that a bust gets dropped.

The recommendation adds what should still be on the board at your next pick, from simulated
runs of the room. The room model is fitted on this league's 2024-2026 drafts.

## What has and has not been checked

    python3 research/backtest_espn.py    # where ESPN's projections miss
    python3 research/draft_sim.py        # does the pick logic out-draft this league (takes minutes)
    python3 research/role_and_impact.py  # do last year's role and BPM add anything (they flag risk, not value)
    python3 research/roster_fit.py       # do positions, one-team stacks or the playoff schedule matter (takes minutes)
    python3 tests/test_engine.py         # engine and live-sync checks

The per-game corrections improve accuracy out of sample. The research notes and the live-sync
polling cannot be backtested; live sync is exercised against made-up feeds in ESPN's response
shape until the real draft opens.

Roster fit is left out of the values on purpose. With ten starters out of thirteen, five of
them flexible slots, and lineups set daily, the kit's mock rosters lose about 0.3% of their
points to players stuck on the bench. The one thing that costs more is a sixth player who can
only play C, and the board warns before that pick.

## Layout

    seasonkit/       in season: pull (ESPN), model (values, lineups), advice (moves), build, crypt.mjs
    site/            the hosted page (static; data.json locally, data.enc when hosted)
    draftkit/        the draft kit: espn, sources, history, calibrate, board, engine, server, static/
    data/league.json league settings (re-read by refresh when cookies are present)
    data/intel.json  research notes (yours to edit)
    data/raw/        latest pulls; data/raw/snapshots keeps each day's ADP
    data/history/    past seasons used for fitting
    research/        the evidence reports
