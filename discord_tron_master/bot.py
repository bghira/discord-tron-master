## Just an example for now. We're following the bot.

import os
import discord
from discord.ext import commands
from discord_tron_master.websocket_hub import WebSocketHub
from discord_tron_master.classes.queue_manager import QueueManager
from discord_tron_master.classes.worker_manager import WorkerManager

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
        self.bot = commands.Bot(command_prefix="!", intents=intents)
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
        print("Bot is ready!")
        await self.websocket_hub.run()

    async def run(self):
        await self.load_cogs()
        self.bot.event(self.on_ready)
        await self.bot.start(self.token)


    async def load_cogs(self, cogs_path="discord_tron_master/cogs"):
        import logging
        logging.info("Loading cogs! Path: " + cogs_path)
        for root, _, files in os.walk(cogs_path):
            logging.info("Found cogs: " + str(files))
            for file in files:
                if file.endswith(".py"):
                    cog_path = os.path.join(root, file).replace("/", ".").replace("\\", ".")[:-3]
                    try:
                        import importlib
                        cog_module = importlib.import_module(cog_path)
                        cog_class_name = getattr(cog_module, file[:-3].capitalize())
                        await self.bot.add_cog(cog_class_name(self.bot))
                        print(f"Loaded cog: {cog_path}")
                    except Exception as e:
                        print(f"Failed to load cog: {cog_path}")
                        print(e)
