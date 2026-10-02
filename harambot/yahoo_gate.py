"""Switch Yahoo-powered commands off while Yahoo API access is pending.

Set ``YAHOO_ENABLED=false`` in .env and every command that needs the Yahoo
Fantasy API replies with a short "waiting on Yahoo" message instead of
failing. Everything else (trophy case, draft lottery, imports) still works.
Remove the line, or set it to true, once Yahoo approves access.
"""

import logging

import discord
from discord import app_commands

from harambot.config import settings

# Commands that call the Yahoo Fantasy API (or show data only Yahoo has).
YAHOO_COMMANDS = {
    "standings",
    "roster",
    "matchups",
    "waivers",
    "stats",
    "draftfeed start",
    "draftfeed stop",
    "league",
    "reports",
    "history sync",
}

logger = logging.getLogger("discord.harambot.errors")

ERROR_MESSAGE = (
    "⚠️ Something went wrong running that command. It's been logged; try "
    "again in a bit."
)

WAITING_MESSAGE = (
    "⏳ This one needs the Yahoo Fantasy API, and we're still waiting on "
    "Yahoo to approve our access. It'll work as soon as they do!"
)


def yahoo_enabled():
    value = settings.get("YAHOO_ENABLED", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off")
    return bool(value)


class FishBallersTree(app_commands.CommandTree):
    """Command tree that turns away Yahoo commands while access is off."""

    async def interaction_check(self, interaction: discord.Interaction):
        if yahoo_enabled() or interaction.command is None:
            return True
        if interaction.command.qualified_name not in YAHOO_COMMANDS:
            return True
        if interaction.type == discord.InteractionType.application_command:
            await interaction.response.send_message(
                WAITING_MESSAGE, ephemeral=True
            )
        # Autocomplete requests are just dropped quietly.
        return False

    async def on_error(self, interaction: discord.Interaction, error):
        """Any command that crashes still gets a reply instead of Discord's
        "The application did not respond"."""
        if isinstance(error, app_commands.CheckFailure):
            return  # already answered (Yahoo gate, permission checks)
        cog = getattr(interaction.command, "binding", None)
        if cog is not None and "cog_app_command_error" in type(cog).__dict__:
            return  # that cog replies to its own errors
        name = interaction.command.qualified_name if interaction.command \
            else "?"
        logger.error("Command /%s failed", name, exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(ERROR_MESSAGE, ephemeral=True)
            else:
                await interaction.response.send_message(ERROR_MESSAGE,
                                                        ephemeral=True)
        except discord.HTTPException:
            pass
