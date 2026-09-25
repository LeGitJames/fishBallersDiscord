"""League-history tables: past seasons, per-manager results, weekly
category totals, and saved draft lotteries.

Everything is scoped by ``guild_id`` like the rest of harambot, so one bot
can serve several Discord servers. Managers are linked across seasons by
their Yahoo manager GUID, because team names change from year to year.
"""

import datetime

from peewee import (
    BooleanField,
    DateTimeField,
    FloatField,
    IntegerField,
    TextField,
)

from harambot.database.models import BaseModel, database


class Season(BaseModel):
    guild_id = TextField()
    season = IntegerField()  # e.g. 2025 for the 2025-26 NBA season
    league_key = TextField()  # e.g. "454.l.12345"
    league_name = TextField(null=True)
    is_finished = BooleanField(default=False)
    num_playoff_teams = IntegerField(null=True)
    # Manager GUIDs. Filled in by sync; admins can override them.
    champion_guid = TextField(null=True)
    consolation_guid = TextField(null=True)
    consolation_overridden = BooleanField(default=False)
    champion_overridden = BooleanField(default=False)
    synced_at = DateTimeField(default=datetime.datetime.utcnow)

    class Meta:
        indexes = ((("guild_id", "season"), True),)


class ManagerSeason(BaseModel):
    guild_id = TextField()
    season = IntegerField()
    team_key = TextField()
    team_id = TextField()
    team_name = TextField()
    manager_guid = TextField()
    manager_name = TextField()
    final_rank = IntegerField(null=True)
    wins = IntegerField(default=0)
    losses = IntegerField(default=0)
    ties = IntegerField(default=0)
    draft_position = IntegerField(null=True)  # first-round pick number

    class Meta:
        indexes = ((("guild_id", "season", "team_key"), True),)


class WeeklyTeamStat(BaseModel):
    """One category total for one team in one completed week."""

    guild_id = TextField()
    season = IntegerField()
    week = IntegerField()
    is_playoffs = BooleanField(default=False)
    team_key = TextField()
    team_name = TextField()
    manager_guid = TextField()
    manager_name = TextField()
    stat_id = TextField()
    value = FloatField()

    class Meta:
        indexes = (
            (("guild_id", "season", "week", "team_key", "stat_id"), True),
            (("guild_id", "stat_id"), False),
        )


class DraftLottery(BaseModel):
    guild_id = TextField()
    season = IntegerField()  # the season being drafted
    run_at = DateTimeField(default=datetime.datetime.utcnow)
    run_by = TextField()  # Discord user id
    # JSON list of {"pick", "manager_guid", "manager_name", "team_name",
    # "balls"}
    results = TextField()


HISTORY_TABLES = [Season, ManagerSeason, WeeklyTeamStat, DraftLottery]


def create_history_tables():
    database.create_tables(HISTORY_TABLES, safe=True)
