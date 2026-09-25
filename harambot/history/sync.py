"""Pull league history from Yahoo into the local database.

Yahoo links each season's league to the one before it through the
``renew`` setting, so starting from the league a guild is configured with
we can walk back through every past season the league has existed.

This makes blocking HTTP calls; run it in a thread (``asyncio.to_thread``)
from Discord handlers.
"""

import datetime
import logging

from harambot.database.history_models import (
    ManagerSeason,
    Season,
    WeeklyTeamStat,
)
from harambot.database.models import database
from harambot.history import parsers

logger = logging.getLogger("discord.harambot.history.sync")

MAX_SEASONS = 25


def _truthy(value):
    return str(value).lower() in ("1", "true")


def _season_is_complete_in_db(guild_id, season):
    s = Season.get_or_none(
        (Season.guild_id == str(guild_id)) & (Season.season == season)
    )
    # Seasons typed in by hand (/history import) are always replaced by
    # real Yahoo data.
    return s is not None and s.is_finished and s.league_key != "manual"


def sync_league_history(guild_id, current_league, full=False, progress=None):
    """Walk from ``current_league`` back through its ``renew`` chain.

    ``current_league`` is a ``yahoo_fantasy_api.League`` for the guild's
    configured league. Finished seasons already in the database are skipped
    unless ``full`` is True. Returns a list of per-season summaries.
    """
    guild_id = str(guild_id)
    handler = current_league.yhandler
    sc = current_league.sc
    league_key = current_league.league_id
    summaries = []

    from yahoo_fantasy_api import League  # local import keeps tests light

    league = current_league
    for _ in range(MAX_SEASONS):
        settings = league.settings()
        season = int(settings["season"])
        prev_key = parsers.renew_to_league_key(settings.get("renew"))

        if not full and _season_is_complete_in_db(guild_id, season):
            logger.info("Season %s already synced, skipping", season)
            summaries.append({"season": season, "skipped": True})
        else:
            if progress:
                progress(season)
            summaries.append(
                _sync_season(guild_id, league_key, league, handler, settings)
            )

        if not prev_key:
            break
        league_key = prev_key
        league = League(sc, league_key, handler=handler)
    return summaries


def _sync_season(guild_id, league_key, league, handler, settings):
    season = int(settings["season"])
    is_finished = _truthy(settings.get("is_finished"))
    logger.info("Syncing season %s (%s)", season, league_key)

    standings = parsers.parse_standings(handler.get_standings_raw(league_key))
    team_key_to_guid = {r["team_key"]: r["manager_guid"] for r in standings}

    draft_order = {}
    if settings.get("draft_status") == "postdraft":
        try:
            draft_order = parsers.first_round_order(
                league.draft_results(), team_key_to_guid
            )
        except Exception:
            logger.exception("Could not read draft results for %s", season)

    start_week = int(settings.get("start_week") or 1)
    if is_finished:
        last_week = int(settings.get("end_week") or 0)
    else:
        last_week = int(settings.get("current_week") or start_week) - 1

    weekly = []
    if settings.get("draft_status") == "postdraft":
        for week in range(start_week, last_week + 1):
            try:
                raw = handler.get_scoreboard_raw(league_key, week=week)
                weekly.extend(parsers.parse_scoreboard(raw))
            except Exception:
                logger.exception("Week %s of %s failed", week, season)

    num_playoff = settings.get("num_playoff_teams")
    num_playoff = int(num_playoff) if num_playoff not in (None, "") else None

    champion_guid = None
    consolation_guid = None
    if is_finished:
        by_rank = {r["final_rank"]: r["manager_guid"] for r in standings}
        champion_guid = by_rank.get(1)
        # Yahoo ranks the consolation-bracket winner right after the
        # playoff teams (e.g. 7th with 6 playoff teams). Admins can
        # correct this with /history set-consolation.
        if num_playoff and _truthy(
            settings.get("has_playoff_consolation_games")
        ):
            consolation_guid = by_rank.get(num_playoff + 1)

    with database.atomic():
        ManagerSeason.delete().where(
            (ManagerSeason.guild_id == guild_id)
            & (ManagerSeason.season == season)
        ).execute()
        for r in standings:
            ManagerSeason.create(
                guild_id=guild_id,
                season=season,
                team_key=r["team_key"],
                team_id=r["team_id"],
                team_name=r["team_name"],
                manager_guid=r["manager_guid"],
                manager_name=r["manager_name"],
                final_rank=r["final_rank"] if is_finished else None,
                wins=r["wins"],
                losses=r["losses"],
                ties=r["ties"],
                draft_position=draft_order.get(r["manager_guid"]),
            )

        WeeklyTeamStat.delete().where(
            (WeeklyTeamStat.guild_id == guild_id)
            & (WeeklyTeamStat.season == season)
        ).execute()
        if weekly:
            rows = [dict(guild_id=guild_id, season=season, **w) for w in weekly]
            for i in range(0, len(rows), 200):
                WeeklyTeamStat.insert_many(rows[i:i + 200]).execute()

        s, _ = Season.get_or_create(
            guild_id=guild_id,
            season=season,
            defaults={"league_key": league_key},
        )
        s.league_key = league_key
        s.league_name = settings.get("name")
        s.is_finished = is_finished
        s.num_playoff_teams = num_playoff
        if not s.champion_overridden:
            s.champion_guid = champion_guid
        if not s.consolation_overridden:
            s.consolation_guid = consolation_guid
        s.synced_at = datetime.datetime.utcnow()
        s.save()

    return {
        "season": season,
        "teams": len(standings),
        "weeks": len({w["week"] for w in weekly}),
        "finished": is_finished,
        "has_draft": bool(draft_order),
    }
