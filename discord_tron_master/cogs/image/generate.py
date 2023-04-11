from discord.ext import commands
from asyncio import Lock
from discord_tron_master.classes.app_config import AppConfig
import logging, traceback
from discord_tron_master.bot import DiscordBot
from discord_tron_master.classes.jobs.image_generation_job import ImageGenerationJob
from discord_tron_master.bot import clean_traceback
# For queue manager, etc.
discord = DiscordBot.get_instance()

# Commands used for Stable Diffusion image gen.
class Generate(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = AppConfig()

    @commands.command(name="generate-x", help="Generates an image based on the given prompt, x number of times at once.")
    async def generate_range(self, ctx, count, *, prompt):
        if not count.isdigit():
            user_config = self.config.get_user_config(user_id=ctx.author.id)
            has_been_warned_about_count_being_digit = user_config.get("has_been_warned_about_count_being_digit", False)
            if not has_been_warned_about_count_being_digit:
                await ctx.send("Count must be a number. I assume you meant 3 images. Here you go! You'll never see this warning again. It's a sort of 'fuck you'.")
                self.config.set_user_setting("has_been_warned_about_count_being_digit", True);
            prompt = count + " " + prompt
            count = 3

        for i in range(0, int(count)):
            await self.generate(ctx, prompt=prompt)

    @commands.command(name="generate", help="Generates an image based on the given prompt.")
    async def generate(self, ctx, *, prompt):
        try:
            # Generate a "Job" object that will be put into the queue.
            discord_first_message = await ctx.send(f"Adding prompt to queue for processing: " + prompt)
            self.config.reload_config()
            job = ImageGenerationJob((self.bot, self.config, ctx, prompt, discord_first_message))
            # Get the worker that will process the job.
            worker = discord.worker_manager.find_best_fit_worker(job)
            if worker is None:
                await discord_first_message.edit(content="No workers available. Image was **not** added to queue. 😭 aw, how sad. 😭")
                # Wait a few seconds before deleting:
                await discord_first_message.delete(delay=10)
                return
            logging.info("Worker selected for job: " + str(worker.worker_id))
            # Add it to the queue
            await discord.queue_manager.enqueue_job(worker, job)
        except Exception as e:
            await ctx.send(
                f"Error generating image: {e}\n\nStack trace:\n{await clean_traceback(traceback.format_exc())}"
            )
