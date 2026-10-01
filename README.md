# FishBallersBot

_The Discord bot for **Fish Ballers**, our Yahoo fantasy basketball league (12 teams, head-to-head, 9 categories, est. 2016)._

It keeps the league's whole history: every champion, record, rivalry, draft and trade back to 2016-17. It also runs the weighted draft lottery, posts the live draft pick by pick, and settles arguments about who won a trade.

> Forked from [harambot](https://github.com/DMcP89/harambot) by DMcP89, which provides the Yahoo connection and the live-season commands.

## Commands

Type `/help` in Discord for the same list, grouped.

### 🏆 League history

| Command | What it does |
| --- | --- |
| `/trophycase` | Every past champion, plus who has the most titles |
| `/alltime` | Career records and titles for every manager |
| `/profile manager` | A manager's career: titles, finishes, records, sweeps, nemesis, favourite player, trades and every team name they've used |
| `/rivalry manager opponent` | All-time head-to-head, including playoff meetings and the current streak |
| `/nemesis manager` | Who they can't beat, and who they always beat (5+ games) |
| `/season season` | Recap of one season: standings, streaks, sweeps, #1 pick |
| `/records [category] [worst]` | All-time best (or worst) category weeks and seasons |
| `/streaks` | Longest win and losing streaks, and most weeks leading the league |
| `/hallofshame` | Winless seasons, worst records, chokers, bridesmaids and 9-0 sweeps |
| `/trades [season] [manager] [partner] [player]` | Every past trade, with vetoed and traded-back ones flagged |
| `/votetrade [trade]` | "Who won this trade?" poll for the latest trade, or any past one |

### 🎱 Drafts

| Command | What it does |
| --- | --- |
| `/draftlottery odds` | Each team's balls and chance at the #1 pick |
| `/draftlottery run [delay] [practice]` | Runs the lottery and reveals the order from last pick to first (admins). `practice:True` does a test run that isn't saved |
| `/draftlottery results` | The most recent lottery result |
| `/draftlottery breakdown` | How the last lottery played out: balls left at each pick, expected vs actual, luckiest and unluckiest |
| `/drafts [season] [rounds] [manager] [player]` | Past drafts: a season's first few rounds, a manager's picks, or every time a player was drafted |
| `/favourites [manager]` | A manager's most-drafted players (or the league's biggest manager/player crushes) |
| `/draftluck` | Does draft position matter? Luckiest and unluckiest lottery history, where champions picked |
| `/draftfeed start [channel] [catch_up]` | Posts each pick of the live Yahoo draft as it happens, with history notes (admins, needs Yahoo) |
| `/draftfeed stop` | Turns the live draft feed off (admins) |

**Lottery rules:** last year's #1 pick gets 10 balls, #2 gets 11, and so on up to 21 for #12. The consolation-bracket winner gets 1 extra ball.

### 📊 This season (needs Yahoo)

| Command | What it does |
| --- | --- |
| `/standings` | Current standings |
| `/matchups [week]` | This week's (or any week's) matchups |
| `/roster team_name` | A team's roster |
| `/stats player_name [week]` | A player's details and stats |
| `/waivers [days]` | Waiver moves from the last few days |

### 🔧 Setup (admins)

| Command | What it does |
| --- | --- |
| `/configure` | Connects the bot to Yahoo for this server |
| `/league` | Chooses which Yahoo league to use |
| `/reports` | Picks a channel for automatic transaction reports |
| `/history import [file]` | Loads league history from the CSV files in the bot's folder (or an attached CSV) |
| `/history sync [full]` | Pulls new seasons from Yahoo. Seasons from the CSV files are never changed |
| `/history link manager user` | Links a manager to their Discord account so the bot can tag them |
| `/history links` | Shows who's linked |
| `/history set-champion season manager` | Fixes a season's champion |
| `/history set-consolation season manager` | Fixes a season's consolation winner (it affects lottery balls) |
| `/ping` | Checks the bot is alive |

"Admins" means members with the Discord **Administrator** permission (the server owner always counts). You can change who can use each command in Server Settings → Integrations → FishBallersBot.

## League history data

The history lives in CSV files in the `data/` folder. `/history import` loads them all:

| File | What's in it |
| --- | --- |
| `league_history.csv` | Every season: manager, team, finish, draft pick, record, consolation winner, notes |
| `league_matchups.csv` | Every weekly head-to-head result |
| `league_drafts.csv` | Every draft pick |
| `league_records.csv` | Yahoo's Record Book: the all-time #1 in each category |
| `league_trades.csv` | Every trade (accepted and vetoed) |
| `league_managers.csv` | Yahoo nicknames that don't match a manager's name, so `/history sync` can match them |

Each import replaces the seasons in the file, so it's safe to run again after fixing something. Lottery results and Discord links aren't in the files, so an import never touches them. [docs/LEAGUE_HISTORY.md](docs/LEAGUE_HISTORY.md) has the column formats and more detail.

## Running the bot

### What you need

- **Python 3.10+** and **[Poetry](https://python-poetry.org/docs/#installation)**
- **A Discord application with a bot token.** Get one from the [Discord Developer Portal](https://discord.com/developers/applications): New Application → **Bot** → **Reset Token**, and copy it. No privileged intents are needed.
- **A Yahoo developer app** (client ID and secret). Create one at [developer.yahoo.com/apps](https://developer.yahoo.com/apps/) with **Fantasy Sports → Read** access. The bot logs in with Yahoo's `oob` (copy-paste code) flow; if the form insists on a redirect URL, `https://localhost` works. _Optional:_ the history and lottery commands work without Yahoo.
- **An encryption key** for the stored Yahoo login. Generate one once and **keep it**: if it changes, you'll need to run `/configure` again.

  ```
  poetry run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```

### 1. Install

```
git clone https://github.com/LeGitJames/fishBallersDiscord.git
cd fishBallersDiscord
poetry install
```

### 2. Create `.env`

Make a file called `.env` in the project root (it's in `.gitignore`, so it never gets committed):

```
DISCORD_TOKEN=your-discord-bot-token
YAHOO_KEY=your-yahoo-client-id
YAHOO_SECRET=your-yahoo-client-secret
DATABASE_URL=sqlite:///fishballers.db
HARAMBOT_KEY=your-encryption-key
YAHOO_ENABLED=false
```

Set `YAHOO_ENABLED=false` while Yahoo API access isn't set up. Yahoo commands then reply with a "waiting on Yahoo" message instead of failing, and `/help` lists them separately. Remove the line (or set it to `true`) once Yahoo is ready.

### 3. Run it

```
poetry run harambot
```

The bot registers its slash commands when it starts. If new commands don't show up in Discord, press **Ctrl+R** to refresh.

Everything the bot saves is in `fishballers.db`, including lottery results, Discord links and the Yahoo login. It's in `.gitignore`, so back it up yourself if you move the bot to another machine.

### 4. Add it to the server

In the Developer Portal, go to **OAuth2 → URL Generator**. Tick the `bot` and `applications.commands` scopes, then these bot permissions: Send Messages, Send Messages in Threads, Embed Links, Attach Files, Read Message History, Add Reactions, Use Slash Commands, Manage Webhooks (permission value `277562378304`). Open the generated URL and pick the server.

### 5. Set it up in Discord

1. Run `/history import` to load the league history.
2. Run `/history link` once for each manager, so lottery reveals and draft picks tag the right person.
3. _When Yahoo is ready:_ run `/configure`, click **Login to Yahoo**, approve, and paste the code into **Configure Guild** with the league ID (`38650`) and type `nba`. Then run `/history sync` to pull in the current season.

## Tests

```
poetry run pytest
```

The history, lottery, sync, trades and draft-feed logic is covered in `tests/test_history.py`. These tests don't need Discord or Yahoo.

## Hosting

The bot only runs while the computer running it is awake. To keep it online all the time, it needs a server (e.g. a small VPS, or a Render background worker).

**Heads-up:** `docker/Dockerfile`, `docker/docker-compose.yml` and `render.yaml` are still the upstream harambot versions. They install the original harambot package rather than this fork, so they need updating before they can deploy FishBallersBot.

## Credits

Built on [harambot](https://github.com/DMcP89/harambot) by DMcP89 (MIT licence, see [LICENSE.md](LICENSE.md)).
