# League history, records and draft lottery

These commands add history to harambot. The data is imported from Yahoo
by following each season's league back to the season before it. You don't
have to type in past champions or stats.

## First-time setup

1. Set up harambot as the main README says (`/configure`, then `/league`).
   Set `DATABASE_URL` so history is saved to disk. Without it harambot uses
   an in-memory database and everything is lost on restart. For example:
   `DATABASE_URL = "sqlite:////data/harambot.db"`.
2. Run **`/history sync`** as a server admin. It imports every past
   season: final standings, first-round draft order, and every completed
   week's category totals. It takes about one Yahoo request per week, so
   the first run can take a few minutes.
3. Look at **`/trophycase`**. Yahoo doesn't mark consolation winners
   directly, so sync guesses the team ranked just below the playoff teams
   (7th with 6 playoff teams). If a season is wrong, fix it with
   `/history set-consolation` (and `/history set-champion` if needed).
   Manual fixes are kept when you sync again.

Run `/history sync` again at the end of each season, or whenever you want
the records updated. Seasons that are already finished and saved are
skipped. Use `full: True` to re-import everything.

## No Yahoo access? Import from a CSV instead

Yahoo now has to approve apps before they can use the Fantasy API. Until
yours is approved, you can type in your league history yourself so
`/trophycase` and `/draftlottery` still work. (Weekly `/records` need
Yahoo.)

1. Make `data/league_history.csv` (the `data` folder sits next to `harambot/`), using
   `docs/league_history_template.csv` as the pattern. One row per manager
   per season:

   | Column | Needed for | Notes |
   |---|---|---|
   | `season` | everything | `2025` or `2025-26` (the year the season started) |
   | `manager` | everything | Spell each person the same way every season |
   | `team` | display | Team name that season |
   | `finish` | trophy case | 1 = champion. Fill in for everyone, or leave blank for a season not played yet. For older seasons, a single row for the champion is enough |
   | `draft_pick` | lottery | First-round pick number in *that* season's draft |
   | `consolation_winner` | lottery | `yes` for one manager |
   | `wins`, `losses`, `ties` | `/alltime` | Category totals, optional |
   | `took_over_from` | lottery | For a new manager: whose team they took over |
   | `note` | trophy case | Shown under that season, e.g. `COVID year, no playoffs were played`. One row per season is enough |
   | `badge` | trophy case | Emoji shown next to that season's trophy, e.g. `🪠`. One row per season is enough |

2. In Discord, run **`/history import`** (admins). Leave `file` empty to
   load every CSV in the `data` folder, or attach a CSV.
   If anything's wrong, nothing is saved and the bot lists what to fix.
3. Check `/trophycase` and `/draftlottery odds`.

**Weekly matchups (optional).** For `/hallofshame`'s 9-0 sweeps, add
`league_matchups.csv` to the `data` folder, one row per matchup:
`season,week,manager1,team1,score1,manager2,team2,score2,is_playoffs`
(scores are categories won; `is_playoffs` is 1 for playoff weeks). `/history import` loads it too when it's there. Manager
names must match `league_history.csv`.

**Past drafts (optional).** For `/favourites`, add `league_drafts.csv`,
one row per pick: `season,round,pick,manager,team,player`. It's loaded by
`/history import` the same way.

**Past trades (optional).** For `/trades`, add `league_trades.csv`, one
row per trade:
`season,date,status,manager1,team1,manager1_gets,manager2,team2,manager2_gets`
(`manager1_gets` is what manager1 received; join several players with
` + `; `status` is `accepted` or `vetoed`; `date` as Yahoo shows it, e.g.
`Jan 4, 3:57 am`). It's loaded by `/history import` the same way.

**Category records (optional).** Until Yahoo approves API access, `/records`
uses `league_records.csv`, copied from Yahoo's Record Book: the all-time
#1 week and season in each category (plus most turnovers), one row per
record: `stat_id,scope,kind,season,week,manager,team,value`. Once
`/history sync` has pulled weekly stats from Yahoo, `/records` switches to
full top-10 lists automatically (leagues without a record book only).

For the lottery you need last season's `finish`, `draft_pick` and
`consolation_winner` rows. Add rows for the new season (manager and team
only) if anyone new is joining; otherwise last season's managers are used.

You can re-import as often as you like; each import replaces the seasons
in the file.

**Syncing from Yahoo after importing.** `/history sync` never changes a
finished season that came from the history files: it stops as soon as it
reaches one. It only adds seasons that aren't in the files (the current
one and every one after), with standings, weekly category stats,
head-to-head results and draft picks, so `/rivalry`, `/streaks`,
`/drafts` and friends carry straight on. `/records` keeps the record-book
numbers and swaps in any synced week that beats them.

Yahoo managers are matched to the names in `league_history.csv` by last
season's team name, or a Yahoo nickname that's the same as their name.
For anyone else (renamed team, nickname like `TheRealJosh`), add
`league_managers.csv`:

```
yahoo_name,manager
TheRealJosh,Josh
```

`yahoo_name` is a Yahoo nickname or team name. Anyone sync can't match is
added under their Yahoo name and listed in the sync reply; add them to the
file, run `/history import`, then sync again.

## Commands

| Command | Who | What it does |
|---|---|---|
| `/trophycase` | everyone | Champions and consolation winners by season, plus most titles |
| `/records` | everyone | Best single week ever in each of the 9 categories |
| `/records category:REB` | everyone | Top 10 rebound weeks. Add `worst:True` for the worst weeks, or `include_playoffs:False` to leave out playoff weeks |
| `/streaks` | everyone | Longest winning and losing streaks (from `league_matchups.csv`) and most weeks in a row leading the league per category (from `league_records.csv`) |
| `/hallofshame` | everyone | Winless seasons, worst records, best records without a title, most runner-ups and every 9-0 sweep (sweeps need `league_matchups.csv`) |
| `/drafts [season] [rounds] [manager] [player]` | everyone | Past drafts: a season's first X rounds (1 by default), a manager's whole draft, or every time a player was drafted (needs `league_drafts.csv`) |
| `/trades [season] [manager] [partner] [player]` | everyone | Every past trade, oldest first, with vetoed trades and trades that were traded straight back flagged; filter by season, manager, a pair of managers, or a player (needs `league_trades.csv`) |
| `/votetrade [trade]` | everyone | "Who won the trade?" poll for the latest trade (or any past one), open for 24 hours by default. This season's trades are pulled from Yahoo when it's connected |
| `/draftfeed start [channel] [catch_up]` | admins | Posts each pick of the live Yahoo draft as it happens (checked every 20 seconds), with history notes: how often that manager has drafted the player, who had him last year, recent #1 picks. Picks are saved straight away so `/drafts` works during the draft. Turns itself off when the draft ends |
| `/draftfeed stop` | admins | Turns the live draft feed off |
| `/favourites [manager]` | everyone | A manager's 5 most-drafted players (with no manager: the manager/player pairs drafted together most often), with seasons and earliest round (needs `league_drafts.csv`) |
| `/profile manager` | everyone | Titles, finishes, records, sweeps, nemesis, favourite player, trades and every team name they've used |
| `/rivalry manager opponent` | everyone | All-time head-to-head, playoff meetings, biggest wins and recent form |
| `/nemesis manager` | everyone | Record against every opponent, with nemesis and punching bag |
| `/season season` | everyone | Recap: standings, streaks, sweeps and how the #1 pick did |
| `/draftluck` | everyone | Where champions picked, how #1 picks finished, glow-ups and flops |
| `/alltime` | everyone | Career W-L-T, win %, titles and best finish for each manager |
| `/draftlottery odds` | everyone | Each team's balls and #1-pick odds |
| `/draftlottery run` | admins | Runs the lottery live, reveals picks from last to first, and saves the result |
| `/draftlottery results` | everyone | Shows the last saved lottery |
| `/draftlottery breakdown` | everyone | How the last lottery played out: balls left and odds at every pick, expected vs actual pick, luckiest and unluckiest, odds of the exact order |
| `/history sync` | admins | Adds new seasons from Yahoo; seasons from the history files are never changed |
| `/history import` | admins | Loads history from a CSV (no Yahoo needed) |
| `/history set-consolation` / `set-champion` | admins | Corrects a season's winners |

TO counts as best when it's lowest. FG% and FT% use the weekly totals Yahoo
reports.

## Lottery rules

* Balls come from the draft order of the **most recent finished season**:
  pick #1 gets 10 balls, #2 gets 11, … #12 gets 21.
* That season's consolation winner gets **+1** ball.
* Entrants are the managers in the newest synced season. A new manager who
  takes over an existing team inherits that team's previous pick.
* One pick is drawn at a time, and a drawn team's balls leave the hopper.
  The draw uses Python's `secrets` module, so it can't be predicted or
  seeded.
* To change the rules, edit `BASE_BALLS` / `CONSOLATION_BONUS` in
  `harambot/history/lottery.py`.

Best time to run it: after Yahoo renews the league for the new season and
all 12 managers have joined. Then run `/history sync` so the new season's
managers are imported, and run the lottery.

## Files

* `harambot/database/history_models.py`: `Season`, `ManagerSeason`,
  `WeeklyTeamStat` and `DraftLottery` tables. They're created on startup
  and need no migration.
* `harambot/history/parsers.py`: turns raw Yahoo JSON into rows (NBA stat
  IDs are defined here).
* `harambot/history/sync.py`: follows the season-to-season chain and saves
  each season.
* `harambot/history/lottery.py`: ball counts and the draw.
* `harambot/history/queries.py`: picks the lottery entrants.
* `harambot/cogs/history.py`: the Discord commands.
* `tests/test_history.py`: tests using real Yahoo NBA 9-cat sample data.
