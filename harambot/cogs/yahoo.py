import discord
import logging
import json

from discord.ext import commands
from discord import app_commands
from typing import List, Optional
from datetime import datetime, timedelta

from harambot.yahoo_api import Yahoo
from harambot import utils

logging.setLoggerClass(logging.Logger)
logging.getLogger("yahoo_oauth").setLevel("INFO")

logger = logging.getLogger("discord.harambot.cogs.yahoo")

yahoo_api = Yahoo()
class YahooCog(commands.Cog):

    error_message = (
        "⚠️ Couldn't get that from Yahoo right now. Try again in a bit; if "
        "it keeps happening, an admin may need to run `/configure` again."
    )
    

    def __init__(self, bot):
        self.bot = bot
        

    @app_commands.command(
        name="standings",
        description="Returns the current standings of your league",
    )
    async def standings(self, interaction: discord.Interaction):
        logger.info("Command:Standings called in %i", interaction.guild_id)
        await interaction.response.defer()
        settings = yahoo_api.get_settings(guild_id=interaction.guild_id)
        if not settings:
            await interaction.followup.send(self.error_message)
            return
        scoring_type = settings.get("scoring_type")
        embed = discord.Embed(
            title="Standings",
            description="W-L-T" if scoring_type == "head" else "Team \nPoints For - Points Change",
            color=0xEEE657,
        )
        standings = yahoo_api.get_standings(guild_id=interaction.guild_id)
        if standings:
            for team in standings:
                embed.add_field(
                    name=team["place"],
                    value=team["record"],
                    inline=False,
                )
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(self.error_message)

    async def roster_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        teams = yahoo_api.get_teams(guild_id=interaction.guild_id)
        if teams:
            options = list(
                map(
                    lambda x: app_commands.Choice(
                        name=teams[x]["name"], value=teams[x]["name"]
                    ),
                    teams,
                )
            )
            return options
        return []

    @app_commands.command(
        name="roster", description="Returns the roster of the given team"
    )
    @app_commands.autocomplete(team_name=roster_autocomplete)
    async def roster(self, interaction: discord.Interaction, team_name: str):
        logger.info(
            "Command:Roster called in %i with team_name:%s",
            interaction.guild_id,
            team_name,
        )
        await interaction.response.defer()
        embed = discord.Embed(
            title="{}'s Roster".format(team_name),
            description="",
            color=0xEEE657,
        )
        settings = yahoo_api.get_settings(guild_id=interaction.guild_id)
        if not settings:
            await interaction.followup.send(self.error_message)
            return
        if settings.get("draft_status") == "predraft":
            await interaction.followup.send("Rosters not available yet")
            return
        roster = yahoo_api.get_roster(
            guild_id=interaction.guild_id, team_name=team_name
        )
        if roster:
            for player in roster:
                if len(roster) > 25 and player["selected_position"] in ["IR", "IL"]:
                    continue
                embed.add_field(
                    name=player["selected_position"],
                    value=player["name"],
                    inline=False,
                )
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(self.error_message)
    
    async def stats_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        players = yahoo_api.get_players(
            current, guild_id=interaction.guild_id
        )
        if players:
            options = list(
                map(
                    lambda x: app_commands.Choice(
                        name=x["name"]["full"],
                        value=x["name"]["full"],
                    ),
                    players,
                )
            )
        else:
            options = []
        return options

    @app_commands.command(
        name="stats", description="Returns the details of the given player"
    )
    @app_commands.autocomplete(player_name=stats_autocomplete)
    async def stats(
        self,
        interaction: discord.Interaction,
        player_name: str,
        week: Optional[int] = None,
    ):
        logger.info(
            "Command:Stats called in %i with player_name:%s",
            interaction.guild_id,
            player_name,
        )
        await interaction.response.defer()
        player = yahoo_api.get_player_details(
            player_name, guild_id=interaction.guild_id, week=week
        )
        if player:
            embed = utils.get_player_embed(player)
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send("Player not found")

    @app_commands.command(
        name="matchups", description="Returns the current weeks matchups"
    )
    async def matchups(
        self, interaction: discord.Interaction, week: Optional[int] = None
    ):
        logger.info(
            "Command:Matchups called in {} with week: {}".format(
                interaction.guild_id, week
            )
        )
        await interaction.response.defer()
        settings = yahoo_api.get_settings(guild_id=interaction.guild_id)
        if not settings:
            await interaction.followup.send(self.error_message)
            return
        if settings.get("draft_status") == "predraft":
            await interaction.followup.send("Matchups not available yet")
            return
        week, details = yahoo_api.get_matchups(
            guild_id=interaction.guild_id, week=week
        )
            
        if details:
            embed = discord.Embed(
                title="Matchups for Week {}".format(week),
                description="",
                color=0xEEE657,
            )
            for detail in details:
                embed.add_field(
                    name=detail["name"], value=detail["value"], inline=False
                )
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(self.error_message)

    @app_commands.command(
        name="waivers",
        description="Returns the waiver transactions from the last 24 hours",
    )
    async def waivers(self, interaction: discord.Interaction, days: int = 1):
        logger.info("Command:Waivers called in %i", interaction.guild_id)

        await interaction.response.defer()
        embed_functions_dict = {
            "add/drop": utils.create_add_drop_embed,
            "add": utils.create_add_embed,
            "drop": utils.create_drop_embed,
        }
        ts = datetime.now() - timedelta(days=days)
        transactions = yahoo_api.get_transactions(
            guild_id=interaction.guild_id, timestamp=ts.timestamp()
        )
        if transactions:
            for transaction in transactions:
                await interaction.followup.send(
                    embed=embed_functions_dict[transaction["type"]](
                        transaction
                    )
                )
        else:
            await interaction.followup.send("No transactions found")
