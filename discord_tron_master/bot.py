## Just an example for now. We're following the bot.

import os
import discord
from discord.ext import commands

class DiscordBot:
    def __init__(self, token):
        self.token = token
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.presences = True
        self.bot = commands.Bot(command_prefix="!", intents=intents)

    async def on_ready(self):
        print(f"{self.bot.user} is connected")

    def run(self):
        self.load_cogs()
        self.bot.run(self.token)

    def load_cogs(self, cogs_path="discord_tron_master/cogs"):
        for root, _, files in os.walk(cogs_path):
            for file in files:
                if file.endswith(".py"):
                    cog_path = os.path.join(root, file).replace("/", ".").replace("\\", ".")[:-3]
                    try:
                        cog_module = importlib.import_module(cog_path)
                        cog_class_name = getattr(cog_module, file[:-3].capitalize())
                        self.bot.add_cog(cog_class_name(self.bot))
                        print(f"Loaded cog: {cog_path}")
                    except Exception as e:
                        print(f"Failed to load cog: {cog_path}")
                        print(e)
