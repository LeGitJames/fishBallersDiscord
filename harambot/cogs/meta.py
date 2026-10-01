from discord.ext import commands
from discord import app_commands

import discord
import logging

from harambot.ui.views import ConfigView, LeagueConfigView, LeagueSelect, ReportConfigView
from harambot.database.models import Guild
from harambot.yahoo_gate import YAHOO_COMMANDS, yahoo_enabled


logger = logging.getLogger("discord.harambot.cogs.meta")
logger.setLevel(logging.INFO)


HELP_SECTIONS = [
    ("🏆 League history", [
        ("/trophycase", "Every past champion"),
        ("/alltime", "Career records and titles for every manager"),
        ("/profile manager", "A manager's career at a glance"),
        ("/rivalry manager opponent", "All-time head-to-head"),
        ("/nemesis manager", "Who you can't beat, and who you always beat"),
        ("/season season", "Recap of a past season"),
        ("/records [category]", "All-time best category weeks and seasons"),
        ("/streaks", "Longest win/loss and category hot streaks"),
        ("/hallofshame", "Winless seasons, chokers, bridesmaids, sweeps"),
        ("/trades [season] [manager] [partner] [player]",
         "Every past trade (and the vetoed ones)"),
        ("/votetrade [trade]", "Who won the trade? Put it to a vote"),
    ]),
    ("🎱 Drafts", [
        ("/draftlottery odds | run | results | breakdown",
         "Weighted draft lottery, and how the last one played out"),
        ("/drafts [season] [rounds] [manager] [player]",
         "Look back at past drafts"),
        ("/favourites [manager]", "Most-drafted players and crushes"),
        ("/draftluck", "Does draft position matter?"),
        ("/draftfeed start | stop", "Post the live draft pick by pick"),
    ]),
    ("📊 This season", [
        ("/standings", "Current standings"),
        ("/matchups", "This week's matchups"),
        ("/roster team_name", "A team's roster"),
        ("/stats player_name", "A player's details"),
        ("/waivers days", "Recent waiver moves"),
    ]),
    ("🔧 Setup (admins)", [
        ("/history import", "Load league history from the CSV files"),
        ("/history link", "Link a manager to their Discord account"),
        ("/history sync", "Pull new seasons from Yahoo"),
        ("/configure", "Connect the bot to Yahoo"),
        ("/league", "Choose which Yahoo league to use"),
        ("/reports", "Automatic transaction reports"),
        ("/ping", "Check the bot is alive"),
    ]),
]


def _command_key(usage):
    words = usage.lstrip("/").split()
    two = " ".join(words[:2])
    return two if two in YAHOO_COMMANDS else (words[0] if words else "")


def build_help_embed(waiting_on_yahoo):
    """Grouped /help. Commands that need the Yahoo API are moved into their
    own section at the bottom while access is pending."""
    embed = discord.Embed(
        title="FishBallersBot",
        description="Fish Ballers fantasy basketball bot",
        color=0xEEE657,
    )
    waiting = []
    for title, commands_ in HELP_SECTIONS:
        lines = []
        for usage, desc in commands_:
            line = "`{}` {}".format(usage, desc)
            if waiting_on_yahoo and _command_key(usage) in YAHOO_COMMANDS:
                waiting.append(line)
            else:
                lines.append(line)
        if lines:
            embed.add_field(name=title, value="\n".join(lines),
                            inline=False)
    if waiting:
        embed.add_field(name="⏳ Waiting on Yahoo API approval",
                        value="\n".join(waiting), inline=False)
        embed.set_footer(text="⏳ These will work once Yahoo approves "
                         "our access.")
    return embed


class Meta(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="View available commands")
    async def help(self, interaction: discord.Interaction):
        embed = build_help_embed(waiting_on_yahoo=not yahoo_enabled())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="ping", description="Gives the latency of FishBallersBot"
    )
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(self.bot.latency)

    @app_commands.command(
        name="configure",
        description="Configure your guild for FishBallersBot",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def configure(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            """
            Lets setup your guild
            1. Login into Yahoo and copy you authentication token
2. Configure FishBallersBot with your league information
            """,
            view=ConfigView(),
            ephemeral=True,
        )

    @configure.error
    async def configure_check_error(
        self, interaction: discord.Interaction, error
    ):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You do not have the required permissions to run this command."
            )

    def webhook_permissions(interaction: discord.Interaction):
        return interaction.guild.me.guild_permissions.manage_webhooks

    def guild_is_configured(interaction: discord.Interaction):
        return (
            Guild.select()
            .where(Guild.guild_id == str(interaction.guild_id))
            .exists()
        )

    @app_commands.command(
        name="reports",
        description="Configure automatic transaction and matchup reporting",
    )
    @app_commands.check(webhook_permissions)
    @app_commands.check(guild_is_configured)
    async def reports(
        self,
        interaction: discord.Interaction,
    ):
        message = "Set what channel transaction reports should be sent to."
        await interaction.response.send_message(
            message, view=ReportConfigView(), ephemeral=True
        )

    @reports.error
    async def reports_check_error(
        self, interaction: discord.Interaction, error
    ):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "Grant FishBallersBot the Manage Webhooks permission to use this command"
            )
        elif isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                "This guild is not configured for FishBallersBot. Please run `/configure` first."
            )

    @app_commands.command(
            name="league",
            description="Set which league FishBallersBot should use for commands"
            )
    @app_commands.check(guild_is_configured)
    async def league(self, interaction: discord.Interaction):
        await interaction.response.defer()
        message = "Select which league you would like to use for commands"
        view = LeagueConfigView()
        view.add_item(LeagueSelect(interaction.guild_id))
        await interaction.followup.send(message, view=view, ephemeral=True)

