## Just an example for now. We're following the bot.

import os
import discord
from discord.ext import commands
from discord_tron_master.websocket_hub import WebSocketHub

class DiscordBot:
    def __init__(self, token):
        self.token = token
        self.websocket_hub = None
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.presences = True
        self.bot = commands.Bot(command_prefix="!", intents=intents)

    async def set_websocket_hub(self, websocket_hub):
        self.websocket_hub = websocket_hub

    async def on_ready(self):
        print("Bot is ready!")
        await self.websocket_hub.run()

    async def run(self):
        await self.load_cogs()
        self.bot.event(self.on_ready)
        await self.bot.start(self.token)


    async def load_cogs(self, cogs_path="discord_tron_master/cogs"):
        for root, _, files in os.walk(cogs_path):
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
