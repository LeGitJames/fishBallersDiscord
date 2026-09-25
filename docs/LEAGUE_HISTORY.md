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

## Commands

| Command | Who | What it does |
|---|---|---|
| `/trophycase` | everyone | Champions and consolation winners by season, plus most titles |
| `/records` | everyone | Best single week ever in each of the 9 categories |
| `/records category:REB` | everyone | Top 10 rebound weeks. Add `worst:True` for the worst weeks, or `include_playoffs:False` to leave out playoff weeks |
| `/alltime` | everyone | Career W-L-T, win %, titles and best finish for each manager |
| `/draftlottery odds` | everyone | Each team's balls and #1-pick odds |
| `/draftlottery run` | admins | Runs the lottery live, reveals picks from last to first, and saves the result |
| `/draftlottery results` | everyone | Shows the last saved lottery |
| `/history sync` | admins | Imports or refreshes history from Yahoo |
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
