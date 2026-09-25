"""Import league history from a CSV file instead of Yahoo.

For when the Yahoo API isn't available. One row per manager per season:

    season,manager,team,finish,draft_pick,consolation_winner,wins,losses,ties,took_over_from,note,badge

Only ``season`` and ``manager`` are required. Leave ``finish`` blank for a
season that hasn't been played yet. See docs/league_history_template.csv.

Seasons imported this way are marked with league_key "manual", and a later
``/history sync`` from Yahoo replaces them.
"""

import csv
import io
import re

from harambot.database.history_models import ManagerSeason, Season
from harambot.database.models import database

MANUAL_LEAGUE_KEY = "manual"
YES = {"y", "yes", "true", "1", "x"}


def manager_key(name):
    """Stable ID for a manager typed by hand: case and spacing don't
    matter, so "Josh" and " josh " are the same person."""
    return "manual:" + " ".join(name.lower().split())


def _int(value, field, line, errors, required=False):
    value = (value or "").strip()
    if not value:
        if required:
            errors.append("Line {}: {} is required".format(line, field))
        return None
    try:
        return int(value)
    except ValueError:
        errors.append(
            "Line {}: {} should be a whole number, got '{}'".format(
                line, field, value
            )
        )
        return None


def _season(value, line, errors):
    # Accept "2025" or "2025-26"
    m = re.match(r"\s*(\d{4})", value or "")
    if not m:
        errors.append(
            "Line {}: season should look like 2025 or 2025-26, got '{}'"
            .format(line, value)
        )
        return None
    return int(m.group(1))


def parse_history_csv(text):
    """Returns (seasons, errors). ``seasons`` maps season -> list of row
    dicts. Nothing is saved if ``errors`` is not empty."""
    text = text.lstrip("﻿")  # Excel adds a byte-order mark
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return {}, ["The file is empty"]
    reader.fieldnames = [f.strip().lower() for f in reader.fieldnames]
    missing = {"season", "manager"} - set(reader.fieldnames)
    if missing:
        return {}, [
            "Missing column(s): {}. The first row must be the column "
            "names from the template.".format(", ".join(sorted(missing)))
        ]

    seasons, errors = {}, []
    for line, row in enumerate(reader, start=2):
        row = {k: (v or "").strip() for k, v in row.items() if k}
        if not any(row.values()):
            continue  # blank line
        season = _season(row.get("season"), line, errors)
        manager = row.get("manager", "")
        if not manager:
            errors.append("Line {}: manager is required".format(line))
            continue
        if season is None:
            continue
        key = manager_key(manager)
        took_over = row.get("took_over_from", "")
        seasons.setdefault(season, []).append(
            {
                "line": line,
                "manager_guid": key,
                "manager_name": manager,
                "team_name": row.get("team") or manager,
                # A new manager who took over a team inherits its slot, so
                # the lottery can find that team's previous draft pick.
                "team_id": manager_key(took_over) if took_over else key,
                "final_rank": _int(row.get("finish"), "finish", line,
                                   errors),
                "draft_position": _int(row.get("draft_pick"), "draft_pick",
                                       line, errors),
                "consolation": row.get("consolation_winner", "").lower()
                in YES,
                "wins": _int(row.get("wins"), "wins", line, errors) or 0,
                "losses": _int(row.get("losses"), "losses", line,
                               errors) or 0,
                "ties": _int(row.get("ties"), "ties", line, errors) or 0,
                "note": row.get("note", ""),
                "badge": row.get("badge", ""),
            }
        )

    for season, rows in seasons.items():
        _check_season(season, rows, errors)
    return seasons, errors


def _check_season(season, rows, errors):
    label = "Season {}".format(season)
    names = [r["manager_guid"] for r in rows]
    for guid in set(names):
        if names.count(guid) > 1:
            name = next(r["manager_name"] for r in rows
                        if r["manager_guid"] == guid)
            errors.append("{}: {} is listed twice".format(label, name))
    for field, what in (("final_rank", "finish"),
                        ("draft_position", "draft_pick")):
        values = [r[field] for r in rows if r[field] is not None]
        for v in set(values):
            if values.count(v) > 1:
                errors.append("{}: two managers have {} {}".format(
                    label, what, v))
    finishes = [r["final_rank"] for r in rows]
    if any(f is not None for f in finishes):
        if None in finishes:
            errors.append(
                "{}: fill in finish for every manager, or for none (a "
                "season not played yet)".format(label))
        elif 1 not in finishes:
            errors.append("{}: nobody has finish 1 (the champion)".format(
                label))
    if sum(r["consolation"] for r in rows) > 1:
        errors.append("{}: only one consolation winner allowed".format(
            label))


def save_history(guild_id, seasons):
    """Replace the given seasons for this guild. Seasons already synced
    from Yahoo are left alone. Returns (saved, skipped) season lists."""
    guild_id = str(guild_id)
    saved, skipped = [], []
    with database.atomic():
        for season, rows in sorted(seasons.items()):
            existing = Season.get_or_none(
                (Season.guild_id == guild_id) & (Season.season == season)
            )
            if existing and existing.league_key != MANUAL_LEAGUE_KEY:
                skipped.append(season)
                continue
            ManagerSeason.delete().where(
                (ManagerSeason.guild_id == guild_id)
                & (ManagerSeason.season == season)
            ).execute()
            for r in rows:
                ManagerSeason.create(
                    guild_id=guild_id,
                    season=season,
                    team_key="manual.{}.{}".format(season, r["manager_guid"]),
                    team_id=r["team_id"],
                    team_name=r["team_name"],
                    manager_guid=r["manager_guid"],
                    manager_name=r["manager_name"],
                    final_rank=r["final_rank"],
                    wins=r["wins"],
                    losses=r["losses"],
                    ties=r["ties"],
                    draft_position=r["draft_position"],
                )
            finished = all(r["final_rank"] is not None for r in rows)
            champion = next(
                (r["manager_guid"] for r in rows if r["final_rank"] == 1),
                None,
            )
            consolation = next(
                (r["manager_guid"] for r in rows if r["consolation"]), None
            )
            s = existing or Season(guild_id=guild_id, season=season)
            s.league_key = MANUAL_LEAGUE_KEY
            s.league_name = None
            s.is_finished = finished
            s.note = next((r["note"] for r in rows if r["note"]), None)
            s.badge = next((r["badge"] for r in rows if r["badge"]), None)
            s.champion_guid = champion
            s.consolation_guid = consolation
            s.champion_overridden = False
            s.consolation_overridden = False
            s.save()
            saved.append(season)
    return saved, skipped
