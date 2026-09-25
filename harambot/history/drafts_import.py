"""Import past draft picks from a CSV (league_drafts.csv).

One row per pick:

    season,round,pick,manager,team,player

Manager names must be spelled the same way as in league_history.csv.
"""

import csv
import io
import re

from harambot.database.history_models import DraftPick
from harambot.database.models import database
from harambot.history.manual_import import manager_key

REQUIRED = {"season", "round", "pick", "manager", "player"}


def looks_like_drafts(text):
    first = text.lstrip("\ufeff").splitlines()[0].lower() if text else ""
    return "player" in first and "round" in first


def parse_drafts_csv(text):
    """Returns (seasons, errors): seasons maps season -> list of picks."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        return {}, ["The drafts file is empty"]
    reader.fieldnames = [f.strip().lower() for f in reader.fieldnames]
    missing = REQUIRED - set(reader.fieldnames)
    if missing:
        return {}, ["Drafts file is missing column(s): {}".format(
            ", ".join(sorted(missing)))]
    seasons, errors = {}, []
    for line, row in enumerate(reader, start=2):
        row = {k: (v or "").strip() for k, v in row.items() if k}
        if not any(row.values()):
            continue
        try:
            season = int(re.match(r"\s*(\d{4})", row["season"]).group(1))
            rnd, pick = int(row["round"]), int(row["pick"])
        except (AttributeError, ValueError):
            errors.append("Drafts line {}: season, round and pick must be "
                          "numbers".format(line))
            continue
        if not row["manager"] or not row["player"]:
            errors.append("Drafts line {}: manager and player are "
                          "required".format(line))
            continue
        seasons.setdefault(season, []).append({
            "round": rnd, "pick": pick, "manager": row["manager"],
            "team": row.get("team", ""), "player": row["player"],
        })
    return seasons, errors


def save_drafts(guild_id, seasons):
    """Replace the given seasons' picks. Returns number of picks saved."""
    guild_id = str(guild_id)
    count = 0
    with database.atomic():
        for season, rows in seasons.items():
            DraftPick.delete().where(
                (DraftPick.guild_id == guild_id)
                & (DraftPick.season == season)
            ).execute()
            for r in rows:
                DraftPick.create(
                    guild_id=guild_id, season=season, round=r["round"],
                    pick=r["pick"], manager_guid=manager_key(r["manager"]),
                    manager_name=r["manager"], team_name=r["team"],
                    player=r["player"],
                )
                count += 1
    return count
