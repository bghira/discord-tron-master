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
        best_worker_gpu = self.discord.worker_manager.find_best_hardware_for_job(next_worker_gpu, job_type="gpu")

        next_worker_compute = self.discord.worker_manager.find_first_worker(job_type="compute")
        best_worker_compute = self.discord.worker_manager.find_best_hardware_for_job(next_worker_gpu, job_type="compute")

        next_worker_memory = self.discord.worker_manager.find_first_worker(job_type="memory")
        best_worker_memory = self.discord.worker_manager.find_best_hardware_for_job(next_worker_gpu, job_type="memory")
        message = "Worker status:\n```"
        if next_worker_memory is not None:
            message = message + f"First GPU worker:     {next_worker_gpu.worker_id}\n"
            message = message + f"- " + str(self.discord.queue_manager.worker_queue_length(next_worker_gpu)) + " jobs in queue\n"
            message = message + f"Best GPU worker:     {best_worker_gpu.worker_id}\n"
            message = message + f"- " + str(self.discord.queue_manager.worker_queue_length(best_worker_gpu)) + " jobs in queue\n"
        else:
            message = message + "No GPU workers available.\n"
        if next_worker_compute is not None:
            message = message + f"First Compute worker: {next_worker_compute.worker_id}\n"
            message = message + f"- " + str(self.discord.queue_manager.worker_queue_length(next_worker_compute)) + " jobs in queue\n"
            message = message + f"Best Compute worker: {best_worker_compute.worker_id}\n"
            message = message + f"- " + str(self.discord.queue_manager.worker_queue_length(best_worker_compute)) + " jobs in queue\n"
        else:
            message = message + "No Compute workers available.\n"
        if next_worker_memory is not None:
            message = message + f"First Memory worker:  {next_worker_memory.worker_id}\n"
            message = message + f"- " + str(self.discord.queue_manager.worker_queue_length(next_worker_memory)) + " jobs in queue\n"
            message = message + f"Best Memory worker:  {best_worker_memory.worker_id}\n"
            message = message + f"- " + str(self.discord.queue_manager.worker_queue_length(best_worker_memory)) + " jobs in queue\n"
        else:
            message = message + "No Memory workers available.\n"
        message = message + "```"
        await ctx.send(message)

def setup(bot):
    bot.add_cog(Handler(bot))