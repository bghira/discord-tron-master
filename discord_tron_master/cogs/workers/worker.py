from discord.ext import commands
from asyncio import Lock
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.bot import DiscordBot

class Worker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = AppConfig()
        self.discord = DiscordBot.get_instance()

    @commands.command(name="worker-stats", help="Shows worker stats.")
    async def workers(self, ctx):
        user_id = ctx.author.id
        user_config = self.config.get_user_config(user_id=user_id)

        next_worker_gpu = self.discord.worker_manager.find_worker_with_fewest_queued_tasks_by_job_type(job_type="gpu")
        next_worker_compute = self.discord.worker_manager.find_worker_with_fewest_queued_tasks_by_job_type(job_type="compute")
        next_worker_memory = self.discord.worker_manager.find_worker_with_fewest_queued_tasks_by_job_type(job_type="memory")
        message = "Worker status:\n"
        message = message + "```"
        if next_worker_memory is not None:
            message = message + f"First GPU worker:     {next_worker_gpu.worker_id}\n"
            message = message + f"- " + str(self.discord.queue_manager.worker_queue_length(next_worker_gpu)) + " jobs in queue\n"
        else:
            message = message + "No GPU workers available.\n"
        message = message + "```"
        message = message + "```"
        if next_worker_compute is not None:
            message = message + f"First Compute worker: {next_worker_compute.worker_id}\n"
            message = message + f"- " + str(self.discord.queue_manager.worker_queue_length(next_worker_compute)) + " jobs in queue\n"
        else:
            message = message + "No Compute workers available.\n"
        message = message + "```"
        message = message + "```"
        if next_worker_memory is not None:
            message = message + f"First Memory worker:  {next_worker_memory.worker_id}\n"
            message = message + f"- " + str(self.discord.queue_manager.worker_queue_length(next_worker_memory)) + " jobs in queue\n"
        else:
            message = message + "No Memory workers available.\n"
        message = message + "```"
        
        all_workers = self.discord.worker_manager.get_all_workers()
        if len(all_workers) > 0:
            # List all payloads from all workers:
            message = message + f"All workers:\n"
            for worker_id in all_workers:
                worker = all_workers[worker_id]
                message = message + f"Worker {worker_id}:\n"
                message = message + f"- {await worker.job_queue.view_payload_prompts()}\n"
        if hasattr(ctx, "message"):
            await ctx.message.delete()
        else:
            await ctx.delete()
        await self.discord.send_large_message(ctx, message, delete_delay=15)

def setup(bot):
    bot.add_cog(Worker(bot))