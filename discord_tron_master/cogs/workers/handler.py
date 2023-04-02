from discord.ext import commands
from asyncio import Lock
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.bot import DiscordBot

class Handler(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = AppConfig()
        self.discord = DiscordBot.get_instance()

    @commands.command(name="worker-stats", help="Shows worker stats.")
    async def workers(self, ctx):
        user_id = ctx.author.id
        user_config = self.config.get_user_config(user_id=user_id)
        next_worker_gpu = self.discord.worker_manager.find_first_worker(job_type="gpu")
        next_worker_compute = self.discord.worker_manager.find_first_worker(job_type="compute")
        next_worker_memory = self.discord.worker_manager.find_first_worker(job_type="memory")
        message = "Worker status:\n```"
        message = message + f"Next GPU worker: {next_worker_gpu}\n"
        message = message + f"Next Compute worker: {next_worker_compute}\n"
        message = message + f"Next Memory worker: {next_worker_memory}\n"

        await ctx.send(message)


def setup(bot):
    bot.add_cog(Handler(bot))