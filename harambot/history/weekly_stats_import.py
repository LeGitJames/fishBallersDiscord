"""Import every team's weekly category totals (league_weekly_stats.csv),
and the weeks to leave out of the records (league_odd_weeks.csv).

league_weekly_stats.csv, one row per team per week:

    season,week,manager,team,is_playoffs,fgm,fga,ftm,fta,3ptm,pts,reb,ast,st,blk,to

FG% and FT% are worked out from the makes and attempts.

league_odd_weeks.csv, one row per week to ignore in /records:

    season,week,reason
"""

import csv
import io
import re

from harambot.database.history_models import OddWeek, WeeklyTeamStat
from harambot.database.models import database
from harambot.history.manual_import import manager_key

COUNTS = {"3ptm": "10", "pts": "12", "reb": "15", "ast": "16", "st": "17",
          "blk": "18", "to": "19"}
SHOOTING = ("fgm", "fga", "ftm", "fta")
REQUIRED = {"season", "week", "manager"} | set(COUNTS) | set(SHOOTING)


def _season(value):
    m = re.match(r"\s*(\d{4})", value or "")
    return int(m.group(1)) if m else None


def _reader(text):
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if reader.fieldnames:
        reader.fieldnames = [f.strip().lower() for f in reader.fieldnames]
    return reader


def looks_like_weekly_stats(text):
    first = text.lstrip("﻿").splitlines()[0].lower() if text else ""
    return "fga" in first and "manager" in first


def parse_weekly_stats_csv(text):
    """Returns (seasons, errors): seasons maps season -> list of rows."""
    reader = _reader(text)
    if not reader.fieldnames:
        return {}, ["The weekly stats file is empty"]
    missing = REQUIRED - set(reader.fieldnames)
    if missing:
        return {}, ["Weekly stats file is missing column(s): {}".format(
            ", ".join(sorted(missing)))]
    seasons, errors = {}, []
    for line, row in enumerate(reader, start=2):
        row = {k: (v or "").strip() for k, v in row.items() if k}
        if not any(row.values()):
            continue
        season = _season(row["season"])
        try:
            week = int(row["week"])
            nums = {k: float(row[k].replace(",", ""))
                    for k in list(COUNTS) + list(SHOOTING)}
        except ValueError:
            errors.append("Weekly stats line {}: numbers expected".format(
                line))
            continue
        if season is None or not row["manager"]:
            errors.append("Weekly stats line {}: season and manager are "
                          "required".format(line))
            continue
        seasons.setdefault(season, []).append(dict(
            nums, week=week, manager=row["manager"],
            team=row.get("team", ""),
            is_playoffs=row.get("is_playoffs", "").lower() in (
                "1", "yes", "y", "true", "x")))
    return seasons, errors


def save_weekly_stats(guild_id, seasons):
    """Replace the given seasons' weekly totals. Returns team-weeks saved."""
    guild_id = str(guild_id)
    count = 0
    with database.atomic():
        for season, rows in seasons.items():
            WeeklyTeamStat.delete().where(
                (WeeklyTeamStat.guild_id == guild_id)
                & (WeeklyTeamStat.season == season)).execute()
            batch = []
            for r in rows:
                guid = manager_key(r["manager"])
                base = dict(guild_id=guild_id, season=season, week=r["week"],
                            is_playoffs=r["is_playoffs"],
                            team_key="manual.{}.{}".format(season, guid),
                            team_name=r["team"], manager_guid=guid,
                            manager_name=r["manager"])
                stats = {sid: r[col] for col, sid in COUNTS.items()}
                if r["fga"]:
                    stats["5"] = r["fgm"] / r["fga"]
                if r["fta"]:
                    stats["8"] = r["ftm"] / r["fta"]
                for col in SHOOTING:
                    stats[col] = r[col]
                batch += [dict(base, stat_id=sid, value=v)
                          for sid, v in stats.items()]
                count += 1
            for i in range(0, len(batch), 200):
                WeeklyTeamStat.insert_many(batch[i:i + 200]).execute()
    return count


def looks_like_odd_weeks(text):
    first = text.lstrip("﻿").splitlines()[0].lower() if text else ""
    return "reason" in first and "week" in first


def parse_odd_weeks_csv(text):
    reader = _reader(text)
    if not reader.fieldnames or not {"season", "week"} <= set(
            reader.fieldnames):
        return {}, ["Odd weeks file needs season and week columns"]
    weeks, errors = [], []
    for line, row in enumerate(reader, start=2):
        row = {k: (v or "").strip() for k, v in row.items() if k}
        if not any(row.values()):
            continue
        season = _season(row.get("season"))
        try:
            week = int(row.get("week"))
        except (TypeError, ValueError):
            week = None
        if season is None or week is None:
            errors.append("Odd weeks line {}: season and week "
                          "needed".format(line))
            continue
        weeks.append((season, week, row.get("reason", "")))
    return {"weeks": weeks}, errors


def save_odd_weeks(guild_id, parsed):
    guild_id = str(guild_id)
    with database.atomic():
        OddWeek.delete().where(OddWeek.guild_id == guild_id).execute()
        for season, week, reason in parsed.get("weeks", []):
            OddWeek.create(guild_id=guild_id, season=season, week=week,
                           reason=reason)
    return len(parsed.get("weeks", []))
