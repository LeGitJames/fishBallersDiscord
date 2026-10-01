"""Pull league history from Yahoo into the local database.

Yahoo links each season's league to the one before it through the
``renew`` setting, so starting from the league a guild is configured with
we can walk back through every past season the league has existed.

This makes blocking HTTP calls; run it in a thread (``asyncio.to_thread``)
from Discord handlers.
"""

import datetime
import logging

from harambot.database.history_models import (
    DraftPick,
    ManagerAlias,
    ManagerSeason,
    Season,
    WeeklyMatchup,
    WeeklyTeamStat,
)
from harambot.database.models import database
from harambot.history import parsers
from harambot.history.manual_import import MANUAL_LEAGUE_KEY, manager_key

logger = logging.getLogger("discord.harambot.history.sync")

MAX_SEASONS = 25


def _truthy(value):
    return str(value).lower() in ("1", "true")


def _season_is_complete_in_db(guild_id, season):
    s = Season.get_or_none(
        (Season.guild_id == str(guild_id)) & (Season.season == season)
    )
    return s is not None and s.is_finished and \
        s.league_key != MANUAL_LEAGUE_KEY


def _season_is_locked(guild_id, season):
    """Finished seasons loaded from the history files are never touched by
    a sync: they're the league's hand-checked record."""
    s = Season.get_or_none(
        (Season.guild_id == str(guild_id)) & (Season.season == season)
    )
    return s is not None and s.is_finished and \
        s.league_key == MANUAL_LEAGUE_KEY


class ManagerResolver:
    """Maps Yahoo managers onto the managers already in the history.

    Leagues whose history came only from Yahoo keep Yahoo's ids. Once any
    history was loaded from files, every Yahoo manager is matched, in order,
    by: league_managers.csv (Yahoo nickname or team name), a Yahoo account
    matched in an earlier sync, last season's team name, then a nickname
    that's the same as a manager's name. Anyone left over is added under
    their Yahoo nickname and reported back.
    """

    def __init__(self, guild_id):
        self.guild_id = str(guild_id)
        self.active = Season.select().where(
            (Season.guild_id == self.guild_id)
            & (Season.league_key == MANUAL_LEAGUE_KEY)).exists()
        self.unmatched = []
        self.learned = {}
        if not self.active:
            return
        self.by_name, self.by_guid = {}, {}
        for a in ManagerAlias.select().where(
                ManagerAlias.guild_id == self.guild_id):
            hit = (a.manager_guid, a.manager_name)
            if a.yahoo_name:
                self.by_name[a.yahoo_name.strip().lower()] = hit
            if a.yahoo_guid:
                self.by_guid[a.yahoo_guid] = hit
        self.by_team, self.by_manager = {}, {}
        for r in ManagerSeason.select().where(
                ManagerSeason.guild_id == self.guild_id).order_by(
                    ManagerSeason.season):
            hit = (r.manager_guid, r.manager_name)
            if r.team_name:
                self.by_team[r.team_name.strip().lower()] = hit
            self.by_manager[r.manager_name.strip().lower()] = hit

    def resolve(self, guid, nickname, team_name):
        """Returns (manager_guid, manager_name)."""
        if not self.active:
            return guid, nickname
        nick = (nickname or "").strip().lower()
        team = (team_name or "").strip().lower()
        hit = (self.by_name.get(nick) or self.by_name.get(team)
               or self.by_guid.get(guid) or self.by_team.get(team)
               or self.by_manager.get(nick))
        if hit is None:
            name = nickname if nick and not nick.startswith("--") \
                else team_name
            hit = (manager_key(name), name)
            self.unmatched.append("{} ({})".format(nickname, team_name))
        if guid and not str(guid).startswith("team:"):
            self.learned[guid] = hit
        return hit

    def save(self):
        for guid, (key, name) in self.learned.items():
            ManagerAlias.delete().where(
                (ManagerAlias.guild_id == self.guild_id)
                & (ManagerAlias.yahoo_guid == guid)).execute()
            ManagerAlias.create(guild_id=self.guild_id, yahoo_guid=guid,
                                manager_guid=key, manager_name=name)


def sync_league_history(guild_id, current_league, full=False, progress=None):
    """Walk from ``current_league`` back through its ``renew`` chain.

    ``current_league`` is a ``yahoo_fantasy_api.League`` for the guild's
    configured league. Finished seasons already in the database are skipped
    unless ``full`` is True. Returns a list of per-season summaries.
    """
    guild_id = str(guild_id)
    handler = current_league.yhandler
    sc = current_league.sc
    league_key = current_league.league_id
    summaries = []

    from yahoo_fantasy_api import League  # local import keeps tests light

    league = current_league
    for _ in range(MAX_SEASONS):
        settings = league.settings()
        season = int(settings["season"])
        prev_key = parsers.renew_to_league_key(settings.get("renew"))

        if _season_is_locked(guild_id, season):
            # This season and every one before it came from the history
            # files: leave them exactly as they are.
            logger.info("Season %s is from the history files, stopping",
                        season)
            summaries.append({"season": season, "skipped": True,
                              "from_files": True})
            break
        if not full and _season_is_complete_in_db(guild_id, season):
            logger.info("Season %s already synced, skipping", season)
            summaries.append({"season": season, "skipped": True})
        else:
            if progress:
                progress(season)
            summaries.append(
                _sync_season(guild_id, league_key, league, handler, settings)
            )

        if not prev_key:
            break
        league_key = prev_key
        league = League(sc, league_key, handler=handler)
    return summaries


def trade_rows(raw_trades, team_lookup):
    """Yahoo trade transactions -> rows in the league_trades.csv shape,
    oldest first. ``team_lookup(team_key, team_name)`` returns
    (manager_name, team_name)."""
    rows = []
    for t in raw_trades or []:
        tr = parsers.parse_trade(t)
        if tr is None:
            continue
        status = {"successful": "accepted", "vetoed": "vetoed"}.get(
            tr["status"])
        if status is None:
            continue
        (k1, n1), (k2, n2) = tr["teams"]
        if not tr["gets"][k1] or not tr["gets"][k2]:
            continue
        m1, team1 = team_lookup(k1, n1)
        m2, team2 = team_lookup(k2, n2)
        when = datetime.datetime.fromtimestamp(tr["timestamp"])
        rows.append({
            "ts": tr["timestamp"],
            "date": "{} {}, {}:{:02d} {}".format(
                when.strftime("%b"), when.day, (when.hour % 12) or 12,
                when.minute, "am" if when.hour < 12 else "pm"),
            "status": status,
            "manager1": m1, "team1": team1,
            "manager1_gets": " + ".join(tr["gets"][k1]),
            "manager2": m2, "team2": team2,
            "manager2_gets": " + ".join(tr["gets"][k2]),
        })
    rows.sort(key=lambda r: r["ts"])
    return rows


def _player_names(league, player_ids):
    """{player_id: full name}, looked up 25 at a time."""
    names = {}
    ids = [str(p) for p in player_ids]
    for i in range(0, len(ids), 25):
        try:
            details = league.player_details([int(p) for p in ids[i:i + 25]])
        except Exception:
            logger.exception("Could not look up drafted players")
            continue
        for d in details or []:
            name = d.get("name")
            if isinstance(name, dict):
                name = name.get("full")
            if d.get("player_id") is not None and name:
                names[str(d["player_id"])] = name
    return names


def _sync_season(guild_id, league_key, league, handler, settings):
    season = int(settings["season"])
    is_finished = _truthy(settings.get("is_finished"))
    logger.info("Syncing season %s (%s)", season, league_key)
    who = ManagerResolver(guild_id)

    standings = parsers.parse_standings(handler.get_standings_raw(league_key))
    for r in standings:
        r["manager_guid"], r["manager_name"] = who.resolve(
            r["manager_guid"], r["manager_name"], r["team_name"])
    team_key_to_guid = {r["team_key"]: r["manager_guid"] for r in standings}
    by_team_key = {r["team_key"]: r for r in standings}

    draft_order, draft = {}, []
    if settings.get("draft_status") == "postdraft":
        try:
            draft = league.draft_results() or []
            draft_order = parsers.first_round_order(draft, team_key_to_guid)
        except Exception:
            logger.exception("Could not read draft results for %s", season)

    start_week = int(settings.get("start_week") or 1)
    if is_finished:
        last_week = int(settings.get("end_week") or 0)
    else:
        last_week = int(settings.get("current_week") or start_week) - 1

    weekly, results = [], []
    if settings.get("draft_status") == "postdraft":
        for week in range(start_week, last_week + 1):
            try:
                raw = handler.get_scoreboard_raw(league_key, week=week)
                weekly.extend(parsers.parse_scoreboard(raw))
                results.extend(parsers.parse_matchup_results(raw))
            except Exception:
                logger.exception("Week %s of %s failed", week, season)
    for w in weekly:
        team = by_team_key.get(w["team_key"])
        if team:
            w["manager_guid"] = team["manager_guid"]
            w["manager_name"] = team["manager_name"]
        else:
            w["manager_guid"], w["manager_name"] = who.resolve(
                w["manager_guid"], w["manager_name"], w["team_name"])

    picks = []
    if draft:
        num_teams = max(len(standings), 1)
        names = _player_names(league, {p.get("player_id") for p in draft
                                       if p.get("player_id")})
        for p in draft:
            team = by_team_key.get(p.get("team_key"))
            name = names.get(str(p.get("player_id")))
            if not team or not name:
                continue
            rnd = int(p.get("round", 0))
            overall = int(p.get("pick", 0))
            picks.append(dict(
                round=rnd, pick=overall - (rnd - 1) * num_teams,
                manager_guid=team["manager_guid"],
                manager_name=team["manager_name"],
                team_name=team["team_name"], player=name))

    num_playoff = settings.get("num_playoff_teams")
    num_playoff = int(num_playoff) if num_playoff not in (None, "") else None

    champion_guid = None
    consolation_guid = None
    if is_finished:
        by_rank = {r["final_rank"]: r["manager_guid"] for r in standings}
        champion_guid = by_rank.get(1)
        # Yahoo ranks the consolation-bracket winner right after the
        # playoff teams (e.g. 7th with 6 playoff teams). Admins can
        # correct this with /history set-consolation.
        if num_playoff and _truthy(
            settings.get("has_playoff_consolation_games")
        ):
            consolation_guid = by_rank.get(num_playoff + 1)

    with database.atomic():
        ManagerSeason.delete().where(
            (ManagerSeason.guild_id == guild_id)
            & (ManagerSeason.season == season)
        ).execute()
        for r in standings:
            ManagerSeason.create(
                guild_id=guild_id,
                season=season,
                team_key=r["team_key"],
                team_id=r["team_id"],
                team_name=r["team_name"],
                manager_guid=r["manager_guid"],
                manager_name=r["manager_name"],
                final_rank=r["final_rank"] if is_finished else None,
                wins=r["wins"],
                losses=r["losses"],
                ties=r["ties"],
                draft_position=draft_order.get(r["manager_guid"]),
            )

        WeeklyTeamStat.delete().where(
            (WeeklyTeamStat.guild_id == guild_id)
            & (WeeklyTeamStat.season == season)
        ).execute()
        if weekly:
            rows = [dict(guild_id=guild_id, season=season, **w) for w in weekly]
            for i in range(0, len(rows), 200):
                WeeklyTeamStat.insert_many(rows[i:i + 200]).execute()

        if who.active:
            # Head-to-head results and draft picks, in the same shape as the
            # history files, so rivalries, streaks, sweeps and /drafts carry
            # on into synced seasons.
            WeeklyMatchup.delete().where(
                (WeeklyMatchup.guild_id == guild_id)
                & (WeeklyMatchup.season == season)).execute()
            for m in results:
                (k1, n1), (k2, n2) = m["teams"]
                t1, t2 = by_team_key.get(k1), by_team_key.get(k2)
                if not t1 or not t2:
                    continue
                WeeklyMatchup.create(
                    guild_id=guild_id, season=season, week=m["week"],
                    manager1_guid=t1["manager_guid"],
                    manager1_name=t1["manager_name"], team1=n1,
                    score1=m["wins"][k1],
                    manager2_guid=t2["manager_guid"],
                    manager2_name=t2["manager_name"], team2=n2,
                    score2=m["wins"][k2], is_playoffs=m["is_playoffs"])
            try:
                raw_trades = league.transactions("trade", "")
            except Exception:
                logger.exception("Could not read trades for %s", season)
                raw_trades = None
            if raw_trades is not None:
                from harambot.history.trades_import import save_trades

                def lookup(key, name):
                    team = by_team_key.get(key)
                    if team:
                        return team["manager_name"], team["team_name"]
                    return who.resolve(None, None, name)[1], name
                save_trades(guild_id, {season: trade_rows(raw_trades,
                                                          lookup)})
            if picks:
                DraftPick.delete().where(
                    (DraftPick.guild_id == guild_id)
                    & (DraftPick.season == season)).execute()
                for p in picks:
                    DraftPick.create(guild_id=guild_id, season=season, **p)
            who.save()

        s, _ = Season.get_or_create(
            guild_id=guild_id,
            season=season,
            defaults={"league_key": league_key},
        )
        s.league_key = league_key
        s.league_name = settings.get("name")
        s.is_finished = is_finished
        s.num_playoff_teams = num_playoff
        if not s.champion_overridden:
            s.champion_guid = champion_guid
        if not s.consolation_overridden:
            s.consolation_guid = consolation_guid
        s.synced_at = datetime.datetime.utcnow()
        s.save()

    return {
        "season": season,
        "teams": len(standings),
        "weeks": len({w["week"] for w in weekly}),
        "finished": is_finished,
        "has_draft": bool(draft_order),
        "unmatched": list(dict.fromkeys(who.unmatched)),
    }


def _team_lookup(guild_id, season, league, who):
    """team_key -> (manager_guid, manager_name, team_name) for the league's
    current teams, using the synced season if there is one."""
    teams = {r.team_key: (r.manager_guid, r.manager_name, r.team_name)
             for r in ManagerSeason.select().where(
                 (ManagerSeason.guild_id == str(guild_id))
                 & (ManagerSeason.season == season))}
    try:
        live = league.teams()
    except Exception:
        logger.exception("Could not read teams")
        live = {}
    for key, t in (live or {}).items():
        if key in teams:
            continue
        mgr = {}
        for m in t.get("managers") or []:
            mgr = m.get("manager", m) if isinstance(m, dict) else {}
            break
        guid, name = who.resolve(mgr.get("guid"), mgr.get("nickname"),
                                 t.get("name", ""))
        teams[key] = (guid, name, t.get("name", ""))
    return teams


def refresh_current_trades(guild_id, league):
    """Save this season's trades from Yahoo (used by /trade). Seasons from
    the history files are never touched. Returns the season or None."""
    season = int(league.settings()["season"])
    if _season_is_locked(guild_id, season):
        return None
    who = ManagerResolver(guild_id)
    teams = _team_lookup(guild_id, season, league, who)

    def lookup(key, name):
        t = teams.get(key)
        if t:
            return t[1], t[2]
        return who.resolve(None, None, name)[1], name
    from harambot.history.trades_import import save_trades
    rows = trade_rows(league.transactions("trade", ""), lookup)
    save_trades(guild_id, {season: rows})
    who.save()
    return season
