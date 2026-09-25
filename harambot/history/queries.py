"""Database queries shared by the history cog. Kept free of discord.py so
they can be unit tested."""

from harambot.database.history_models import (
    DiscordLink,
    ManagerSeason,
    Season,
)
from harambot.history import lottery


def season_label(season):
    # NBA seasons span two calendar years; Yahoo labels them by the first.
    return "{}-{:02d}".format(season, (season + 1) % 100)


def latest_names(guild_id):
    """Most recent manager nickname and team name for each GUID."""
    names = {}
    rows = (
        ManagerSeason.select()
        .where(ManagerSeason.guild_id == str(guild_id))
        .order_by(ManagerSeason.season)
    )
    for r in rows:
        names[r.manager_guid] = (r.manager_name, r.team_name)
    return names


def has_history(guild_id):
    return Season.select().where(Season.guild_id == str(guild_id)).exists()


NO_HISTORY = (
    "No league history saved yet. An admin needs to run `/history sync` "
    "(from Yahoo) or `/history import` (from a CSV) first."
)


def build_lottery_entrants(guild_id):
    """Work out who is in this year's lottery and how many balls each gets.

    Entrants are the managers in the newest synced season. Balls come from
    the most recently *finished* season: its first-round draft order and
    its consolation winner. Returns (entrants, previous_season, problems).
    """
    guild_id = str(guild_id)
    newest = (
        Season.select()
        .where(Season.guild_id == guild_id)
        .order_by(Season.season.desc())
        .first()
    )
    previous = (
        Season.select()
        .where((Season.guild_id == guild_id) & (Season.is_finished == True))  # noqa: E712
        .order_by(Season.season.desc())
        .first()
    )
    if newest is None or previous is None:
        return [], None, ["No finished season has been synced yet."]

    prev_rows = list(
        ManagerSeason.select().where(
            (ManagerSeason.guild_id == guild_id)
            & (ManagerSeason.season == previous.season)
        )
    )
    by_guid = {r.manager_guid: r for r in prev_rows}
    by_team_id = {r.team_id: r for r in prev_rows}

    current_rows = list(
        ManagerSeason.select().where(
            (ManagerSeason.guild_id == guild_id)
            & (ManagerSeason.season == newest.season)
        )
    )
    entrants, problems = [], []
    for r in current_rows:
        # Same manager as last year, or a new manager who took over the
        # same team slot.
        prev = by_guid.get(r.manager_guid) or by_team_id.get(r.team_id)
        if prev is None or prev.draft_position is None:
            problems.append(
                "No {} draft pick found for **{}** ({})".format(
                    season_label(previous.season), r.manager_name,
                    r.team_name,
                )
            )
            continue
        is_consolation = (
            previous.consolation_guid is not None
            and prev.manager_guid == previous.consolation_guid
        )
        entrants.append(
            {
                "manager_guid": r.manager_guid,
                "manager_name": r.manager_name,
                "team_name": r.team_name,
                "previous_pick": prev.draft_position,
                "consolation_winner": is_consolation,
                "balls": lottery.balls_for(
                    prev.draft_position, is_consolation
                ),
            }
        )
    links = discord_links(guild_id)
    for e in entrants:
        e["discord_id"] = links.get(e["manager_guid"])
    entrants.sort(key=lambda e: e["previous_pick"])
    return entrants, previous, problems


def discord_links(guild_id):
    """manager_guid -> Discord user id, for managers linked with
    /history link."""
    return {
        link.manager_guid: link.discord_user_id
        for link in DiscordLink.select().where(
            DiscordLink.guild_id == str(guild_id)
        )
    }


def mention(entry):
    """"<@id>" for a linked manager, otherwise their bold name."""
    if entry.get("discord_id"):
        return "<@{}>".format(entry["discord_id"])
    return "**{}**".format(entry["manager_name"])


def hall_of_shame(guild_id, sweep_score=9):
    """Data for /hallofshame.

    Returns dict with:
      winless: ManagerSeason rows with no wins (and at least one loss)
      worst:   the three worst non-winless seasons by win %
      sweeps:  list of (season, week, loser_name, loser_team, winner_name,
               winner_team), newest first. A sweep is losing
               ``sweep_score``-0 with no ties.
    """
    from harambot.database.history_models import WeeklyMatchup

    guild_id = str(guild_id)
    rows = list(ManagerSeason.select().where(
        (ManagerSeason.guild_id == guild_id)
        & ((ManagerSeason.wins + ManagerSeason.losses
            + ManagerSeason.ties) > 0)
    ))
    winless = [r for r in rows if r.wins == 0]
    winless.sort(key=lambda r: (-r.losses, r.season))

    def pct(r):
        return (r.wins + 0.5 * r.ties) / (r.wins + r.losses + r.ties)

    worst = sorted((r for r in rows if r.wins > 0), key=pct)[:3]

    sweeps = []
    for m in WeeklyMatchup.select().where(WeeklyMatchup.guild_id == guild_id):
        if m.score1 >= sweep_score and m.score2 == 0:
            sweeps.append((m.season, m.week, m.manager2_name, m.team2,
                           m.manager1_name, m.team1, m.manager2_guid,
                           m.manager1_guid))
        elif m.score2 >= sweep_score and m.score1 == 0:
            sweeps.append((m.season, m.week, m.manager1_name, m.team1,
                           m.manager2_name, m.team2, m.manager1_guid,
                           m.manager2_guid))
    sweeps.sort(key=lambda s: (-s[0], -s[1]))
    return {"winless": winless, "worst": worst, "sweeps": sweeps,
            "pct": pct}


def most_drafted(guild_id, manager_guid=None, limit=10):
    """Players drafted most often, by one manager or the whole league.

    Returns a list of dicts: player, times, seasons (sorted list), and
    best_round (earliest round they were taken), most drafted first.
    """
    from harambot.database.history_models import DraftPick

    q = DraftPick.select().where(DraftPick.guild_id == str(guild_id))
    if manager_guid:
        q = q.where(DraftPick.manager_guid == manager_guid)
    players = {}
    for p in q:
        d = players.setdefault(
            p.player, {"player": p.player, "times": 0, "seasons": set(),
                       "best_round": p.round, "managers": set()}
        )
        d["times"] += 1
        d["seasons"].add(p.season)
        d["best_round"] = min(d["best_round"], p.round)
        d["managers"].add(p.manager_name)
    ranked = sorted(
        players.values(),
        key=lambda d: (-d["times"], d["best_round"], d["player"]),
    )
    for d in ranked:
        d["seasons"] = sorted(d["seasons"])
        d["managers"] = sorted(d["managers"])
    return ranked[:limit]


def loyal_pairs(guild_id, limit=10, min_times=2):
    """Manager/player pairs drafted together most often across seasons,
    e.g. a manager who took the same player four years running."""
    from harambot.database.history_models import DraftPick

    pairs = {}
    for p in DraftPick.select().where(DraftPick.guild_id == str(guild_id)):
        d = pairs.setdefault(
            (p.manager_guid, p.player),
            {"manager": p.manager_name, "manager_guid": p.manager_guid,
             "player": p.player, "times": 0,
             "seasons": set(), "best_round": p.round},
        )
        d["times"] += 1
        d["seasons"].add(p.season)
        d["best_round"] = min(d["best_round"], p.round)
    ranked = sorted(
        (d for d in pairs.values() if d["times"] >= min_times),
        key=lambda d: (-d["times"], d["best_round"], d["manager"]),
    )
    for d in ranked:
        d["seasons"] = sorted(d["seasons"])
    return ranked[:limit]


RIP = "🪦"


def current_managers(guild_id):
    """GUIDs of managers in the newest saved season."""
    newest = (
        Season.select()
        .where(Season.guild_id == str(guild_id))
        .order_by(Season.season.desc())
        .first()
    )
    if newest is None:
        return set()
    return {
        r.manager_guid
        for r in ManagerSeason.select(ManagerSeason.manager_guid).where(
            (ManagerSeason.guild_id == str(guild_id))
            & (ManagerSeason.season == newest.season)
        )
    }


def name_marker(guild_id):
    """Returns tag(guid, name): the name, plus a 🪦 for managers who are
    no longer in the league."""
    current = current_managers(guild_id)

    def tag(guid, name):
        if current and guid not in current:
            return "{} {}".format(name, RIP)
        return name

    return tag


def matchup_streaks(guild_id, limit=5):
    """Longest winning and losing streaks within a season, from weekly
    head-to-head results (playoff weeks included; a tie ends a streak).

    Returns {"W": [...], "L": [...]}, each a list of dicts with manager,
    manager_guid, team, season, start, end and length, longest first.
    """
    from harambot.database.history_models import WeeklyMatchup

    games = {}
    for m in WeeklyMatchup.select().where(
            WeeklyMatchup.guild_id == str(guild_id)):
        r1 = "W" if m.score1 > m.score2 else "L" if m.score1 < m.score2 \
            else "T"
        r2 = {"W": "L", "L": "W", "T": "T"}[r1]
        for guid, name, team, res in (
            (m.manager1_guid, m.manager1_name, m.team1, r1),
            (m.manager2_guid, m.manager2_name, m.team2, r2),
        ):
            games.setdefault((m.season, guid), (name, team, []))[2].append(
                (m.week, res))

    streaks = {"W": [], "L": []}
    for (season, guid), (name, team, results) in games.items():
        results.sort()
        for kind in ("W", "L"):
            run, start = 0, None
            for week, res in results + [(None, None)]:
                if res == kind:
                    if run == 0:
                        start = week
                    run += 1
                    end = week
                    continue
                if run:
                    streaks[kind].append({
                        "manager": name, "manager_guid": guid, "team": team,
                        "season": season, "start": start, "end": end,
                        "length": run,
                    })
                run = 0
    for kind in streaks:
        streaks[kind].sort(key=lambda s: (-s["length"], s["season"]))
        streaks[kind] = streaks[kind][:limit]
    return streaks


def draft_board(guild_id, season=None, max_round=None, manager_guid=None,
                player=None):
    """Past draft picks, filtered (``max_round`` keeps rounds 1..N).
    Sorted by season, round, pick."""
    from harambot.database.history_models import DraftPick

    q = DraftPick.select().where(DraftPick.guild_id == str(guild_id))
    if season is not None:
        q = q.where(DraftPick.season == season)
    if max_round is not None:
        q = q.where(DraftPick.round <= max_round)
    if manager_guid:
        q = q.where(DraftPick.manager_guid == manager_guid)
    if player:
        q = q.where(DraftPick.player == player)
    return list(q.order_by(DraftPick.season, DraftPick.round,
                           DraftPick.pick))


def drafted_players(guild_id, search="", limit=25):
    """Distinct drafted player names containing ``search``."""
    from harambot.database.history_models import DraftPick

    q = (DraftPick.select(DraftPick.player).distinct()
         .where(DraftPick.guild_id == str(guild_id)))
    if search:
        q = q.where(DraftPick.player.contains(search))
    return sorted(p.player for p in q)[:limit]
