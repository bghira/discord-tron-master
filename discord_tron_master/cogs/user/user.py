from discord.ext import commands
from discord_tron_master.models.transformers import Transformers

class User(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

