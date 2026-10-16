# Basket of Balls, 2026-27: Season HQ and the draft kit

Tools for ESPN league 1997238870 (12 teams, head-to-head points, default scoring).
`seasonkit/` runs the season: injuries, IR, waivers, streams and lineups, on a page hosted by
GitHub. `draftkit/` was the draft board. Standard library Python (3.9 up); Node only encrypts
the hosted page's data.

## Season HQ

The page: https://justinryukirogers.github.io/nba-season-hq/ (asks once per device for the
passphrase, which is `SITE_PASSPHRASE` in `.env`).

- **Today**: what to do now, most urgent first. A healthy player left in an IR slot (ESPN then
  blocks every add), an Out player who could go to IR, today's lineup against the best one
  (starting with opening night), the best stream, a clear upgrade, an IR stash. Then the matchup:
  score, projection from both sides' best daily lineups, starts left.
- **Roster**: slot, status, next game, games this week, rest-of-season points per game, value.
- **Waivers**: best adds (value against your weakest player), IR stashes (free agents tagged Out,
  so IR-eligible, ranked by what they add once back), streams for the rest of the week (points
  added to your daily lineups, minus any long-term value given up), and ESPN-wide trending adds.
- **Injuries**: everyone hurt that a team holds or might add, with ESPN's return estimate, and
  for longer absences the free agents at his positions on his NBA team.
- **League**: standings, this week's scores, adds and drops of the last four days.

Tap any player for his card: injury report, how his value is built, next games with the chance
he plays each, fantasy points by period, news.

### How it stays live

`.github/workflows/refresh.yml` runs every 20 minutes (GitHub can run scheduled jobs late or skip
one when busy; the page shows the data's age and turns amber after 90 minutes). It runs the
checks, pulls from ESPN with the cookies in the repository secrets `ESPN_S2` and `SWID`,
encrypts the result with `SITE_PASSPHRASE`, and publishes `site/` to GitHub Pages. If a run
fails, GitHub emails you and the page keeps the last good data. Run it by hand from the repo's
Actions tab (refresh, Run workflow).

What is public: this code, `data/priors.json` (the draft board's per-game numbers) and
`data/calibration.json`. The page itself is public too, but its data is only `data.enc`,
AES-256 under the passphrase. The cookies never leave the secrets; `.gitignore` keeps `.env`,
the draft-night board and `data/intel.json` out of the repo.

When something changes:

    gh secret set ESPN_S2 -R JustinRyukiRogers/nba-season-hq     # cookies expired (runs fail with HTTP 401)
    gh secret set SWID -R JustinRyukiRogers/nba-season-hq
    gh secret set SITE_PASSPHRASE -R JustinRyukiRogers/nba-season-hq   # new passphrase; update .env too

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

`data/season_notes.json` overrides a return date, games share or per-game number for a player.

### Not yet checked

The blend weight, the 60% and 93% play chances and the stream thresholds are judgment, not
fitted. ESPN's rules that matter, as read on 2026-10-16: 1 add a day, waivers 24 hours with a
rolling order, lineups lock per game, only players tagged Out fit an IR slot, and a healthy
player in IR blocks adds. Whether ESPN lets you add an Out player straight into an empty IR slot
when the bench is full is not confirmed; the stash advice allows for either. Streams assume an
add made after the day's first tip-off counts from the next day.

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
