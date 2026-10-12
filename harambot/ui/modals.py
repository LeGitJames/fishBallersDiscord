import discord
import logging

from discord.utils import MISSING
from typing import Optional
from harambot import yahoo_api
from harambot.database.models import Guild
from harambot.utils import yahoo_auth

logger = logging.getLogger(__file__)
logger.setLevel(logging.INFO)


class ConfigModal(discord.ui.Modal, title="Configure Guild"):

    yahoo_token = discord.ui.TextInput(
        label="Yahoo Token",
        placeholder="Enter the token from the Yahoo login link",
    )

    league_id = discord.ui.TextInput(
        label="Yahoo League ID", placeholder="Enter Yahoo League ID"
    )
    league_type = discord.ui.TextInput(
        label="Yahoo League Type",
        placeholder="Enter Yahoo League Type(nfl, nhl, nba, mlb)",
        default="nba",
    )

    guild = None
    view = None

    def __init__(
        self,
        *,
        title: str = MISSING,
        timeout: Optional[float] = None,
        custom_id: str = MISSING,
        guild_id: str = None,
        view: discord.ui.View = None,
    ) -> None:
        super().__init__(title=title, timeout=timeout, custom_id=custom_id)
        self.view = view
        self.guild = Guild.get_or_none(Guild.guild_id == str(guild_id))
        if self.guild:
            # Already set up: the Yahoo code is optional, so you can change
            # the league without logging in again, or paste a fresh code to
            # renew the Yahoo login (e.g. after switching Yahoo apps).
            self.yahoo_token.required = False
            self.yahoo_token.label = "Yahoo code (optional)"
            self.yahoo_token.placeholder = (
                "Paste a new code to log in again, or leave empty")
            self.league_id.default = self.guild.league_id
            self.league_type.default = self.guild.league_type

    async def on_submit(self, interaction: discord.Interaction):
        details = {
            "league_id": self.league_id.value.strip(),
            "league_type": self.league_type.value.strip().lower(),
        }
        code = (self.yahoo_token.value or "").strip()
        if code:
            oauth_details = yahoo_auth(code)
            if not oauth_details:
                await interaction.response.send_message(
                    "Couldn't log in to Yahoo with that code. Codes only "
                    "work once and expire quickly; click **Login to Yahoo** "
                    "for a fresh one and try again.",
                    ephemeral=True,
                )
                return
            details.update(oauth_details)
        elif not self.guild:
            await interaction.response.send_message(
                "Paste the code from **Login to Yahoo** first.",
                ephemeral=True,
            )
            return
        # Keep only columns the Guild table has (Yahoo may send extras).
        details = {k: v for k, v in details.items()
                   if k in Guild._meta.fields}
        if self.guild:
            Guild.update(details).where(
                Guild.guild_id == self.guild.guild_id
            ).execute()
        else:
            # The database still has these columns; /rip was removed.
            details.update({"RIP_text": "", "RIP_image_url": ""})
            self.guild = Guild(guild_id=str(interaction.guild_id), **details)
            self.guild.save()
        # Forget anything cached from the old login or league.
        yahoo_api.cache.clear()
        await interaction.response.send_message(
            "Guild settings updated!{}".format(
                " Yahoo login renewed." if code else ""),
            ephemeral=True,
        )
        self.view.stop()

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        logger.exception(error)
        await interaction.response.send_message(
            "Oops! Something went wrong with configuring your guild. Please try again.",
            ephemeral=True,
        )
