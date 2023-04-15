from discord.ext import commands
from discord_tron_master.models.conversation import Conversations
from discord_tron_master.classes.text_replies import return_random as random_fact
from discord_tron_master.classes.app_config import AppConfig
import logging

config = AppConfig()
app = AppConfig.flask

class User(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="clear", help="Clear your GPT conversation history and start again.")
    async def clear_history(self, ctx):
        user_id = ctx.author.id
        try:
            with app.app_context():
                Conversations.clear_history_by_owner(owner=user_id)
                await ctx.send(
                    f"{ctx.author.mention} Well, well, well. It is like I don't even know you anymore. Did you know {random_fact()}?"
                )
        except Exception as e:
            logging.error("Caught error when clearing user conversation history: " + str(e))
            await ctx.send(
                f"{ctx.author.mention} The smoothbrain geriatric that writes my codebase did not correctly implement that method. I am sorry. Trying again will only lead to tears."
            )
