"""League history commands: trophy case, all-time records, career stats,
and the weighted draft lottery."""

import asyncio
import json
import logging
import os
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from harambot.database.history_models import (
    RecordBookEntry,
    DiscordLink,
    DraftLottery,
    ManagerSeason,
    Season,
    WeeklyTeamStat,
)
from harambot.database.models import Guild
from harambot.history import lottery
from harambot.history.queries import (
    NO_HISTORY,
    has_history,
    latest_names,
    season_label,
    build_lottery_entrants,
    discord_links,
    hall_of_shame,
    draft_board,
    drafted_players,
    loyal_pairs,
    matchup_streaks,
    most_drafted,
    name_marker,
)
from harambot.history.parsers import LOWER_IS_BETTER, NBA_STATS
from harambot.history.manual_import import parse_history_csv, save_history
from harambot.history.records_import import (
    looks_like_records,
    parse_records_csv,
    save_records,
)
from harambot.history.drafts_import import (
    looks_like_drafts,
    parse_drafts_csv,
    save_drafts,
)
from harambot.history.matchups_import import (
    looks_like_matchups,
    parse_matchups_csv,
    save_matchups,
)
from harambot.history import stats
from harambot.history.sync import sync_league_history
from harambot.yahoo_api import Yahoo

logger = logging.getLogger("discord.harambot.cogs.history")

COLOR = 0xEEE657
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
STAT_CHOICES = [
    app_commands.Choice(name=name, value=stat_id)
    for stat_id, name in NBA_STATS.items()
]
LOCAL_HISTORY_FILE = "league_history.csv"
LOCAL_MATCHUPS_FILE = "league_matchups.csv"
LOCAL_DRAFTS_FILE = "league_drafts.csv"
LOCAL_RECORDS_FILE = "league_records.csv"

# Extra files /history import understands, besides league_history.csv:
# (label, local file name, detector, parser, saver)
EXTRA_FILES = [
    ("Weekly matchups", LOCAL_MATCHUPS_FILE, looks_like_matchups,
     parse_matchups_csv, save_matchups),
    ("Draft picks", LOCAL_DRAFTS_FILE, looks_like_drafts,
     parse_drafts_csv, save_drafts),
    ("Category records", LOCAL_RECORDS_FILE, looks_like_records,
     parse_records_csv, save_records),
]


def _decode(data):
    # Excel on Windows may save CSVs as UTF-8 (with or without a BOM) or
    # as Windows-1252.
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def guild_is_configured(interaction: discord.Interaction):
    return (
        Guild.select()
        .where(Guild.guild_id == str(interaction.guild_id))
        .exists()
    )


def _ordinal(n):
    if n is None:
        return "—"
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(
        n % 10, "th")
    return "{}{}".format(n, suffix)


def _rec(r):
    return "{}-{}-{}".format(r["w"], r["l"], r["t"])


def _fmt(stat_id, value):
    if stat_id in ("5", "8"):
        return "{:.3f}".format(value).lstrip("0")
    return "{:g}".format(value)


class HistoryCog(commands.Cog):
    history = app_commands.Group(
        name="history",
        description="Manage saved league history (admins)",
        default_permissions=discord.Permissions(administrator=True),
    )
    draftlottery = app_commands.Group(
        name="draftlottery", description="Weighted draft-order lottery"
    )

    def __init__(self, bot):
        self.bot = bot
        # Separate instance so a long sync never races the other cogs.
        self.yahoo = Yahoo()
        # Guilds with a lottery reveal in progress
        self._lottery_running = set()

    # ------------------------------------------------------------------
    # Autocomplete
    # ------------------------------------------------------------------
    async def manager_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        names = latest_names(interaction.guild_id)
        choices = [
            app_commands.Choice(
                name="{} ({})".format(mgr, team)[:100], value=guid
            )
            for guid, (mgr, team) in names.items()
            if current.lower() in (mgr + " " + team).lower()
        ]
        return choices[:25]

    async def season_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[int]]:
        seasons = (
            Season.select(Season.season)
            .where(Season.guild_id == str(interaction.guild_id))
            .order_by(Season.season.desc())
        )
        return [
            app_commands.Choice(name=season_label(s.season), value=s.season)
            for s in seasons
            if current in str(s.season)
        ][:25]

    # ------------------------------------------------------------------
    # /trophycase
    # ------------------------------------------------------------------
    @app_commands.command(
        name="trophycase", description="Every past champion of the league"
    )
    async def trophycase(self, interaction: discord.Interaction):
        if not has_history(interaction.guild_id):
            await interaction.response.send_message(NO_HISTORY)
            return
        names = latest_names(interaction.guild_id)
        tag = name_marker(interaction.guild_id)
        seasons = (
            Season.select()
            .where(
                (Season.guild_id == str(interaction.guild_id))
                & (Season.is_finished == True)  # noqa: E712
            )
            .order_by(Season.season.desc())
        )
        lines, title_counts = [], {}
        for s in seasons:
            champ = self._team_in_season(s, s.champion_guid)
            if champ is None:
                continue
            title_counts[s.champion_guid] = (
                title_counts.get(s.champion_guid, 0) + 1
            )
            line = "🏆{} **{}{}** — {} (*{}*)".format(
                s.badge or "",
                season_label(s.season),
                "\\*" if s.note else "",
                tag(champ.manager_guid, champ.manager_name),
                champ.team_name,
            )
            if s.note:
                line += "\n  ↳ \\* {}".format(s.note)
            lines.append(line)

        embed = discord.Embed(
            title="🏆 Trophy Case",
            description="\n".join(lines) or "No finished seasons yet.",
            color=COLOR,
        )
        if title_counts:
            leaders = sorted(
                title_counts.items(), key=lambda kv: kv[1], reverse=True
            )
            embed.add_field(
                name="Most titles",
                value="\n".join(
                    "{} — {}".format(tag(g, names.get(g, ("?",))[0]), n)
                    for g, n in leaders[:5]
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed)

    @staticmethod
    def _team_in_season(season, guid):
        if not guid:
            return None
        return ManagerSeason.get_or_none(
            (ManagerSeason.guild_id == season.guild_id)
            & (ManagerSeason.season == season.season)
            & (ManagerSeason.manager_guid == guid)
        )

    # ------------------------------------------------------------------
    # /records
    # ------------------------------------------------------------------
    @app_commands.command(
        name="records",
        description="All-time single-week category records",
    )
    @app_commands.describe(
        category="Show the top 10 for one category",
        worst="Show the worst weeks instead of the best",
        include_playoffs="Count playoff weeks too (default: yes)",
    )
    @app_commands.choices(category=STAT_CHOICES)
    async def records(
        self,
        interaction: discord.Interaction,
        category: Optional[app_commands.Choice[str]] = None,
        worst: bool = False,
        include_playoffs: bool = True,
    ):
        if not has_history(interaction.guild_id):
            await interaction.response.send_message(NO_HISTORY)
            return
        guild_id = str(interaction.guild_id)
        tag = name_marker(guild_id)

        has_weekly = WeeklyTeamStat.select().where(
            WeeklyTeamStat.guild_id == guild_id).exists()
        if not has_weekly:
            await self._record_book(interaction, tag, category, worst)
            return

        def query(stat_id, limit):
            ascending = (stat_id in LOWER_IS_BETTER) != worst
            order = (
                WeeklyTeamStat.value.asc()
                if ascending
                else WeeklyTeamStat.value.desc()
            )
            q = WeeklyTeamStat.select().where(
                (WeeklyTeamStat.guild_id == guild_id)
                & (WeeklyTeamStat.stat_id == stat_id)
            )
            if not include_playoffs:
                q = q.where(WeeklyTeamStat.is_playoffs == False)  # noqa: E712
            return list(q.order_by(order, WeeklyTeamStat.season).limit(limit))

        label = "Worst" if worst else "Best"
        if category is None:
            embed = discord.Embed(
                title="📈 All-Time {} Weeks".format(label),
                description="Single-week records in every category",
                color=COLOR,
            )
            for stat_id, name in NBA_STATS.items():
                top = query(stat_id, 1)
                if not top:
                    continue
                r = top[0]
                embed.add_field(
                    name=name,
                    value="**{}** — {}\n{} wk {}".format(
                        _fmt(stat_id, r.value),
                        tag(r.manager_guid, r.manager_name),
                        season_label(r.season), r.week,
                    ),
                    inline=True,
                )
        else:
            stat_id = category.value
            rows = query(stat_id, 10)
            lines = [
                "{} **{}** — {} (*{}*), {} wk {}{}".format(
                    MEDALS.get(i, "`{:>2}.`".format(i)),
                    _fmt(stat_id, r.value),
                    tag(r.manager_guid, r.manager_name),
                    r.team_name,
                    season_label(r.season),
                    r.week,
                    " (playoffs)" if r.is_playoffs else "",
                )
                for i, r in enumerate(rows, start=1)
            ]
            embed = discord.Embed(
                title="📈 {} Weekly {} Ever".format(label, category.name),
                description="\n".join(lines) or "No data yet.",
                color=COLOR,
            )
        embed.set_footer(
            text="Some weeks are longer than others (e.g. All-Star break)."
        )
        await interaction.response.send_message(embed=embed)

    async def _record_book(self, interaction, tag, category, worst):
        """/records from Yahoo's Record Book (all-time #1s only), used
        until weekly stats can be synced from the Yahoo API."""
        entries = list(RecordBookEntry.select().where(
            RecordBookEntry.guild_id == str(interaction.guild_id)))
        if not entries:
            await interaction.response.send_message(
                "No category records saved yet. An admin needs to add "
                "league_records.csv and run `/history import`."
            )
            return
        # "best" for TO means fewest; "worst" means most
        kind = "worst" if worst else "best"

        def line(stat_id, scope):
            hits = [e for e in entries if e.stat_id == stat_id
                    and e.scope == scope and e.kind == kind]
            if not hits:
                return None
            when = ", ".join(
                "{} (*{}*) {}{}".format(
                    tag(e.manager_guid, e.manager_name), e.team_name,
                    season_label(e.season),
                    " wk {}".format(e.week) if e.week else "",
                )
                for e in hits
            )
            return "**{}** — {}".format(_fmt(stat_id, hits[0].value), when)

        stats = [category.value] if category else list(NBA_STATS)
        embed = discord.Embed(
            title="📈 All-Time {} {}".format(
                "Worst" if worst else "Best",
                category.name if category else "Records"),
            color=COLOR,
        )
        for stat_id in stats:
            parts = []
            wk = line(stat_id, "week")
            ssn = line(stat_id, "season")
            if wk:
                parts.append("Week: " + wk)
            if ssn:
                parts.append("Season: " + ssn)
            if parts:
                embed.add_field(name=NBA_STATS[stat_id],
                                value="\n".join(parts), inline=False)
        if not embed.fields:
            embed.description = (
                "Yahoo's record book only keeps the best in each "
                "category (plus most turnovers)."
            )
        embed.set_footer(
            text="From Yahoo's record book: only the #1 in each category "
            "until Yahoo approves API access. Some weeks are longer than "
            "others."
        )
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /streaks
    # ------------------------------------------------------------------
    @app_commands.command(
        name="streaks",
        description="Longest win and loss streaks, and category hot "
        "streaks",
    )
    async def streaks(self, interaction: discord.Interaction):
        if not has_history(interaction.guild_id):
            await interaction.response.send_message(NO_HISTORY)
            return
        guild_id = str(interaction.guild_id)
        tag = name_marker(guild_id)
        data = matchup_streaks(guild_id)
        embed = discord.Embed(title="📈 Streaks", color=COLOR)

        def streak_lines(rows):
            return "\n".join(
                "**{}** — {} (*{}*), {} wks {}-{}".format(
                    s["length"], tag(s["manager_guid"], s["manager"]),
                    s["team"], season_label(s["season"]), s["start"],
                    s["end"],
                )
                for s in rows
            )

        if data["W"]:
            embed.add_field(name="🔥 Longest winning streaks",
                            value=streak_lines(data["W"]), inline=False)
        if data["L"]:
            embed.add_field(name="🧊 Longest losing streaks",
                            value=streak_lines(data["L"]), inline=False)

        leads = list(RecordBookEntry.select().where(
            (RecordBookEntry.guild_id == guild_id)
            & (RecordBookEntry.scope == "lead_streak")))
        if leads:
            order = list(NBA_STATS)
            leads.sort(key=lambda e: order.index(e.stat_id)
                       if e.stat_id in order else 99)
            embed.add_field(
                name="👑 Most weeks in a row leading the league",
                value="\n".join(
                    "**{}**: {} wks — {} (*{}*), {} wks {}-{}".format(
                        NBA_STATS.get(e.stat_id, e.stat_id), int(e.value),
                        tag(e.manager_guid, e.manager_name), e.team_name,
                        season_label(e.season), e.week,
                        e.week + int(e.value) - 1,
                    )
                    for e in leads
                ),
                inline=False,
            )
        if not embed.fields:
            embed.description = (
                "No streaks yet. An admin needs to add league_matchups.csv "
                "and run `/history import`."
            )
        embed.set_footer(text="Streaks run within a season, playoffs "
                         "included · 🪦 = no longer in the league")
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /hallofshame
    # ------------------------------------------------------------------
    @app_commands.command(
        name="hallofshame",
        description="Winless seasons, 9-0 beatdowns and other lowlights",
    )
    async def hallofshame(self, interaction: discord.Interaction):
        if not has_history(interaction.guild_id):
            await interaction.response.send_message(NO_HISTORY)
            return
        data = hall_of_shame(interaction.guild_id)
        tag = name_marker(interaction.guild_id)
        embed = discord.Embed(title="🤡 Hall of Shame", color=0x8B0000)

        if data["winless"]:
            embed.add_field(
                name="🥚 Winless seasons",
                value="\n".join(
                    "**{}** (*{}*) went **{}-{}-{}** in {}".format(
                        tag(r.manager_guid, r.manager_name), r.team_name, r.wins, r.losses,
                        r.ties, season_label(r.season),
                    )
                    for r in data["winless"]
                ),
                inline=False,
            )
        if data["worst"]:
            embed.add_field(
                name="📉 Worst records (with at least one win)",
                value="\n".join(
                    "**{}** (*{}*) {}-{}-{} ({:.3f}) in {}".format(
                        tag(r.manager_guid, r.manager_name), r.team_name, r.wins, r.losses,
                        r.ties, data["pct"](r), season_label(r.season),
                    )
                    for r in data["worst"]
                ),
                inline=False,
            )

        choke = stats.chokers(interaction.guild_id)
        if choke["no_title"]:
            embed.add_field(
                name="😰 Best records that didn't win it all",
                value="\n".join(
                    "**{}** (*{}*) {}-{}-{} in {}, finished {}".format(
                        tag(r.manager_guid, r.manager_name), r.team_name,
                        r.wins, r.losses, r.ties, season_label(r.season),
                        _ordinal(r.final_rank),
                    )
                    for r in choke["no_title"]
                ),
                inline=False,
            )
        maids = [b for b in choke["bridesmaids"] if len(b["seasons"]) > 1]
        if maids:
            embed.add_field(
                name="💍 Always the bridesmaid (most runner-up finishes)",
                value="\n".join(
                    "**{}**: {} ({})".format(
                        tag(b["guid"], b["name"]), len(b["seasons"]),
                        ", ".join(season_label(s) for s in b["seasons"]))
                    for b in maids
                ),
                inline=False,
            )

        sweeps = data["sweeps"]
        if sweeps:
            counts = {}
            for s in sweeps:
                loser = tag(s[6], s[2])
                counts[loser] = counts.get(loser, 0) + 1
            leaders = sorted(counts.items(), key=lambda kv: -kv[1])
            embed.add_field(
                name="🧹 Most times swept 9-0",
                value="\n".join(
                    "**{}**: {}".format(name, n) for name, n in leaders[:5]
                ),
                inline=False,
            )
            lines = [
                "{} wk {}: **{}** (*{}*) got swept by {} (*{}*)".format(
                    season_label(season), week, tag(lguid, loser), lteam,
                    tag(wguid, winner), wteam,
                )
                for (season, week, loser, lteam, winner, wteam, lguid,
                     wguid) in sweeps
            ]
            text = ""
            for i, line in enumerate(lines):
                if len(text) + len(line) > 950:
                    text += "…and {} more".format(len(lines) - i)
                    break
                text += line + "\n"
            embed.add_field(
                name="🧹 Every 9-0 sweep ({})".format(len(sweeps)),
                value=text, inline=False,
            )
        elif not data["winless"]:
            embed.description = "Nothing shameful on record… yet."
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /favourites
    # ------------------------------------------------------------------
    @app_commands.command(
        name="favourites",
        description="The players a manager (or the whole league) keeps "
        "drafting",
    )
    @app_commands.describe(
        manager="League manager. Leave empty for the whole league."
    )
    @app_commands.autocomplete(manager=manager_autocomplete)
    async def favourites(
        self,
        interaction: discord.Interaction,
        manager: Optional[str] = None,
    ):
        names = latest_names(interaction.guild_id)
        tag = name_marker(interaction.guild_id)
        if manager and manager not in names:
            await interaction.response.send_message(
                "Pick a manager from the list.", ephemeral=True
            )
            return
        if manager:
            rows = most_drafted(interaction.guild_id, manager, limit=5)
            title = "❤️ {}'s favourites".format(names[manager][0])
            footer = "Most drafted players, all seasons"
        else:
            rows = loyal_pairs(interaction.guild_id)
            title = "❤️ The league's biggest crushes"
            footer = "Same manager, same player, most seasons"
        if not rows:
            await interaction.response.send_message(
                "No draft history saved yet."
            )
            return
        lines = []
        for i, r in enumerate(rows, start=1):
            seasons = ", ".join(season_label(s)[2:] for s in r["seasons"])
            who = "" if manager else "**{}** ❤️ ".format(
                tag(r["manager_guid"], r["manager"]))
            lines.append(
                "{} {}**{}**: drafted {}x ({}) · earliest: round {}".format(
                    MEDALS.get(i, "`{}.`".format(i)), who, r["player"],
                    r["times"], seasons, r["best_round"],
                )
            )
        embed = discord.Embed(
            title=title, description="\n".join(lines)[:4000], color=COLOR
        )
        embed.set_footer(text=footer)
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /drafts
    # ------------------------------------------------------------------
    async def player_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=p[:100], value=p[:100])
            for p in drafted_players(interaction.guild_id, current)
        ]

    @app_commands.command(
        name="drafts", description="Look back at past drafts"
    )
    @app_commands.describe(
        season="Which draft",
        rounds="Show the first X rounds (default: 1 for a whole draft, "
        "all for one manager's draft, 3 across every season)",
        manager="Show this manager's picks",
        player="Every time this player was drafted",
    )
    @app_commands.autocomplete(
        season=season_autocomplete,
        manager=manager_autocomplete,
        player=player_autocomplete,
    )
    async def drafts(
        self,
        interaction: discord.Interaction,
        season: Optional[int] = None,
        rounds: Optional[app_commands.Range[int, 1, 30]] = None,
        manager: Optional[str] = None,
        player: Optional[str] = None,
    ):
        names = latest_names(interaction.guild_id)
        tag = name_marker(interaction.guild_id)
        if manager and manager not in names:
            await interaction.response.send_message(
                "Pick a manager from the list.", ephemeral=True
            )
            return
        if season is None and not player and not manager:
            await interaction.response.send_message(
                "Pick a season (and optionally rounds or a manager), or a "
                "player.", ephemeral=True
            )
            return
        # Sensible defaults so results fit in one message
        if rounds is None and not player:
            if season is not None and not manager:
                rounds = 1
            elif manager and season is None:
                rounds = 3

        picks = draft_board(interaction.guild_id, season, rounds, manager,
                            player)
        if not picks:
            await interaction.response.send_message(
                "No draft picks found for that."
            )
            return

        def slot(p):
            return "`{}.{:02d}`".format(p.round, p.pick)

        def who(p):
            return "{} (*{}*)".format(
                tag(p.manager_guid, p.manager_name), p.team_name)

        def rounds_label():
            if not rounds:
                return ""
            return " · round 1" if rounds == 1 else \
                " · rounds 1-{}".format(rounds)

        lines = []
        if player:
            title = "📜 {} draft history".format(player)
            lines = ["{} {} {}".format(season_label(p.season), slot(p),
                                       who(p)) for p in picks]
            footer = "Drafted {} time{}{}".format(
                len(picks), "" if len(picks) == 1 else "s",
                " in the first {} rounds".format(rounds) if rounds else "")
        elif manager and season is None:
            title = "📜 {}'s picks{}".format(names[manager][0],
                                            rounds_label())
            lines = ["{} {} **{}**".format(season_label(p.season), slot(p),
                                           p.player) for p in picks]
            footer = "Every season · add a season to see one whole draft"
        elif manager:
            title = "📜 {}'s {} draft{}".format(
                names[manager][0], season_label(season), rounds_label())
            lines = ["*{}*".format(picks[0].team_name)] + [
                "{} **{}**".format(slot(p), p.player) for p in picks]
            footer = "Round.pick"
        else:
            title = "📜 {} draft{}".format(season_label(season),
                                          rounds_label())
            current = None
            for p in picks:
                if rounds and rounds > 1 and p.round != current:
                    current = p.round
                    lines.append("**Round {}**".format(p.round))
                lines.append("{} **{}**: {}".format(slot(p), p.player,
                                                     who(p)))
            footer = "Round.pick · 🪦 = no longer in the league"

        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:3970].rsplit("\n", 1)[0] + \
                "\n… too long for one message, try fewer rounds"
        embed = discord.Embed(title=title, description=text, color=COLOR)
        embed.set_footer(text=footer)
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /rivalry, /nemesis, /profile, /season, /draftluck
    # ------------------------------------------------------------------
    @app_commands.command(
        name="rivalry", description="All-time head-to-head between two "
        "managers"
    )
    @app_commands.autocomplete(manager=manager_autocomplete,
                               opponent=manager_autocomplete)
    async def rivalry(self, interaction: discord.Interaction, manager: str,
                      opponent: str):
        names = latest_names(interaction.guild_id)
        if manager not in names or opponent not in names:
            await interaction.response.send_message(
                "Pick both managers from the list.", ephemeral=True)
            return
        if manager == opponent:
            await interaction.response.send_message(
                "Pick two different managers.", ephemeral=True)
            return
        tag = name_marker(interaction.guild_id)
        a = tag(manager, names[manager][0])
        b = tag(opponent, names[opponent][0])
        h = stats.head_to_head(interaction.guild_id, manager, opponent)
        if h is None:
            await interaction.response.send_message(
                "{} and {} have never played each other.".format(a, b))
            return
        o = h["overall"]
        leader = a if o["w"] > o["l"] else b if o["l"] > o["w"] else None
        embed = discord.Embed(
            title="⚔️ {} vs {}".format(a, b),
            description="**{}** all-time ({} games){}".format(
                _rec(o), len(h["games"]),
                " · {} leads".format(leader) if leader else " · dead even"),
            color=COLOR,
        )
        embed.add_field(name="Regular season", value=_rec(h["regular"]))
        kind, n = h["streak"]
        if kind:
            embed.add_field(
                name="Current streak",
                value="{} has won {} straight".format(
                    a if kind == "W" else b, n))
        if h["playoff_meetings"]:
            embed.add_field(
                name="🏆 Playoff meetings",
                value="\n".join(
                    "{} {}: {}".format(
                        season_label(g["season"]), g["round"],
                        "tied {:g}-{:g}".format(g["mine"], g["theirs"])
                        if g["result"] == "T" else
                        "{} won {:g}-{:g}".format(
                            a if g["result"] == "W" else b,
                            max(g["mine"], g["theirs"]),
                            min(g["mine"], g["theirs"])))
                    for g in h["playoff_meetings"]),
                inline=False)
        for label, g, winner in (("💥 {}'s biggest win".format(a),
                                  h["biggest_win"], a),
                                 ("💥 {}'s biggest win".format(b),
                                  h["biggest_loss"], b)):
            if g:
                embed.add_field(
                    name=label,
                    value="{:g}-{:g}, {} wk {}".format(
                        max(g["mine"], g["theirs"]),
                        min(g["mine"], g["theirs"]),
                        season_label(g["season"]), g["week"]))
        embed.add_field(
            name="Last {} meetings".format(len(h["last"])),
            value=" ".join("✅" if g["result"] == "W" else "❌"
                           if g["result"] == "L" else "➖"
                           for g in h["last"]) + "  (from {}'s side)".format(a),
            inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="nemesis",
        description="Who a manager can't beat, and who they always beat",
    )
    @app_commands.autocomplete(manager=manager_autocomplete)
    async def nemesis(self, interaction: discord.Interaction, manager: str):
        names = latest_names(interaction.guild_id)
        if manager not in names:
            await interaction.response.send_message(
                "Pick a manager from the list.", ephemeral=True)
            return
        tag = name_marker(interaction.guild_id)
        o = stats.opponents(interaction.guild_id, manager)
        if not o["rows"]:
            await interaction.response.send_message(
                "{} hasn't played anyone {}+ times yet.".format(
                    names[manager][0], o["min_games"]))
            return
        me = tag(manager, names[manager][0])
        embed = discord.Embed(title="😈 {}'s nemesis".format(me),
                              color=COLOR)
        n, pb = o["nemesis"], o["punching_bag"]
        embed.description = (
            "😈 **Nemesis:** {} ({} vs them)\n"
            "🥊 **Punching bag:** {} ({} vs them)").format(
            tag(n["opp"], n["name"]), _rec(n),
            tag(pb["opp"], pb["name"]), _rec(pb))
        embed.add_field(
            name="Record vs everyone (best first)",
            value="\n".join(
                "{} — {} ({:.3f})".format(tag(r["opp"], r["name"]), _rec(r),
                                          r["pct"])
                for r in o["rows"])[:1024],
            inline=False)
        embed.set_footer(text="Opponents with {}+ games{}".format(
            o["min_games"],
            " · {} others left out".format(o["hidden"]) if o["hidden"]
            else ""))
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="profile",
                          description="A manager's career at a glance")
    @app_commands.autocomplete(manager=manager_autocomplete)
    async def profile(self, interaction: discord.Interaction, manager: str):
        names = latest_names(interaction.guild_id)
        if manager not in names:
            await interaction.response.send_message(
                "Pick a manager from the list.", ephemeral=True)
            return
        tag = name_marker(interaction.guild_id)
        p = stats.profile(interaction.guild_id, manager)
        if p is None:
            await interaction.response.send_message("No history yet.")
            return
        links = discord_links(interaction.guild_id)
        embed = discord.Embed(
            title="🪪 {}".format(tag(manager, p["name"])),
            description="*{}* · {} season{} ({}–{}){}".format(
                p["team"], p["seasons"], "" if p["seasons"] == 1 else "s",
                season_label(p["first_season"]),
                season_label(p["last_season"]),
                " · <@{}>".format(links[manager]) if manager in links
                else ""),
            color=COLOR,
        )
        embed.add_field(
            name="🏆 Titles",
            value="{} {}".format(
                "🏆" * len(p["titles"]) or "None yet",
                "({})".format(", ".join(season_label(s)
                                        for s in p["titles"]))
                if p["titles"] else ""),
            inline=False)
        embed.add_field(name="🥈 Runner-up", value=str(len(p["finals"])))
        embed.add_field(name="🎟️ Playoffs", value=str(p["playoffs"]))
        if p["best"]:
            embed.add_field(
                name="📈 Best / 📉 worst finish",
                value="{} ({}) / {} ({})".format(
                    _ordinal(p["best"].final_rank),
                    season_label(p["best"].season),
                    _ordinal(p["worst"].final_rank),
                    season_label(p["worst"].season)))
        if p["record"]:
            embed.add_field(
                name="Regular season",
                value="{} ({:.3f})".format(_rec(p["record"]),
                                           p["record"]["pct"]))
            pr = p["playoff_record"]
            if pr["w"] + pr["l"] + pr["t"]:
                embed.add_field(name="Playoffs & consolation",
                                value=_rec(pr))
        embed.add_field(
            name="🧹 9-0 sweeps",
            value="{} given · {} taken".format(p["sweeps_given"],
                                              p["sweeps_taken"]))
        o = p["opponents"]
        if o["nemesis"]:
            embed.add_field(
                name="😈 Nemesis / 🥊 punching bag",
                value="{} ({}) / {} ({})".format(
                    tag(o["nemesis"]["opp"], o["nemesis"]["name"]),
                    _rec(o["nemesis"]),
                    tag(o["punching_bag"]["opp"], o["punching_bag"]["name"]),
                    _rec(o["punching_bag"])),
                inline=False)
        if p["favourite"]:
            embed.add_field(
                name="❤️ Favourite player",
                value="{} (drafted {}x)".format(*p["favourite"]))
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="season",
                          description="Recap of a past season")
    @app_commands.autocomplete(season=season_autocomplete)
    async def season_cmd(self, interaction: discord.Interaction,
                         season: int):
        r = stats.season_recap(interaction.guild_id, season)
        if r is None:
            await interaction.response.send_message(
                "No saved season {}.".format(season_label(season)))
            return
        tag = name_marker(interaction.guild_id)
        s = r["season"]
        lines = []
        for m in r["standings"]:
            medal = MEDALS.get(m.final_rank, "`{:>2}.`".format(
                m.final_rank or "-"))
            rec = " {}-{}-{}".format(m.wins, m.losses, m.ties) \
                if (m.wins + m.losses + m.ties) else ""
            lines.append("{} **{}** (*{}*){}".format(
                medal, tag(m.manager_guid, m.manager_name), m.team_name,
                rec))
        embed = discord.Embed(
            title="📅 {} season{}{}".format(
                season_label(season), " " + s.badge if s.badge else "",
                "*" if s.note else ""),
            description="\n".join(lines)[:4000], color=COLOR)
        if s.note:
            embed.add_field(name="* Note", value=s.note, inline=False)
        st = r["streaks"]
        if st["W"]:
            embed.add_field(
                name="🔥 Longest win streak",
                value="{} — {} (wks {}-{})".format(
                    st["W"]["length"], tag(st["W"]["guid"], st["W"]["name"]),
                    *st["W"]["range"]))
        if st["L"]:
            embed.add_field(
                name="🧊 Longest losing streak",
                value="{} — {} (wks {}-{})".format(
                    st["L"]["length"], tag(st["L"]["guid"], st["L"]["name"]),
                    *st["L"]["range"]))
        if r["sweeps"]:
            embed.add_field(
                name="🧹 9-0 sweeps",
                value="\n".join("wk {}: {} swept {}".format(
                    g["week"], tag(g["me"], g["me_name"]),
                    tag(g["opp"], g["opp_name"])) for g in r["sweeps"]),
                inline=False)
        if r["first_pick"]:
            fp = r["first_pick"]
            embed.add_field(
                name="🎯 #1 pick",
                value="{}{} → finished {}".format(
                    tag(fp.manager_guid, fp.manager_name),
                    " took **{}**".format(r["first_pick_player"])
                    if r["first_pick_player"] else "",
                    _ordinal(fp.final_rank)))
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="draftluck",
        description="Does draft position matter? Picks vs final finish")
    async def draftluck(self, interaction: discord.Interaction):
        d = stats.draft_luck(interaction.guild_id)
        if not d["champions"]:
            await interaction.response.send_message(
                "Not enough draft history saved yet.")
            return
        tag = name_marker(interaction.guild_id)
        embed = discord.Embed(title="🎲 Does the draft matter?",
                              color=COLOR)
        if d["luckiest"]:
            embed.add_field(
                name="🍀 Luckiest draft slots (lowest average pick)",
                value="\n".join(
                    "{} — avg pick {:.1f} over {} drafts ({} top-3)".format(
                        tag(x["guid"], x["name"]), x["avg"], x["seasons"],
                        x["top3"]) for x in d["luckiest"]),
                inline=False)
            embed.add_field(
                name="🌧️ Unluckiest draft slots (highest average pick)",
                value="\n".join(
                    "{} — avg pick {:.1f} over {} drafts ({} top-3)".format(
                        tag(x["guid"], x["name"]), x["avg"], x["seasons"],
                        x["top3"]) for x in d["unluckiest"]),
                inline=False)
        embed.add_field(
            name="🏆 Where each champion picked",
            value="\n".join("{}: {} (pick {})".format(
                season_label(r.season), tag(r.manager_guid, r.manager_name),
                r.draft_position) for r in d["champions"]),
            inline=False)
        embed.add_field(
            name="🎯 How the #1 pick finished",
            value="\n".join("{}: {} → {}".format(
                season_label(r.season), tag(r.manager_guid, r.manager_name),
                _ordinal(r.final_rank)) for r in d["first_picks"]),
            inline=False)
        embed.add_field(
            name="📊 Average finish by draft slot",
            value=" · ".join("#{}: {:.1f}".format(k, v)
                             for k, v in d["avg_by_slot"].items()),
            inline=False)
        embed.add_field(
            name="🚀 Biggest glow-ups",
            value="\n".join("{} (*{}*) {}: pick {} → {}".format(
                tag(r.manager_guid, r.manager_name), r.team_name,
                season_label(r.season), r.draft_position,
                _ordinal(r.final_rank)) for r in d["glow_ups"]),
            inline=False)
        embed.add_field(
            name="💀 Biggest flops",
            value="\n".join("{} (*{}*) {}: pick {} → {}".format(
                tag(r.manager_guid, r.manager_name), r.team_name,
                season_label(r.season), r.draft_position,
                _ordinal(r.final_rank)) for r in d["flops"]),
            inline=False)
        embed.set_footer(text="Pick = first-round draft position · luck "
                         "needs 3+ drafts")
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /alltime
    # ------------------------------------------------------------------
    @app_commands.command(
        name="alltime",
        description="Career records for every manager in league history",
    )
    async def alltime(self, interaction: discord.Interaction):
        if not has_history(interaction.guild_id):
            await interaction.response.send_message(NO_HISTORY)
            return
        guild_id = str(interaction.guild_id)
        names = latest_names(guild_id)
        tag = name_marker(guild_id)
        titles = {}
        for s in Season.select().where(
            (Season.guild_id == guild_id) & Season.champion_guid.is_null(False)
        ):
            titles[s.champion_guid] = titles.get(s.champion_guid, 0) + 1

        # Seasons imported with only a champion have no W-L-T, so they
        # count toward titles and best finish but not the record.
        careers = {}
        for r in ManagerSeason.select().where(
            ManagerSeason.guild_id == guild_id
        ):
            c = careers.setdefault(
                r.manager_guid, {"w": 0, "l": 0, "t": 0, "seasons": 0,
                                 "best": None}
            )
            if r.wins or r.losses or r.ties:
                c["w"] += r.wins
                c["l"] += r.losses
                c["t"] += r.ties
                c["seasons"] += 1
            if r.final_rank and (c["best"] is None
                                 or r.final_rank < c["best"]):
                c["best"] = r.final_rank
        stats = []
        for guid, c in careers.items():
            played = c["w"] + c["l"] + c["t"]
            c["pct"] = (c["w"] + 0.5 * c["t"]) / played if played else 0
            stats.append((guid, c))
        stats.sort(
            key=lambda x: (titles.get(x[0], 0), x[1]["pct"]), reverse=True
        )

        lines = []
        for guid, c in stats[:25]:
            mgr = tag(guid, names.get(guid, ("Unknown",))[0])
            trophies = "🏆" * titles.get(guid, 0)
            if c["seasons"]:
                record = "{}-{}-{} ({:.3f}) over {} season{}".format(
                    c["w"], c["l"], c["t"], c["pct"], c["seasons"],
                    "" if c["seasons"] == 1 else "s",
                )
            else:
                record = "no win-loss records saved"
            lines.append(
                "**{}** {}\n  ↳ {} · best finish: {}".format(
                    mgr, trophies, record, c["best"] or "—"
                )
            )
        embed = discord.Embed(
            title="📚 All-Time Manager Records",
            description="\n".join(lines)[:4000],
            color=COLOR,
        )
        embed.set_footer(
            text="Wins-losses-ties across all seasons · 🪦 = no longer in "
            "the league"
        )
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /draftlottery
    # ------------------------------------------------------------------
    @draftlottery.command(
        name="odds", description="Balls and #1-pick odds for each team"
    )
    async def lottery_odds(self, interaction: discord.Interaction):
        entrants, previous, problems = build_lottery_entrants(
            interaction.guild_id
        )
        if not entrants:
            await interaction.response.send_message(
                "\n".join(problems) or NO_HISTORY
            )
            return
        lines = [
            "`{:>2} balls` {:>5.1%} — **{}** ({}) · picked #{} in {}{}".format(
                e["balls"], p, e["manager_name"], e["team_name"],
                e["previous_pick"], season_label(previous.season),
                " · 🏅 +1 consolation" if e["consolation_winner"] else "",
            )
            for e, p in lottery.first_pick_odds(entrants)
        ]
        embed = discord.Embed(
            title="🎱 Draft Lottery Odds",
            description="\n".join(lines),
            color=COLOR,
        )
        if problems:
            embed.add_field(
                name="⚠️ Missing", value="\n".join(problems)[:1024],
                inline=False,
            )
        embed.set_footer(
            text="Odds shown are for the #1 pick. Picks are drawn one at a "
            "time until every team is placed."
        )
        await interaction.response.send_message(embed=embed)

    @draftlottery.command(
        name="run",
        description="Run the lottery live, revealing picks from last to "
        "first (admins)",
    )
    @app_commands.describe(
        delay="Seconds between picks (default 8). The top 3 get extra "
        "drama.",
        practice="Rehearse it: same show, but nothing is saved",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def lottery_run(
        self,
        interaction: discord.Interaction,
        delay: app_commands.Range[int, 2, 30] = 8,
        practice: bool = False,
    ):
        guild_id = interaction.guild_id
        if guild_id in self._lottery_running:
            await interaction.response.send_message(
                "A lottery is already being revealed. Hang tight!",
                ephemeral=True,
            )
            return
        entrants, previous, problems = build_lottery_entrants(guild_id)
        if problems or not entrants:
            await interaction.response.send_message(
                "Can't run the lottery yet:\n"
                + "\n".join(problems or [NO_HISTORY])
                + "\nCheck `/draftlottery odds`.",
                ephemeral=True,
            )
            return

        # Draw and save before revealing anything, so a crash mid-show
        # can't lose or change the result.
        order = lottery.draw(entrants)
        draft_season = previous.season + 1
        if not practice:
            DraftLottery.create(
                guild_id=str(guild_id),
                season=draft_season,
                run_by=str(interaction.user.id),
                results=json.dumps(order),
            )

        self._lottery_running.add(guild_id)
        try:
            title = "🎱 **The {} draft lottery is starting!**".format(
                season_label(draft_season)
            )
            if practice:
                title = "🧪 **PRACTICE RUN, results won't count.** " + title
            await interaction.response.send_message(
                title + "\nRevealing picks from last to first. "
                "Get comfortable…"
            )
            # Send to the channel rather than as interaction follow-ups,
            # which expire after 15 minutes.
            channel = interaction.channel
            # Practice runs show the tags without pinging anyone.
            pings = discord.AllowedMentions(
                everyone=False, roles=False, users=not practice
            )
            for wait, message in lottery.reveal_script(order, delay):
                await asyncio.sleep(wait)
                await channel.send(message, allowed_mentions=pings)
            await asyncio.sleep(delay)
            embed = self._order_embed(draft_season, order, interaction.user)
            if practice:
                embed.title = "🧪 PRACTICE: " + embed.title
            await channel.send(embed=embed)
        finally:
            self._lottery_running.discard(guild_id)

    @draftlottery.command(
        name="results", description="Show the most recent lottery result"
    )
    async def lottery_results(self, interaction: discord.Interaction):
        last = (
            DraftLottery.select()
            .where(DraftLottery.guild_id == str(interaction.guild_id))
            .order_by(DraftLottery.run_at.desc())
            .first()
        )
        if last is None:
            await interaction.response.send_message(
                "No lottery has been run yet."
            )
            return
        user = self.bot.get_user(int(last.run_by))
        await interaction.response.send_message(
            embed=self._order_embed(
                last.season, json.loads(last.results), user,
                run_at=last.run_at,
            )
        )

    @staticmethod
    def _order_embed(season, order, user, run_at=None):
        embed = discord.Embed(
            title="📋 {} Draft Order".format(season_label(season)),
            description="\n".join(
                "`{:>2}.` **{}** ({}){}".format(
                    e["pick"], e["manager_name"], e["team_name"],
                    " <@{}>".format(e["discord_id"])
                    if e.get("discord_id") else "",
                )
                for e in order
            ),
            color=COLOR,
        )
        footer = "Run by {}".format(user.display_name if user else "an admin")
        if run_at:
            footer += " · {:%b %d, %Y}".format(run_at)
        embed.set_footer(text=footer)
        return embed

    # ------------------------------------------------------------------
    # /history (admin)
    # ------------------------------------------------------------------
    @history.command(
        name="sync",
        description="Import past seasons, results and weekly stats from "
        "Yahoo",
    )
    @app_commands.describe(
        full="Re-import seasons that were already saved (slower)"
    )
    @app_commands.check(guild_is_configured)
    async def history_sync(
        self, interaction: discord.Interaction, full: bool = False
    ):
        await interaction.response.defer(thinking=True)
        guild_id = interaction.guild_id
        league = await asyncio.to_thread(
            self.yahoo.get_current_league, guild_id=guild_id
        )
        if league is None:
            await interaction.followup.send(
                "Couldn't reach your Yahoo league. Try `/configure` again."
            )
            return
        try:
            summaries = await asyncio.to_thread(
                sync_league_history, guild_id, league, full
            )
        except Exception:
            logger.exception("History sync failed for %s", guild_id)
            await interaction.followup.send(
                "Sync failed partway through; anything already saved was "
                "kept. Check the bot logs and try again."
            )
            return

        lines = []
        for s in summaries:
            if s.get("skipped"):
                lines.append("`{}` already saved".format(
                    season_label(s["season"])))
            else:
                lines.append(
                    "`{}` {} teams · {} weeks{}{}".format(
                        season_label(s["season"]), s["teams"], s["weeks"],
                        "" if s["finished"] else " · in progress",
                        "" if s["has_draft"] else " · no draft yet",
                    )
                )
        embed = discord.Embed(
            title="✅ History synced",
            description="\n".join(lines),
            color=COLOR,
        )
        embed.add_field(
            name="Check this",
            value="Consolation winners are guessed from Yahoo's final "
            "ranks. Check `/draftlottery odds` and fix any with "
            "`/history set-consolation`.",
            inline=False,
        )
        await interaction.followup.send(embed=embed)

    @history.command(
        name="import",
        description="Load league history from a CSV file (no Yahoo needed)",
    )
    @app_commands.describe(
        file="CSV file to load. Leave empty to use league_history.csv in "
        "the bot's folder"
    )
    async def history_import(
        self,
        interaction: discord.Interaction,
        file: Optional[discord.Attachment] = None,
    ):
        await interaction.response.defer(thinking=True)
        extras = []  # (label, text, parser, saver)
        if file is not None:
            if file.size > 5_000_000:
                await interaction.followup.send("That file is too big.")
                return
            text = _decode(await file.read())
            for label, _, detect, parser, saver in EXTRA_FILES:
                if detect(text):
                    extras.append((label, text, parser, saver))
                    text = None
                    break
        else:
            path = os.path.join(os.getcwd(), LOCAL_HISTORY_FILE)
            if not os.path.exists(path):
                await interaction.followup.send(
                    "No file attached, and there's no {} in the bot's "
                    "folder.".format(LOCAL_HISTORY_FILE)
                )
                return
            with open(path, "rb") as f:
                text = _decode(f.read())
            for label, name, _, parser, saver in EXTRA_FILES:
                extra_path = os.path.join(os.getcwd(), name)
                if os.path.exists(extra_path):
                    with open(extra_path, "rb") as f:
                        extras.append((label, _decode(f.read()), parser,
                                       saver))

        # Check every extra file before saving any of them.
        parsed_extras = []
        for label, extra_text, parser, saver in extras:
            extra_seasons, extra_errors = parser(extra_text)
            if extra_errors:
                await interaction.followup.send(
                    "Nothing was saved. Fix these in the {} file:\n".format(
                        label.lower())
                    + "\n".join("• " + e for e in extra_errors[:15])
                )
                return
            parsed_extras.append((label, extra_seasons, saver))
        extra_notes = []
        for label, extra_seasons, saver in parsed_extras:
            count = await asyncio.to_thread(
                saver, interaction.guild_id, extra_seasons
            )
            extra_notes.append("{}: {} loaded".format(label, count))
        if text is None:
            await interaction.followup.send("✅ " + "\n".join(extra_notes))
            return

        seasons, errors = parse_history_csv(text)
        if errors:
            shown = "\n".join("• " + e for e in errors[:15])
            more = len(errors) - 15
            if more > 0:
                shown += "\n…and {} more".format(more)
            await interaction.followup.send(
                "Nothing was saved. Fix these and try again:\n" + shown
            )
            return
        if not seasons:
            await interaction.followup.send("The file has no seasons in it.")
            return

        saved, skipped = await asyncio.to_thread(
            save_history, interaction.guild_id, seasons
        )
        lines = []
        for season in saved:
            rows = seasons[season]
            champ = next(
                (r for r in rows if r["final_rank"] == 1), None
            )
            lines.append(
                "`{}` {} managers{}".format(
                    season_label(season),
                    len(rows),
                    " · 🏆 " + champ["manager_name"] if champ
                    else " · not played yet",
                )
            )
        for season in skipped:
            lines.append(
                "`{}` skipped: already synced from Yahoo".format(
                    season_label(season)
                )
            )
        embed = discord.Embed(
            title="✅ History imported",
            description="\n".join(lines),
            color=COLOR,
        )
        if extra_notes:
            embed.add_field(name="Also loaded", value="\n".join(extra_notes),
                            inline=False)
        embed.add_field(
            name="Next",
            value="Check `/trophycase` and `/draftlottery odds`.",
            inline=False,
        )
        await interaction.followup.send(embed=embed)

    @history.command(
        name="link",
        description="Link a league manager to their Discord account so the "
        "bot can tag them",
    )
    @app_commands.describe(
        manager="League manager", user="Their Discord account"
    )
    @app_commands.autocomplete(manager=manager_autocomplete)
    async def history_link(
        self,
        interaction: discord.Interaction,
        manager: str,
        user: discord.Member,
    ):
        names = latest_names(interaction.guild_id)
        if manager not in names:
            await interaction.response.send_message(
                "Pick a manager from the list.", ephemeral=True
            )
            return
        guild_id = str(interaction.guild_id)
        DiscordLink.delete().where(
            (DiscordLink.guild_id == guild_id)
            & (DiscordLink.manager_guid == manager)
        ).execute()
        DiscordLink.create(
            guild_id=guild_id,
            manager_guid=manager,
            discord_user_id=str(user.id),
        )
        mgr, team = names[manager]
        await interaction.response.send_message(
            "Linked **{}** ({}) to {}.".format(mgr, team, user.mention),
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=True,
        )

    @history.command(
        name="links", description="Show which managers are linked to Discord"
    )
    async def history_links(self, interaction: discord.Interaction):
        names = latest_names(interaction.guild_id)
        links = discord_links(interaction.guild_id)
        if not names:
            await interaction.response.send_message(NO_HISTORY,
                                                    ephemeral=True)
            return
        lines = [
            "{} **{}** ({}){}".format(
                "✅" if guid in links else "⬜", mgr, team,
                " → <@{}>".format(links[guid]) if guid in links else "",
            )
            for guid, (mgr, team) in sorted(
                names.items(), key=lambda kv: kv[1][0].lower()
            )
        ]
        await interaction.response.send_message(
            "\n".join(lines),
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=True,
        )

    @history.command(
        name="set-consolation",
        description="Set who won the consolation bracket in a season",
    )
    @app_commands.autocomplete(
        season=season_autocomplete, manager=manager_autocomplete
    )
    async def set_consolation(
        self, interaction: discord.Interaction, season: int, manager: str
    ):
        await self._set_winner(interaction, season, manager, "consolation")

    @history.command(
        name="set-champion",
        description="Override the champion for a season",
    )
    @app_commands.autocomplete(
        season=season_autocomplete, manager=manager_autocomplete
    )
    async def set_champion(
        self, interaction: discord.Interaction, season: int, manager: str
    ):
        await self._set_winner(interaction, season, manager, "champion")

    async def _set_winner(self, interaction, season, manager_guid, kind):
        s = Season.get_or_none(
            (Season.guild_id == str(interaction.guild_id))
            & (Season.season == season)
        )
        team = s and self._team_in_season(s, manager_guid)
        if team is None:
            await interaction.response.send_message(
                "That manager wasn't in the {} season.".format(
                    season_label(season)
                ),
                ephemeral=True,
            )
            return
        setattr(s, kind + "_guid", manager_guid)
        setattr(s, kind + "_overridden", True)
        s.save()
        await interaction.response.send_message(
            "Saved: {} {} winner is **{}** ({}).".format(
                season_label(season), kind, team.manager_name,
                team.team_name,
            )
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error
    ):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "Only server admins can do that."
        elif isinstance(error, app_commands.CheckFailure):
            msg = "Run `/configure` first to connect your Yahoo league."
        else:
            logger.exception("History command failed", exc_info=error)
            msg = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
