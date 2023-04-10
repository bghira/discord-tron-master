# Discord cog for watching on_message events with a mention of itself and an attached image, so that we can run an image variation job on it:

# Path: discord_tron_master/cogs/image/img2img.py
# Compare this snippet from discord_tron_master/cogs/image/img2img.py:
from io import BytesIO
from discord.ext import commands
from discord_tron_master.classes.app_config import AppConfig
import logging, traceback
from PIL import Image
from discord_tron_master.bot import DiscordBot
from discord_tron_master.classes.jobs.image_variation_job import ImageVariationJob
from discord_tron_master.bot import clean_traceback
# For queue manager, etc.
discord = DiscordBot.get_instance()

# Commands used for Stable Diffusion image gen.
class Img2img(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = AppConfig()

    @commands.Cog.listener()
    async def on_message(self, message):
        self.config.reload_config()
        if message.author == self.bot.user:
            logging.debug("Ignoring message from self.")
            return
        if self.bot.user in message.mentions:
            logging.debug("Message contains mention of self.")
            # Strip the mention from the message:
            message.content = message.content.replace(f"<@{self.bot.user.id}>", "")
            message.content = message.content.replace(f"<@!{self.bot.user.id}>", "")
            if len(message.attachments) > 0:
                logging.debug("Message contains attachment.")
                attachment = message.attachments[0]
                if attachment.content_type.startswith("image/"):
                    logging.debug("Attachment is an image.")
                    try:
                        # Generate a "Job" object that will be put into the queue.
                        discord_first_message = await message.channel.send(f"Adding image to queue for processing: " + attachment.url)
                        job = ImageVariationJob((self.bot, self.config, message, message.content, discord_first_message, attachment.url))
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
                        await message.channel.send(
                            f"Error generating image: {e}\n\nStack trace:\n{await clean_traceback(traceback.format_exc())}"
                        )

