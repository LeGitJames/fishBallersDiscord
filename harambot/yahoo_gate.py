"""Switch Yahoo-powered commands off while Yahoo API access is pending.

Set ``YAHOO_ENABLED=false`` in .env and every command that needs the Yahoo
Fantasy API replies with a short "waiting on Yahoo" message instead of
failing. Everything else (trophy case, draft lottery, imports) still works.
Remove the line, or set it to true, once Yahoo approves access.
"""

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
    "trade",
    "league",
    "reports",
    "history sync",
}

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
