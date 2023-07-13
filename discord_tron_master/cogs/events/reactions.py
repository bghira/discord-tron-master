from discord.ext import commands
from asyncio import Lock
from discord_tron_master.classes.openai.text import GPT
from discord_tron_master.classes.app_config import AppConfig
import logging, traceback
from PIL import Image
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
        no_op = [ '👎', '👍' ] # WE do nothing with these right now.
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
            import os
            filename = os.path.basename(embed.image.url)
            img = Image.open(os.path.join(self.config.get_web_root(), filename))
            logging.debug(f'Image info: {img.info}')
            if img.info == {}:
                logging.debug(f'No info found, continuing')
                continue
            else:
                logging.debug(f'Found info, exiting loop.')
                break
        # We have our info.
        if reaction.emoji == "©️":
            # We want to clone the settings of this post.
            logging.debug(f'Would clone settings: user_config {img.info["user_config"]}, prompt {img.info["prompt"]}.')
            logging.debug(f'User id: {user.id}')
            # Set the config:
            self.config.set_user_config(user.id, img.info["user_config"])
            # Send a message back to the reaction thread/channel:
            await reaction.message.channel.send(f'Cloned settings from {user.mention}\'s post for {reaction.user.mention}.')
        
    @commands.Cog.listener()
    async def on_reaction_remove(self, reaction, user):
        if user.bot:  # Ignore bot reactions
            return
        # Code to execute when a reaction is removed
        # await reaction.message.channel.send(f'{user.name} has removed their reaction of {reaction.emoji}!')
        logging.debug(f'{user.name} has removed their reaction of {reaction.emoji}!')

