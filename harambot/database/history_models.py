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
    # Shown under the season in /trophycase, e.g. "COVID year".
    note = TextField(null=True)
    # Extra emoji shown next to the trophy, e.g. "🪠"
    badge = TextField(null=True)

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


class WeeklyMatchup(BaseModel):
    """One head-to-head result: categories won by each side that week."""

    guild_id = TextField()
    season = IntegerField()
    week = IntegerField()
    manager1_guid = TextField()
    manager1_name = TextField()
    team1 = TextField(null=True)
    score1 = FloatField()
    manager2_guid = TextField()
    manager2_name = TextField()
    team2 = TextField(null=True)
    score2 = FloatField()
    is_playoffs = BooleanField(default=False)

    class Meta:
        indexes = ((("guild_id", "season", "week"), False),)


class DraftPick(BaseModel):
    """One pick from a past draft."""

    guild_id = TextField()
    season = IntegerField()
    round = IntegerField()
    pick = IntegerField()  # pick number within the round
    manager_guid = TextField()
    manager_name = TextField()
    team_name = TextField(null=True)
    player = TextField()

    class Meta:
        indexes = ((("guild_id", "manager_guid"), False),)


class RecordBookEntry(BaseModel):
    """An all-time #1 from Yahoo's Record Book (used until weekly stats
    can be synced from the Yahoo API)."""

    guild_id = TextField()
    stat_id = TextField()
    scope = TextField()  # "week" or "season"
    kind = TextField()  # "best" or "worst"
    season = IntegerField()
    week = IntegerField(null=True)
    manager_guid = TextField()
    manager_name = TextField()
    team_name = TextField(null=True)
    value = FloatField()


class DiscordLink(BaseModel):
    """Which Discord account belongs to a league manager, for @mentions."""

    guild_id = TextField()
    manager_guid = TextField()
    discord_user_id = TextField()

    class Meta:
        indexes = ((("guild_id", "manager_guid"), True),)


HISTORY_TABLES = [
    Season,
    ManagerSeason,
    WeeklyTeamStat,
    DraftLottery,
    DiscordLink,
    WeeklyMatchup,
    DraftPick,
    RecordBookEntry,
]


def create_history_tables():
    database.create_tables(HISTORY_TABLES, safe=True)
    _add_missing_columns()


def _add_missing_columns():
    """Add columns introduced after a database was first created, so
    existing databases keep working without a manual migration."""
    from playhouse.migrate import SchemaMigrator, migrate

    migrator = SchemaMigrator.from_database(database)
    for model in HISTORY_TABLES:
        table = model._meta.table_name
        existing = {c.name for c in database.get_columns(table)}
        for field in model._meta.sorted_fields:
            if field.column_name not in existing:
                migrate(migrator.add_column(table, field.column_name, field))
