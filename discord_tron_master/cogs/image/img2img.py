# Discord cog for watching on_message events with a mention of itself and an attached image, so that we can run an image variation job on it:

# Path: discord_tron_master/cogs/image/img2img.py
# Compare this snippet from discord_tron_master/cogs/image/img2img.py:
from io import BytesIO
from discord.ext import commands
from discord_tron_master.classes.app_config import AppConfig
import logging, traceback, discord
from PIL import Image
from discord_tron_master.bot import DiscordBot
from discord_tron_master.classes.jobs.promptless_variation_job import PromptlessVariationJob
from discord_tron_master.classes.jobs.prompt_variation_job import PromptVariationJob
from discord_tron_master.classes.jobs.image_generation_job import ImageGenerationJob
from discord_tron_master.classes.jobs.image_upscaling_job import ImageUpscalingJob
from discord_tron_master.bot import clean_traceback
from discord_tron_master.cogs.image.generate import Generate

# For queue manager, etc.
discord_wrapper = DiscordBot.get_instance()
from discord_tron_master.classes.openai.text import GPT

# Commands used for Stable Diffusion image gen.
class Img2img(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = AppConfig()

    @commands.Cog.listener()
    async def on_message(self, message):
        self.config.reload_config()
        if message.author == self.bot.user or message.author.bot:
            logging.debug("Ignoring message from this or another bot.")
            return
        # Threaded replies without any command prefix:
        in_my_thread = False
        is_in_thread = False
        if isinstance(message.channel, discord.Thread):
            is_in_thread = True

        # Whether we're in a thread the bot started.
        if is_in_thread and message.channel.owner_id == self.bot.user.id or message.author.bot:
            in_my_thread = True
        if is_in_thread and len(message.content) > 0 and message.content[0] == "*":
            # Respond to * as all bots.
            in_my_thread = True
        # Run only if it's in the bot's thread, and has no image attachments, and, has no "!" commands.
        if self.bot.user not in message.mentions and \
            in_my_thread and \
            not message.attachments \
            and message.content[0] != "!" \
            and message.content[0] != "+":
            print("Attempting to run generate command?")
            generator = self.bot.get_cog('Generate')
            prompt = message.content
            # Strip the star if it's there.
            if message.content[0] == "*":
                prompt = message.content[1:]
            # Now the whitespace:
            prompt = message.content.strip()
            await generator.generate(message, prompt=prompt)
            return

        # Img2Img via bot @mention
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
                        return await self._handle_image_attachment(message, attachment)
                    except Exception as e:
                        await message.channel.send(
                            f"Error generating image: {e}\n\nStack trace:\n{await clean_traceback(traceback.format_exc())}"
                        )
            else:
                # We were mentioned, but no attachments. They must want to converse.
                logging.debug("Message contains no attachments. Initiating conversation.")
                try:
                    gpt = GPT()
                    from discord_tron_master.classes.openai.chat_ml import ChatML
                    from discord_tron_master.models.conversation import Conversations
                    app = AppConfig.flask
                    with app.app_context():
                        user_conversation = Conversations.create(message.author.id, self.config.get_user_setting(message.author.id, "gpt_role"), Conversations.get_new_history())
                        chat_ml = ChatML(user_conversation)
                    await chat_ml.add_user_reply(message.content)
                    response = await gpt.discord_bot_response(prompt=await chat_ml.get_prompt(), ctx=message)
                    await chat_ml.add_assistant_reply(response)
                    await DiscordBot.send_large_message(message, message.author.mention + ' ' + ChatML.clean(response))
                except Exception as e:
                    await message.channel.send(
                        f"{message.author.mention} I am sorry, friend. I had an error while generating text inference: {e}"
                    )
                    logging.error(f"Error generating text inference: {e}\n\nStack trace:\n{await clean_traceback(traceback.format_exc())}")
    async def _handle_image_attachment(self, message, attachment):
        # Generate a "Job" object that will be put into the queue.
        discord_first_message = await message.channel.send(f"{message.author.mention} Adding image to queue for processing")
        # Does message contain "!upscale"?
        if "!upscale" in message.content:
            # Remove "!upscale" from the contents:
            message.content = message.content.replace("!upscale", "")
            job = ImageUpscalingJob((self.bot, self.config, message, message.content, discord_first_message, attachment.url))
        elif message.content != "":
            job = PromptVariationJob((self.bot, self.config, message, message.content, discord_first_message, attachment.url))
        else:
            # Default to image variation job
            job = PromptlessVariationJob((self.bot, self.config, message, message.content, discord_first_message, attachment.url))
        # Get the worker that will process the job.
        worker = discord_wrapper.worker_manager.find_best_fit_worker(job)
        if worker is None:
            await discord_first_message.edit(content="No workers available. Image was **not** added to queue. 😭 aw, how sad. 😭")
            # Wait a few seconds before deleting:
            await discord_first_message.delete(delay=10)
            return
        logging.info("Worker selected for job: " + str(worker.worker_id))
        # Add it to the queue
        await discord_wrapper.queue_manager.enqueue_job(worker, job)
