## Just an example for now. We're following the bot.

import os
import discord, logging
from discord.ext import commands
from discord_tron_master.websocket_hub import WebSocketHub
from discord_tron_master.classes.queue_manager import QueueManager
from discord_tron_master.classes.worker_manager import WorkerManager
from discord_tron_master.classes.custom_help import CustomHelp
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.bot import DiscordBot
config = AppConfig()

class DiscordBot:
    discord_instance = None
    def __init__(self, token):
        self.token = token
        self.websocket_hub = None
        self.queue_manager = None
        self.worker_manager = None
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.presences = True
        websocket_logger = logging.getLogger('discord')
        websocket_logger.setLevel(logging.WARNING) 
        self.bot = commands.Bot(command_prefix=config.get_command_prefix(), intents=intents)
        DiscordBot.discord_instance = self

    @classmethod
    def get_instance(cls):
        return cls.discord_instance

    async def set_websocket_hub(self, websocket_hub: WebSocketHub):
        self.websocket_hub = websocket_hub

    async def set_worker_manager(self, worker_manager: WorkerManager):
        self.worker_manager = worker_manager

    async def set_queue_manager(self, queue_manager: QueueManager):
        self.queue_manager = queue_manager

    async def on_ready(self):
        logging.info("Bot is ready!")
        await self.websocket_hub.run()

    async def run(self):
        await self.load_cogs()
        self.bot.event(self.on_ready)
        await self.bot.start(self.token)


    async def load_cogs(self, cogs_path="discord_tron_master/cogs"):
        import logging
        logging.debug("Loading cogs! Path: " + cogs_path)
        try:
            for root, _, files in os.walk(cogs_path):
                logging.debug("Found cogs: " + str(files))
                for file in files:
                    if file.endswith(".py"):
                        cog_path = os.path.join(root, file).replace("/", ".").replace("\\", ".")[:-3]
                        try:
                            import importlib
                            cog_module = importlib.import_module(cog_path)
                            cog_class_name = getattr(cog_module, file[:-3].capitalize())
                            await self.bot.add_cog(cog_class_name(self.bot))
                            logging.debug(f"Loaded cog: {cog_path}")
                        except Exception as e:
                            logging.error(f"Failed to load cog: {cog_path}")
                            logging.error(e)
        except Exception as e:
            logging.error(f"Ran into error: {e}")
            raise e

    async def find_channel(self, channel_id):
        for guild in self.bot.guilds:
            for channel in guild.channels:
                if isinstance(channel, discord.TextChannel):
                    if channel.id == channel_id:
                        return channel
        thread = self.bot.get_channel(channel_id)
        if thread is not None:
            return thread

        return None

    async def create_mention(self, user_id):
        for guild in self.bot.guilds:
            for member in guild.members:
                if member.id == user_id:
                    return member.mention
        return None

    async def send_private_message(self, user_id, message):
        for guild in self.bot.guilds:
            for member in guild.members:
                if member.id == user_id:
                    try:
                        await member.send(message)
                    except discord.Forbidden:
                        logging.info(f"Bot doesn't have permission to send messages to {member.name} ({member.id})")
                    except Exception as e:
                        logging.info(f"Error sending message to {member.name} ({member.id}): {e}")

    async def search_message_history(self, channel_id, search_term):
        channel = await self.find_channel(channel_id)
        if channel is not None:
            try:
                async for message in channel.history(limit=100):
                    if search_term in message.content:
                        return message
            except discord.Forbidden:
                logging.info(f"Bot doesn't have permission to search message history in {channel.name} ({channel.id})")
            except Exception as e:
                logging.info(f"Error searching message history in {channel.name} ({channel.id}): {e}")
        return None

    async def send_random_emojis(self, channel_id, count):
        channel = await self.find_channel(channel_id)
        if channel is not None:
            emojis = await self.get_random_emojis(count)
            # Now that we have the emojis, send them to the channel as a string:
            emoji_string = ""
            for emoji in emojis:
                emoji_string += str(emoji)
            try:
                await channel.send(emoji_string)
            except discord.Forbidden:
                logging.info(f"Bot doesn't have permission to send messages to {channel.name} ({channel.id})")
            except Exception as e:
                logging.info(f"Error sending message to {channel.name} ({channel.id}): {e}")
                

    async def get_random_emojis(self, count):
        emojis = []
        for guild in self.bot.guilds:
            for emoji in guild.emojis:
                emojis.append(emoji)
        import random
        return random.sample(emojis, count)

    async def get_messages_after_timestamp(self, channel_id, start_time):
        # end_time is now:
        import datetime
        end_time = datetime.datetime.now()

        channel = await self.find_channel(channel_id)
        if channel is not None:
            try:
                async for message in channel.history(limit=100, after=start_time, before=end_time):
                    return message
            except discord.Forbidden:
                logging.info(f"Bot doesn't have permission to search message history in {channel.name} ({channel.id})")
            except Exception as e:
                logging.info(f"Error searching message history in {channel.name} ({channel.id}): {e}")
        return None

    async def find_message_by_reaction(self, channel_id, emoji):
        channel = await self.find_channel(channel_id)
        if channel is not None:
            try:
                async for message in channel.history(limit=100):
                    for reaction in message.reactions:
                        if reaction.emoji == emoji:
                            return message
            except discord.Forbidden:
                logging.info(f"Bot doesn't have permission to search message history in {channel.name} ({channel.id})")
            except Exception as e:
                logging.info(f"Error searching message history in {channel.name} ({channel.id}): {e}")
        return None

    async def set_thread_topic(self, channel_id, topic):
        channel = await self.find_channel(channel_id)
        if channel is not None:
            try:
                await channel.edit(topic=topic)
            except discord.Forbidden:
                logging.info(f"Bot doesn't have permission to edit topic in {channel.name} ({channel.id})")
            except Exception as e:
                logging.info(f"Error editing topic in {channel.name} ({channel.id}): {e}")

    async def delete_previous_errors(self, channel_id, exclude_id = None, prefix="seems we had an error"):
        # Find any previous messages containing "seems we had an error" from this bot, and delete all except the most recent.
        channel = await self.find_channel(channel_id)
        if channel is not None:
            try:
                async for message in channel.history(limit=100):
                    if prefix in message.content and (exclude_id is None or message.id != exclude_id):
                        await message.delete()
            except discord.Forbidden:
                logging.info(f"Bot doesn't have permission to delete messages in {channel.name} ({channel.id})")
            except Exception as e:
                logging.info(f"Error deleting messages in {channel.name} ({channel.id}): {e}")

    @staticmethod
    async def fix_onmessage_context(ctx):
        context = ctx
        if not hasattr(ctx, "send"):
            # Likely this came from on_message. Get the context properly.
            context = await DiscordBot.get_instance().get_context(ctx)
        return context

    @staticmethod
    async def send_large_message(ctx, text, max_chars=2000, delete_delay=None):
        ctx = DiscordBot.fix_onmessage_context(ctx)
        if len(text) <= max_chars:
            if hasattr(ctx, "channel"):
                response = await ctx.channel.send(text)
            elif hasattr(ctx, "send"):
                response = await ctx.send(text)
            if delete_delay is not None:
                await response.delete(delay=delete_delay)
            return

        lines = text.split("\n")
        buffer = ""
        for line in lines:
            if len(buffer) + len(line) + 1 > max_chars:
                if hasattr(ctx, "channel"):
                    response = await ctx.channel.send(buffer)
                elif hasattr(ctx, "send"):
                    response = await ctx.send(buffer)

                if delete_delay is not None:
                    await response.delete(delay=delete_delay)
                buffer = ""
            buffer += line + "\n"
        if buffer:
            if hasattr(ctx, "channel"):
                response = await ctx.channel.send(buffer)
            elif hasattr(ctx, "send"):
                response = await ctx.send(buffer)
            if delete_delay is not None:
                await response.delete(delay=delete_delay)

    # for guild in command_processor.discord.bot.guilds:
    #     for channel in guild.channels:
    #         if isinstance(channel, discord.TextChannel):
    #             try:
    #                 await channel.send(message)
    #             except discord.Forbidden:
    #                 logging.info(f"Bot doesn't have permission to send messages in {channel.name} ({channel.id})")
    #             except Exception as e:
    #                 logging.info(f"Error sending message to {channel.name} ({channel.id}): {e}")
    
async def clean_traceback(trace: str):
    lines = trace.split("\n")
    new_lines = []
    for line in lines:
        new_line = line.split("discord-tron-client/")[-1].strip()
        new_lines.append('File: "' + new_line)
    new_string = "\n".join(new_lines)
    return new_string