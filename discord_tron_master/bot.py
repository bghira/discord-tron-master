## Just an example for now. We're following the bot.

import os
import discord
from discord.ext import commands

class DiscordBot:
    def __init__(self, token):
        self.token = token
        self.bot = commands.Bot(command_prefix="!")

    async def on_ready(self):
        print(f"{self.bot.user} is connected")

    def run(self):
        self.bot.run(self.token)