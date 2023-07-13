from discord.ext import commands
from asyncio import Lock
from discord_tron_master.classes.openai.text import GPT
from discord_tron_master.classes.app_config import AppConfig
import logging, traceback
from discord_tron_master.bot import DiscordBot
from discord_tron_master.classes.jobs.image_generation_job import ImageGenerationJob
from discord_tron_master.bot import clean_traceback

# For queue manager, etc.
discord = DiscordBot.get_instance()

class Reactions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = AppConfig()

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        if user.bot:  # Ignore bot reactions
            return
        # Code to execute when a reaction is added
        # await reaction.message.channel.send(f'{user.name} has reacted with {reaction.emoji}!')
        logging.debug(f'{user.name} has reacted with {reaction.emoji}!')
        no_op = [ '👎', '👍', '©️' ] # WE do nothing with these right now.
        if reaction.emoji in no_op:
            logging.debug(f'Ignoring no-op reaction: {reaction.emoji}')
            return
        # Now, we need to check if this is a reaction to a message we sent.
        logging.debug(f'Reaction: {reaction} on message content: {reaction.message.content}')
        if reaction.message.author != self.bot.user:
            logging.debug(f'Ignoring reaction on message not from me.')
            return
        for embed in reaction.message.embeds:
            logging.debug(f'Embed: {embed}, url: {embed.image.url}')

    @commands.Cog.listener()
    async def on_reaction_remove(self, reaction, user):
        if user.bot:  # Ignore bot reactions
            return
        # Code to execute when a reaction is removed
        # await reaction.message.channel.send(f'{user.name} has removed their reaction of {reaction.emoji}!')
        logging.debug(f'{user.name} has removed their reaction of {reaction.emoji}!')
