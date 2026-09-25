"""Import all-time category records (league_records.csv), copied from
Yahoo's Record Book. One row per record:

    stat_id,scope,kind,season,week,manager,team,value

scope is "week", "season" or "lead_streak" (most weeks in a row leading
the league in that category: week = first week, value = number of weeks);
kind is "best" or "worst". Ties are separate rows. Manager names must match league_history.csv.
"""

import csv
import io
import re

from harambot.database.history_models import RecordBookEntry
from harambot.database.models import database
from harambot.history.manual_import import manager_key

REQUIRED = {"stat_id", "scope", "kind", "season", "manager", "value"}


def looks_like_records(text):
    first = text.lstrip("\ufeff").splitlines()[0].lower() if text else ""
    return "stat_id" in first and "scope" in first


def parse_records_csv(text):
    """Returns ({0: rows}, errors). The whole file replaces what's saved."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        return {}, ["The records file is empty"]
    reader.fieldnames = [f.strip().lower() for f in reader.fieldnames]
    missing = REQUIRED - set(reader.fieldnames)
    if missing:
        return {}, ["Records file is missing column(s): {}".format(
            ", ".join(sorted(missing)))]
    rows, errors = [], []
    for line, row in enumerate(reader, start=2):
        row = {k: (v or "").strip() for k, v in row.items() if k}
        if not any(row.values()):
            continue
        try:
            season = int(re.match(r"\s*(\d{4})", row["season"]).group(1))
            week = int(row["week"]) if row.get("week") else None
            value = float(row["value"])
        except (AttributeError, ValueError):
            errors.append("Records line {}: season, week and value must "
                          "be numbers".format(line))
            continue
        if row["scope"] not in ("week", "season", "lead_streak") or \
                row["kind"] not in ("best", "worst"):
            errors.append("Records line {}: scope must be week, season or "
                          "lead_streak and kind best/worst".format(line))
            continue
        rows.append({
            "stat_id": row["stat_id"], "scope": row["scope"],
            "kind": row["kind"], "season": season, "week": week,
            "manager": row["manager"], "team": row.get("team", ""),
            "value": value,
        })
    return {0: rows}, errors


def save_records(guild_id, parsed):
    guild_id = str(guild_id)
    rows = parsed.get(0, [])
    with database.atomic():
        RecordBookEntry.delete().where(
            RecordBookEntry.guild_id == guild_id).execute()
        for r in rows:
            RecordBookEntry.create(
                guild_id=guild_id, stat_id=r["stat_id"], scope=r["scope"],
                kind=r["kind"], season=r["season"], week=r["week"],
                manager_guid=manager_key(r["manager"]),
                manager_name=r["manager"], team_name=r["team"],
                value=r["value"],
            )
    return len(rows)
