"""League history commands: trophy case, all-time records, career stats,
and the weighted draft lottery."""

import asyncio
import json
import logging
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands
from peewee import fn

from harambot.database.history_models import (
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
)
from harambot.history.parsers import LOWER_IS_BETTER, NBA_STATS
from harambot.history.sync import sync_league_history
from harambot.yahoo_api import Yahoo

logger = logging.getLogger("discord.harambot.cogs.history")

COLOR = 0xEEE657
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
STAT_CHOICES = [
    app_commands.Choice(name=name, value=stat_id)
    for stat_id, name in NBA_STATS.items()
]
REVEAL_DELAY_SECONDS = 3


def guild_is_configured(interaction: discord.Interaction):
    return (
        Guild.select()
        .where(Guild.guild_id == str(interaction.guild_id))
        .exists()
    )


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
            line = "🏆 **{}** — {} (*{}*)".format(
                season_label(s.season), champ.manager_name, champ.team_name
            )
            cons = self._team_in_season(s, s.consolation_guid)
            if cons is not None:
                line += "\n  ↳ 🏅 Consolation: {} (*{}*)".format(
                    cons.manager_name, cons.team_name
                )
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
                    "{} — {}".format(names.get(g, ("?",))[0], n)
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
                        _fmt(stat_id, r.value), r.manager_name,
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
                    r.manager_name,
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
        titles = {}
        for s in Season.select().where(
            (Season.guild_id == guild_id) & Season.champion_guid.is_null(False)
        ):
            titles[s.champion_guid] = titles.get(s.champion_guid, 0) + 1

        rows = (
            ManagerSeason.select(
                ManagerSeason.manager_guid,
                fn.SUM(ManagerSeason.wins).alias("w"),
                fn.SUM(ManagerSeason.losses).alias("l"),
                fn.SUM(ManagerSeason.ties).alias("t"),
                fn.COUNT(ManagerSeason.id).alias("seasons"),
                fn.MIN(ManagerSeason.final_rank).alias("best"),
            )
            .where(ManagerSeason.guild_id == guild_id)
            .group_by(ManagerSeason.manager_guid)
        )
        stats = []
        for r in rows:
            played = (r.w or 0) + (r.l or 0) + (r.t or 0)
            pct = ((r.w or 0) + 0.5 * (r.t or 0)) / played if played else 0
            stats.append((r, pct))
        stats.sort(
            key=lambda x: (titles.get(x[0].manager_guid, 0), x[1]),
            reverse=True,
        )

        lines = []
        for r, pct in stats[:25]:
            mgr = names.get(r.manager_guid, ("Unknown",))[0]
            trophies = "🏆" * titles.get(r.manager_guid, 0)
            lines.append(
                "**{}** {}\n  ↳ {}-{}-{} ({:.3f}) · {} seasons · best "
                "finish: {}".format(
                    mgr, trophies, r.w, r.l, r.t, pct, r.seasons,
                    r.best or "—",
                )
            )
        embed = discord.Embed(
            title="📚 All-Time Manager Records",
            description="\n".join(lines)[:4000],
            color=COLOR,
        )
        embed.set_footer(text="Category wins-losses-ties, all seasons")
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
        description="Run the lottery live (admins). Results are saved.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def lottery_run(self, interaction: discord.Interaction):
        entrants, previous, problems = build_lottery_entrants(
            interaction.guild_id
        )
        if problems or not entrants:
            await interaction.response.send_message(
                "Can't run the lottery yet:\n"
                + "\n".join(problems or [NO_HISTORY])
                + "\nRun `/history sync` and check `/draftlottery odds`.",
                ephemeral=True,
            )
            return

        order = lottery.draw(entrants)
        draft_season = previous.season + 1
        DraftLottery.create(
            guild_id=str(interaction.guild_id),
            season=draft_season,
            run_by=str(interaction.user.id),
            results=json.dumps(order),
        )

        await interaction.response.send_message(
            "🎱 **The {} draft lottery is starting!** Revealing picks from "
            "last to first…".format(season_label(draft_season))
        )
        for e in reversed(order):
            await asyncio.sleep(REVEAL_DELAY_SECONDS)
            await interaction.followup.send(
                "{} Pick **#{}** — **{}** ({}) · had {} balls".format(
                    MEDALS.get(e["pick"], "🔹"), e["pick"],
                    e["manager_name"], e["team_name"], e["balls"],
                )
            )
        await interaction.followup.send(
            embed=self._order_embed(draft_season, order, interaction.user)
        )

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
                "`{:>2}.` **{}** ({})".format(
                    e["pick"], e["manager_name"], e["team_name"]
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
            "ranks. Look at `/trophycase` and fix any with "
            "`/history set-consolation`.",
            inline=False,
        )
        await interaction.followup.send(embed=embed)

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
