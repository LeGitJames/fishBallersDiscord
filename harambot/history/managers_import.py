"""Tell /history sync who's who on Yahoo (league_managers.csv).

One row per Yahoo name:

    yahoo_name,manager

``yahoo_name`` is a Yahoo manager nickname (e.g. TheRealJosh) or a team
name; ``manager`` is the name used in league_history.csv. /history sync
matches most managers on its own (by team name, or a nickname that's the
same as their name); this file covers the rest, e.g. someone who renamed
their team over the summer and whose Yahoo nickname isn't their name.
"""

import csv
import io

from harambot.database.history_models import ManagerAlias
from harambot.database.models import database
from harambot.history.manual_import import manager_key


def looks_like_managers(text):
    first = text.lstrip("﻿").splitlines()[0].lower() if text else ""
    return "yahoo_name" in first


def parse_managers_csv(text):
    """Returns ({"aliases": [(yahoo_name, manager)]}, errors)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        return {}, ["The managers file is empty"]
    reader.fieldnames = [f.strip().lower() for f in reader.fieldnames]
    missing = {"yahoo_name", "manager"} - set(reader.fieldnames)
    if missing:
        return {}, ["Managers file is missing column(s): {}".format(
            ", ".join(sorted(missing)))]
    aliases, errors = [], []
    for line, row in enumerate(reader, start=2):
        row = {k: (v or "").strip() for k, v in row.items() if k}
        if not any(row.values()):
            continue
        if not row["yahoo_name"] or not row["manager"]:
            errors.append("Managers line {}: yahoo_name and manager are "
                          "both required".format(line))
            continue
        aliases.append((row["yahoo_name"], row["manager"]))
    return {"aliases": aliases}, errors


def save_managers(guild_id, parsed):
    """Replace the guild's Yahoo-name aliases. Returns how many."""
    guild_id = str(guild_id)
    with database.atomic():
        ManagerAlias.delete().where(
            (ManagerAlias.guild_id == guild_id)
            & ManagerAlias.yahoo_name.is_null(False)
        ).execute()
        for yahoo_name, manager in parsed.get("aliases", []):
            ManagerAlias.create(
                guild_id=guild_id, yahoo_name=yahoo_name,
                manager_guid=manager_key(manager), manager_name=manager)
    return len(parsed.get("aliases", []))
