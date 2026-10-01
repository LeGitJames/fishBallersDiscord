"""Live draft feed: turn new Yahoo draft picks into chat lines, with a bit
of league history mixed in.

``new_picks`` does the Yahoo calls (run it in a thread); ``pick_notes`` and
``format_pick`` only read the database, so they can be unit tested.
"""

import logging

from harambot.database.history_models import DraftPick, Season
from harambot.history import parsers
from harambot.history.queries import season_label
from harambot.history.sync import ManagerResolver, _team_lookup

logger = logging.getLogger("discord.harambot.history.draft_feed")


def new_picks(guild_id, league, after):
    """Picks made since overall pick ``after``.

    Returns (season, picks, finished) where each pick is a dict with
    overall, round, pick (within the round), manager_guid, manager_name,
    team_name, player, position, nba_team."""
    settings = league.settings()
    season = int(settings["season"])
    results = [p for p in (league.draft_results() or [])
               if p.get("player_id")]
    fresh = sorted((p for p in results if int(p["pick"]) > after),
                   key=lambda p: int(p["pick"]))
    who = ManagerResolver(guild_id)
    teams = _team_lookup(guild_id, season, league, who)
    num_teams = max(len(teams), 1)
    details = {}
    ids = [int(p["player_id"]) for p in fresh]
    for i in range(0, len(ids), 25):
        try:
            for d in league.player_details(ids[i:i + 25]) or []:
                details[str(d.get("player_id"))] = d
        except Exception:
            logger.exception("Could not look up drafted players")
    picks = []
    for p in fresh:
        d = details.get(str(p["player_id"]), {})
        name = d.get("name")
        if isinstance(name, dict):
            name = name.get("full")
        guid, mgr, team = teams.get(p.get("team_key"),
                                    (None, "?", p.get("team_key", "")))
        rnd, overall = int(p["round"]), int(p["pick"])
        picks.append({
            "overall": overall, "round": rnd,
            "pick": overall - (rnd - 1) * num_teams,
            "manager_guid": guid, "manager_name": mgr, "team_name": team,
            "player": name or "Player #{}".format(p["player_id"]),
            "position": d.get("display_position", ""),
            "nba_team": d.get("editorial_team_abbr", ""),
        })
    who.save()
    return season, picks, settings.get("draft_status") == "postdraft"


def pick_notes(guild_id, season, pick):
    """A few lines of history about this pick (may be empty)."""
    guild_id = str(guild_id)
    player = pick["player"]
    guid = pick["manager_guid"]
    notes = []
    before = list(DraftPick.select().where(
        (DraftPick.guild_id == guild_id) & (DraftPick.player == player)
        & (DraftPick.season < season)).order_by(DraftPick.season.desc()))
    mine = [p for p in before if p.manager_guid == guid]
    if len(mine) >= 2:
        notes.append("❤️ {} has drafted {} {} times before ({})".format(
            pick["manager_name"], player, len(mine),
            ", ".join(season_label(p.season)[2:] for p in reversed(mine))))
    elif len(mine) == 1:
        notes.append("🔁 {} drafted him before too ({}, round {})".format(
            pick["manager_name"], season_label(mine[0].season),
            mine[0].round))
    if before and before[0].season == season - 1 \
            and before[0].manager_guid != guid:
        last = before[0]
        diff = last.round - pick["round"]
        move = ""
        if diff >= 2:
            move = ", {} rounds earlier this time".format(diff)
        elif diff <= -2:
            move = ", slid {} rounds this year".format(-diff)
        notes.append("📜 Last year: {}'s round {} pick{}".format(
            last.manager_name, last.round, move))
    elif not before and pick["round"] <= 3:
        notes.append("🆕 First time anyone in the league has drafted him")
    if pick["overall"] == 1:
        firsts = list(DraftPick.select().where(
            (DraftPick.guild_id == guild_id) & (DraftPick.round == 1)
            & (DraftPick.pick == 1) & (DraftPick.season < season))
            .order_by(DraftPick.season.desc()).limit(3))
        if firsts:
            notes.append("🎯 Recent #1 picks: " + ", ".join(
                "{} ({}, {})".format(p.player, p.manager_name,
                                     season_label(p.season))
                for p in firsts))
    return notes


def save_pick(guild_id, season, pick):
    """Keep the pick so /drafts, /favourites etc. work straight away.
    Skipped for seasons that came from the history files."""
    s = Season.get_or_none((Season.guild_id == str(guild_id))
                           & (Season.season == season))
    if s is not None and s.league_key == "manual" and s.is_finished:
        return
    DraftPick.delete().where(
        (DraftPick.guild_id == str(guild_id)) & (DraftPick.season == season)
        & (DraftPick.round == pick["round"])
        & (DraftPick.pick == pick["pick"])).execute()
    if pick["manager_guid"]:
        DraftPick.create(
            guild_id=str(guild_id), season=season, round=pick["round"],
            pick=pick["pick"], manager_guid=pick["manager_guid"],
            manager_name=pick["manager_name"], team_name=pick["team_name"],
            player=pick["player"])


def format_pick(pick, notes, mention=None):
    head = "`R{}.{:02d}` **#{}** {}{} takes **{}**{}".format(
        pick["round"], pick["pick"], pick["overall"], pick["manager_name"],
        " ({})".format(mention) if mention else "", pick["player"],
        " · {} {}".format(pick["nba_team"], pick["position"]).rstrip()
        if pick["nba_team"] or pick["position"] else "")
    lines = []
    if pick["pick"] == 1:
        lines.append("**━━ Round {} ━━**".format(pick["round"]))
    lines.append(head)
    lines += ["> " + n for n in notes]
    return "\n".join(lines)
