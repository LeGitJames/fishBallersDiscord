"""Import past trades from a CSV (league_trades.csv).

One row per trade:

    season,date,status,manager1,team1,manager1_gets,manager2,team2,manager2_gets

``manager1_gets`` is what manager1 received. Several players are joined
with " + ". ``status`` is accepted (default) or vetoed. ``date`` is free
text such as "Jan 4, 3:57 am". Within a season, rows are put in date order
(dates without a year count Aug-Dec as the season's first year).
Manager names must be spelled the same way as in league_history.csv.
"""

import csv
import io
import re
from datetime import datetime

from harambot.database.history_models import Trade
from harambot.database.models import database
from harambot.history.manual_import import manager_key

REQUIRED = {"season", "manager1", "manager1_gets", "manager2",
            "manager2_gets"}


def looks_like_trades(text):
    first = text.lstrip("﻿").splitlines()[0].lower() if text else ""
    return "manager1_gets" in first


def date_sort_key(season, date):
    """Sortable key for a Yahoo date like "Jan 4, 3:57 am"."""
    m = re.match(r"\s*([A-Za-z]{3})\w*\s+(\d{1,2})(?:,\s*(\d{1,2}):(\d{2})"
                 r"\s*([ap]m))?", date or "", re.I)
    if not m:
        return (9999, 99, 99, 99, 99)
    try:
        month = datetime.strptime(m.group(1).title(), "%b").month
    except ValueError:
        return (9999, 99, 99, 99, 99)
    year = season if month >= 8 else season + 1
    hour = int(m.group(3) or 0) % 12
    if (m.group(5) or "").lower() == "pm":
        hour += 12
    return (year, month, int(m.group(2)), hour, int(m.group(4) or 0))


def parse_trades_csv(text):
    """Returns (seasons, errors): seasons maps season -> trades in order."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        return {}, ["The trades file is empty"]
    reader.fieldnames = [f.strip().lower() for f in reader.fieldnames]
    missing = REQUIRED - set(reader.fieldnames)
    if missing:
        return {}, ["Trades file is missing column(s): {}".format(
            ", ".join(sorted(missing)))]
    seasons, errors = {}, []
    for line, row in enumerate(reader, start=2):
        row = {k: (v or "").strip() for k, v in row.items() if k}
        if not any(row.values()):
            continue
        m = re.match(r"\s*(\d{4})", row["season"])
        if not m:
            errors.append("Trades line {}: season must be a year like "
                          "2023 or 2023-24".format(line))
            continue
        if not all(row[k] for k in ("manager1", "manager2",
                                    "manager1_gets", "manager2_gets")):
            errors.append("Trades line {}: both managers and what each "
                          "got are required".format(line))
            continue
        status = (row.get("status") or "accepted").lower()
        if status not in ("accepted", "vetoed"):
            errors.append("Trades line {}: status must be accepted or "
                          "vetoed".format(line))
            continue
        season = int(m.group(1))
        seasons.setdefault(season, []).append({
            "line": line, "date": row.get("date", ""), "status": status,
            "manager1": row["manager1"], "team1": row.get("team1", ""),
            "manager1_gets": row["manager1_gets"],
            "manager2": row["manager2"], "team2": row.get("team2", ""),
            "manager2_gets": row["manager2_gets"],
        })
    for season, rows in seasons.items():
        rows.sort(key=lambda r: (date_sort_key(season, r["date"]),
                                 r["line"]))
    return seasons, errors


def save_trades(guild_id, seasons):
    """Replace the given seasons' trades. Returns number of trades."""
    guild_id = str(guild_id)
    count = 0
    with database.atomic():
        for season, rows in seasons.items():
            Trade.delete().where(
                (Trade.guild_id == guild_id) & (Trade.season == season)
            ).execute()
            for seq, r in enumerate(rows, start=1):
                Trade.create(
                    guild_id=guild_id, season=season, seq=seq,
                    date=r["date"], status=r["status"],
                    manager1_guid=manager_key(r["manager1"]),
                    manager1_name=r["manager1"], team1=r["team1"],
                    manager1_gets=r["manager1_gets"],
                    manager2_guid=manager_key(r["manager2"]),
                    manager2_name=r["manager2"], team2=r["team2"],
                    manager2_gets=r["manager2_gets"],
                )
                count += 1
    return count
