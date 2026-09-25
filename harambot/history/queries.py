"""Database queries shared by the history cog. Kept free of discord.py so
they can be unit tested."""

from harambot.database.history_models import ManagerSeason, Season
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
    "first."
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
    entrants.sort(key=lambda e: e["previous_pick"])
    return entrants, previous, problems
