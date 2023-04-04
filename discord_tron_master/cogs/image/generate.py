from discord.ext import commands
from asyncio import Lock
from discord_tron_master.classes.app_config import AppConfig
import logging
from discord_tron_master.bot import DiscordBot
from discord_tron_master.classes.jobs.image_generation_job import ImageGenerationJob

# For queue manager, etc.
discord = DiscordBot.get_instance()

# Commands used for Stable Diffusion image gen.
class Generate(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = AppConfig()

    @commands.command(name="generate", help="Generates an image based on the given prompt.")
    async def generate(self, ctx, *, prompt):
        try:
            logging.info("Begin generate command coroutine.")

            # Send a message so that they know we received theirs.
            discord_first_message = await ctx.send(f"Adding prompt to queue for processing: " + prompt)

            # Generate a "Job" object that will be put into the queue.
            job = ImageGenerationJob((self.bot, self.config, ctx, prompt, discord_first_message))

            # Get the worker that will process the job.
            worker = discord.queue_manager.get_worker_for_job(job)

            # Add it to the queue
            await worker.add_job(job)
        except Exception as e:
            await ctx.send(
                f"Error generating image: {e}\n\nStack trace:\n{traceback.format_exc()}"
            )
