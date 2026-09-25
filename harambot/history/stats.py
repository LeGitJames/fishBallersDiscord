"""League stats built from saved history: rivalries, nemeses, manager
profiles, season recaps, chokers and draft luck.

Everything here reads the database only (no Discord, no Yahoo), so it can be
unit tested.
"""

from harambot.database.history_models import (
    DraftPick,
    ManagerSeason,
    Season,
    WeeklyMatchup,
)

PLAYOFF_SPOTS = 6  # finishing ranks 1-6 made the playoffs


def _games(guild_id, guid=None):
    """Every matchup from each manager's point of view."""
    q = WeeklyMatchup.select().where(WeeklyMatchup.guild_id == str(guild_id))
    if guid:
        q = q.where((WeeklyMatchup.manager1_guid == guid)
                    | (WeeklyMatchup.manager2_guid == guid))
    out = []
    for m in q:
        sides = (
            (m.manager1_guid, m.manager1_name, m.team1, m.score1,
             m.manager2_guid, m.manager2_name, m.team2, m.score2),
            (m.manager2_guid, m.manager2_name, m.team2, m.score2,
             m.manager1_guid, m.manager1_name, m.team1, m.score1),
        )
        for (me, me_name, me_team, mine, opp, opp_name, opp_team,
             theirs) in sides:
            if guid and me != guid:
                continue
            out.append({
                "season": m.season, "week": m.week,
                "playoffs": bool(m.is_playoffs),
                "me": me, "me_name": me_name, "me_team": me_team,
                "opp": opp, "opp_name": opp_name, "opp_team": opp_team,
                "mine": mine, "theirs": theirs,
                "result": "W" if mine > theirs else "L" if mine < theirs
                else "T",
            })
    out.sort(key=lambda g: (g["season"], g["week"]))
    return out


def _record(games):
    w = sum(g["result"] == "W" for g in games)
    l_ = sum(g["result"] == "L" for g in games)
    t = sum(g["result"] == "T" for g in games)
    played = w + l_ + t
    return {"w": w, "l": l_, "t": t,
            "pct": (w + 0.5 * t) / played if played else 0.0}


def _playoff_teams(guild_id):
    """{(season, guid)} for managers who made the playoffs."""
    return {
        (r.season, r.manager_guid)
        for r in ManagerSeason.select().where(
            (ManagerSeason.guild_id == str(guild_id))
            & (ManagerSeason.final_rank.is_null(False))
            & (ManagerSeason.final_rank <= PLAYOFF_SPOTS))
    }


def playoff_round(guild_id, season, week, guids):
    """Name a playoff week: Quarter-final, Semi-final, Grand Final, or a
    placing game, based on the week's position in the playoffs and the two
    teams' final ranks."""
    first = (WeeklyMatchup.select(WeeklyMatchup.week)
             .where((WeeklyMatchup.guild_id == str(guild_id))
                    & (WeeklyMatchup.season == season)
                    & (WeeklyMatchup.is_playoffs == True))  # noqa: E712
             .order_by(WeeklyMatchup.week).first())
    if first is None:
        return "Playoffs"
    n = week - first.week + 1
    ranks = sorted(
        r.final_rank or 99 for r in ManagerSeason.select().where(
            (ManagerSeason.guild_id == str(guild_id))
            & (ManagerSeason.season == season)
            & (ManagerSeason.manager_guid.in_(list(guids)))))
    if n >= 3:
        if ranks[:2] == [1, 2]:
            return "Grand Final"
        if ranks[:2] == [3, 4]:
            return "3rd-place game"
        return "5th-place game"
    if n == 2:
        if ranks and ranks[0] >= 5:
            return "5th-place game"
        return "Semi-final"
    return "Quarter-final"


def head_to_head(guild_id, a, b):
    """Rivalry summary between managers ``a`` and ``b`` (GUIDs), from a's
    point of view."""
    games = [g for g in _games(guild_id, a) if g["opp"] == b]
    if not games:
        return None
    bracket = _playoff_teams(guild_id)
    playoff_meetings = [
        g for g in games if g["playoffs"]
        and (g["season"], a) in bracket and (g["season"], b) in bracket
    ]
    wins = [g for g in games if g["result"] == "W"]
    losses = [g for g in games if g["result"] == "L"]

    def margin(g):
        return abs(g["mine"] - g["theirs"])

    streak_kind, streak = None, 0
    for g in reversed(games):
        if g["result"] == "T":
            break
        if streak_kind is None:
            streak_kind = g["result"]
        if g["result"] != streak_kind:
            break
        streak += 1
    for g in playoff_meetings:
        g["round"] = playoff_round(guild_id, g["season"], g["week"], (a, b))
    return {
        "games": games,
        "overall": _record(games),
        "regular": _record([g for g in games if not g["playoffs"]]),
        "playoff_meetings": playoff_meetings,
        "biggest_win": max(wins, key=margin) if wins else None,
        "biggest_loss": max(losses, key=margin) if losses else None,
        "last": games[-5:],
        "streak": (streak_kind, streak),
    }


MIN_RIVAL_GAMES = 5


def opponents(guild_id, guid, min_games=MIN_RIVAL_GAMES):
    """Record against every opponent with at least ``min_games`` meetings,
    plus nemesis and punching bag. ``hidden`` counts opponents left out."""
    by_opp = {}
    for g in _games(guild_id, guid):
        by_opp.setdefault(g["opp"], {"name": g["opp_name"], "games": []})
        by_opp[g["opp"]]["name"] = g["opp_name"]
        by_opp[g["opp"]]["games"].append(g)
    rows = []
    for opp, d in by_opp.items():
        rec = _record(d["games"])
        rows.append(dict(rec, opp=opp, name=d["name"],
                         games=len(d["games"])))
    rows.sort(key=lambda r: (-r["pct"], -r["w"]))
    eligible = [r for r in rows if r["games"] >= min_games]
    nemesis = max(eligible, key=lambda r: (r["l"], -r["pct"]),
                  default=None)
    punching_bag = max(eligible, key=lambda r: (r["w"], r["pct"]),
                       default=None)
    return {"rows": eligible, "hidden": len(rows) - len(eligible),
            "min_games": min_games, "nemesis": nemesis,
            "punching_bag": punching_bag}


def profile(guild_id, guid):
    """Everything about one manager."""
    guild_id = str(guild_id)
    seasons = list(ManagerSeason.select().where(
        (ManagerSeason.guild_id == guild_id)
        & (ManagerSeason.manager_guid == guid)
    ).order_by(ManagerSeason.season))
    if not seasons:
        return None
    finished = [s for s in seasons if s.final_rank]
    title_years = [s.season for s in seasons if s.final_rank == 1]
    finals = [s.season for s in seasons if s.final_rank == 2]
    playoffs = [s for s in finished if s.final_rank <= PLAYOFF_SPOTS
                and s.season != 2019]  # COVID year had no playoffs
    games = _games(guild_id, guid)
    regular = [g for g in games if not g["playoffs"]]
    sweeps_given = sum(g["mine"] >= 9 and g["theirs"] == 0 for g in games)
    sweeps_taken = sum(g["theirs"] >= 9 and g["mine"] == 0 for g in games)
    fav = (DraftPick.select(DraftPick.player)
           .where((DraftPick.guild_id == guild_id)
                  & (DraftPick.manager_guid == guid)))
    counts = {}
    for p in fav:
        counts[p.player] = counts.get(p.player, 0) + 1
    favourite = max(counts.items(), key=lambda kv: kv[1]) if counts \
        else None
    best = min(finished, key=lambda s: (s.final_rank, -s.season),
               default=None)
    worst = max(finished, key=lambda s: (s.final_rank, s.season),
                default=None)
    return {
        "name": seasons[-1].manager_name,
        "team": seasons[-1].team_name,
        "first_season": seasons[0].season,
        "last_season": seasons[-1].season,
        "seasons": len(seasons),
        "titles": title_years,
        "finals": finals,
        "playoffs": len(playoffs),
        "best": best,
        "worst": worst,
        "record": _record(regular) if regular else None,
        "playoff_record": _record([g for g in games if g["playoffs"]]),
        "sweeps_given": sweeps_given,
        "sweeps_taken": sweeps_taken,
        "favourite": favourite,
        "opponents": opponents(guild_id, guid),
    }


def chokers(guild_id, limit=3):
    """Best regular seasons that didn't end in a title, and most runner-up
    finishes."""
    rows = [
        r for r in ManagerSeason.select().where(
            (ManagerSeason.guild_id == str(guild_id))
            & ManagerSeason.final_rank.is_null(False))
        if (r.wins + r.losses + r.ties) > 0
    ]

    def pct(r):
        return (r.wins + 0.5 * r.ties) / (r.wins + r.losses + r.ties)

    no_title = sorted((r for r in rows if r.final_rank != 1),
                      key=lambda r: (-pct(r), r.season))[:limit]
    runner_ups = {}
    for r in ManagerSeason.select().where(
            (ManagerSeason.guild_id == str(guild_id))
            & (ManagerSeason.final_rank == 2)):
        d = runner_ups.setdefault(r.manager_guid,
                                  {"name": r.manager_name, "seasons": []})
        d["seasons"].append(r.season)
    bridesmaids = sorted(
        ({"guid": g, **d} for g, d in runner_ups.items()),
        key=lambda d: (-len(d["seasons"]), d["name"]))
    return {"no_title": no_title, "pct": pct, "bridesmaids": bridesmaids}


MIN_LUCK_SEASONS = 3


def draft_luck(guild_id):
    """How first-round draft position lined up with final finish."""
    rows = list(ManagerSeason.select().where(
        (ManagerSeason.guild_id == str(guild_id))
        & ManagerSeason.draft_position.is_null(False)
        & ManagerSeason.final_rank.is_null(False)))
    champions = sorted((r for r in rows if r.final_rank == 1),
                       key=lambda r: -r.season)
    first_picks = sorted((r for r in rows if r.draft_position == 1),
                         key=lambda r: -r.season)
    by_slot = {}
    for r in rows:
        by_slot.setdefault(r.draft_position, []).append(r.final_rank)
    avg_by_slot = {
        slot: sum(v) / len(v) for slot, v in sorted(by_slot.items())
    }
    glow_ups = sorted(rows, key=lambda r: (
        -(r.draft_position - r.final_rank), r.season))[:3]
    flops = sorted(rows, key=lambda r: (
        -(r.final_rank - r.draft_position), r.season))[:3]
    by_mgr = {}
    for r in ManagerSeason.select().where(
            (ManagerSeason.guild_id == str(guild_id))
            & ManagerSeason.draft_position.is_null(False)):
        d = by_mgr.setdefault(r.manager_guid, {
            "guid": r.manager_guid, "name": r.manager_name, "picks": []})
        d["name"] = r.manager_name
        d["picks"].append(r.draft_position)
    lottery = [
        dict(d, avg=sum(d["picks"]) / len(d["picks"]),
             seasons=len(d["picks"]),
             top3=sum(p <= 3 for p in d["picks"]))
        for d in by_mgr.values() if len(d["picks"]) >= MIN_LUCK_SEASONS
    ]
    luckiest = sorted(lottery, key=lambda d: (d["avg"], -d["seasons"]))[:3]
    unluckiest = sorted(lottery, key=lambda d: (-d["avg"],
                                                -d["seasons"]))[:3]
    return {"luckiest": luckiest, "unluckiest": unluckiest,
            "champions": champions, "first_picks": first_picks,
            "avg_by_slot": avg_by_slot, "glow_ups": glow_ups,
            "flops": flops}


def season_recap(guild_id, season):
    """Standings plus the notable moments of one season."""
    guild_id = str(guild_id)
    s = Season.get_or_none((Season.guild_id == guild_id)
                           & (Season.season == season))
    if s is None:
        return None
    standings = sorted(
        ManagerSeason.select().where(
            (ManagerSeason.guild_id == guild_id)
            & (ManagerSeason.season == season)),
        key=lambda r: (r.final_rank is None, r.final_rank or 0, -r.wins))
    games = [g for g in _games(guild_id) if g["season"] == season]
    sweeps = [g for g in games if g["mine"] >= 9 and g["theirs"] == 0]
    blowouts = sorted(
        (g for g in games if g["result"] == "W"),
        key=lambda g: -(g["mine"] - g["theirs"]))
    # longest streaks this season
    streaks = {"W": None, "L": None}
    by_mgr = {}
    for g in games:
        by_mgr.setdefault(g["me"], []).append(g)
    for mgr_games in by_mgr.values():
        for kind in ("W", "L"):
            run, best, start, best_range = 0, 0, None, None
            for g in mgr_games:
                if g["result"] == kind:
                    if run == 0:
                        start = g["week"]
                    run += 1
                    if run > best:
                        best, best_range = run, (start, g["week"])
                else:
                    run = 0
            cur = streaks[kind]
            if best and (cur is None or best > cur["length"]):
                streaks[kind] = {"length": best, "name": mgr_games[0][
                    "me_name"], "guid": mgr_games[0]["me"],
                    "team": mgr_games[0]["me_team"], "range": best_range}
    first_pick = next((r for r in standings if r.draft_position == 1), None)
    first_pick_player = None
    if first_pick:
        pick = DraftPick.get_or_none(
            (DraftPick.guild_id == guild_id) & (DraftPick.season == season)
            & (DraftPick.round == 1) & (DraftPick.pick == 1))
        first_pick_player = pick.player if pick else None
    return {
        "first_pick_player": first_pick_player,
        "season": s, "standings": standings, "sweeps": sweeps,
        "biggest_blowout": blowouts[0] if blowouts else None,
        "streaks": streaks, "first_pick": first_pick,
    }
