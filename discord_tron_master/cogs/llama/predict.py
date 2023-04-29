import logging, traceback
from discord.ext import commands
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.bot import DiscordBot
from discord_tron_master.classes.jobs.llama_prediction_job import LlamaPredictionJob
from discord_tron_master.bot import clean_traceback
# For queue manager, etc.
discord = DiscordBot.get_instance()
logging.debug(f"Loading StableLM predict helper")
# Commands used for Stable Diffusion image gen.
class Predict(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = AppConfig()

    @commands.command(name="l", help="An alias for `!llama`")
    async def l(self, ctx, *, prompt):
        self.llama(ctx, prompt=prompt)

    @commands.command(name="llama", help="Generates an image based on the given prompt.")
    async def llama(self, ctx, *, prompt):
        try:
            # Generate a "Job" object that will be put into the queue.
            discord_first_message = await DiscordBot.send_large_message(ctx=ctx, text="A worker has been selected for your query: `" + prompt + "`")

            self.config.reload_config()

            job = LlamaPredictionJob((self.bot, self.config, ctx, prompt, discord_first_message))
            # Get the worker that will process the job.
            worker = discord.worker_manager.find_best_fit_worker(job)
            if worker is None:
                await discord_first_message.edit(content="No workers available. Llama query was **not** added to queue. 😭 aw, how sad. 😭")
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