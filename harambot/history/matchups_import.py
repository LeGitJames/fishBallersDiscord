"""Import weekly head-to-head results from a CSV (league_matchups.csv).

One row per matchup:

    season,week,manager1,team1,score1,manager2,team2,score2,is_playoffs

Scores are categories won that week (e.g. 9 and 0). is_playoffs is optional
(1/yes for playoff weeks, including consolation games). Manager names must be
spelled the same way as in league_history.csv.
"""

import csv
import io
import re

from harambot.database.history_models import WeeklyMatchup
from harambot.database.models import database
from harambot.history.manual_import import manager_key

REQUIRED = {"season", "week", "manager1", "score1", "manager2", "score2"}


def looks_like_matchups(text):
    first = text.lstrip("﻿").splitlines()[0].lower() if text else ""
    return "manager1" in first and "week" in first


def parse_matchups_csv(text):
    """Returns (seasons, errors): seasons maps season -> list of rows."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        return {}, ["The matchups file is empty"]
    reader.fieldnames = [f.strip().lower() for f in reader.fieldnames]
    missing = REQUIRED - set(reader.fieldnames)
    if missing:
        return {}, ["Matchups file is missing column(s): {}".format(
            ", ".join(sorted(missing)))]
    seasons, errors = {}, []
    for line, row in enumerate(reader, start=2):
        row = {k: (v or "").strip() for k, v in row.items() if k}
        if not any(row.values()):
            continue
        try:
            season = int(re.match(r"\s*(\d{4})", row["season"]).group(1))
            week = int(row["week"])
            s1, s2 = float(row["score1"]), float(row["score2"])
        except (AttributeError, ValueError):
            errors.append("Matchups line {}: season, week and scores must "
                          "be numbers".format(line))
            continue
        if not row["manager1"] or not row["manager2"]:
            errors.append("Matchups line {}: both managers are "
                          "required".format(line))
            continue
        seasons.setdefault(season, []).append({
            "week": week,
            "manager1": row["manager1"], "team1": row.get("team1", ""),
            "score1": s1,
            "manager2": row["manager2"], "team2": row.get("team2", ""),
            "score2": s2,
            "is_playoffs": row.get("is_playoffs", "").lower() in (
                "1", "yes", "y", "true", "x"),
        })
    return seasons, errors


def save_matchups(guild_id, seasons):
    """Replace the given seasons' matchups. Returns number of rows."""
    guild_id = str(guild_id)
    count = 0
    with database.atomic():
        for season, rows in seasons.items():
            WeeklyMatchup.delete().where(
                (WeeklyMatchup.guild_id == guild_id)
                & (WeeklyMatchup.season == season)
            ).execute()
            for r in rows:
                WeeklyMatchup.create(
                    guild_id=guild_id, season=season, week=r["week"],
                    manager1_guid=manager_key(r["manager1"]),
                    manager1_name=r["manager1"], team1=r["team1"],
                    score1=r["score1"],
                    manager2_guid=manager_key(r["manager2"]),
                    manager2_name=r["manager2"], team2=r["team2"],
                    score2=r["score2"], is_playoffs=r["is_playoffs"],
                )
                count += 1
    return count
