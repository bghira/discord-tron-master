# Discord cog for watching on_message events with a mention of itself and an attached image, so that we can run an image variation job on it:

# Path: discord_tron_master/cogs/image/img2img.py
# Compare this snippet from discord_tron_master/cogs/image/img2img.py:
from io import BytesIO
from discord.ext import commands
from discord_tron_master.classes.app_config import AppConfig
import logging, traceback, discord
from PIL import Image
from discord_tron_master.bot import DiscordBot
from discord_tron_master.classes.jobs.promptless_variation_job import (
    PromptlessVariationJob,
)
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

        # Ignore bot messages
        if message.author == self.bot.user or message.author.bot:   
            logging.debug("Ignoring message from this or another bot.")
            return

        if isinstance(message.channel, discord.Thread) and message.channel.owner_id == self.bot.user.id and not self.bot.user in message.mentions:
            await self._handle_thread_message(message)
        elif self.bot.user in message.mentions:
            await self._handle_mentioned_message(message)

    async def _handle_thread_message(self, message):
        if message.content.startswith("!") or message.content.startswith("+"):
            return
        # Handle * commands for all bots
        prompt = message.content.strip("*").strip()
        if prompt:
            generator = self.bot.get_cog("Generate")
            await generator.generate(message, prompt=prompt)

    async def _handle_mentioned_message(self, message):
        # Clean the message content
        message.content = message.content.replace(f"<@{self.bot.user.id}>", "").replace(f"<@!{self.bot.user.id}>", "").strip()
        # Log the content
        logging.debug(f"Message content: {message.content}")
        # Handle image attachments
        if message.attachments:
            attachment = message.attachments[0]
            if attachment.content_type.startswith("image/"):
                try:
                    return await self._handle_image_attachment(message, attachment)
                except Exception as e:
                    await message.channel.send(
                        f"Error generating image: {e}\n\nStack trace:\n{await clean_traceback(traceback.format_exc())}"
                    )
            return

        # There might be URLs in the message body, grab them via regex and check if they're images. The images may be surrounded by <> or () or [] or nothing.
        import re
        url_list = re.findall(r'((?:<|[(|\[])?https?://[^\s]+(?:>|)|[)|\]]))', message.content)
        
        if len(url_list) > 0:
            # Remove the URLs from the original string:
            message.content = re.sub(r'(https?://[^\s]+)', '', message.content).strip()
            for url in url_list:
                if url.endswith(".png") or url.endswith(".jpg") or url.endswith(".jpeg") or url.endswith(".webp"):
                    try:
                        return await self._handle_image_attachment(message, url)
                    except Exception as e:
                        await message.channel.send(
                            f"Error generating image: {e}\n\nStack trace:\n{await clean_traceback(traceback.format_exc())}"
                        )
                    return

        # Handle conversation
        try:
            gpt = GPT()
            from discord_tron_master.classes.openai.chat_ml import ChatML
            from discord_tron_master.models.conversation import Conversations

            app = AppConfig.flask
            with app.app_context():
                user_conversation = Conversations.create(
                    message.author.id,
                    self.config.get_user_setting(message.author.id, "gpt_role"),
                    Conversations.get_new_history(),
                )
                chat_ml = ChatML(user_conversation)
            await chat_ml.add_user_reply(message.content)
            response = await gpt.discord_bot_response(
                prompt=await chat_ml.get_prompt(), ctx=message
            )
            await chat_ml.add_assistant_reply(response)
            await DiscordBot.send_large_message(
                message, message.author.mention + " " + ChatML.clean(response)
            )
        except Exception as e:
            await message.channel.send(
                f"{message.author.mention} I am sorry, friend. I had an error while generating text inference: {e}"
            )
            logging.error(
                f"Error generating text inference: {e}\n\nStack trace:\n{await clean_traceback(traceback.format_exc())}"
            )

    async def _handle_image_attachment(
        self,
        message,
        attachment,
        prompt_override: str = None,
        user_config_override: dict = None,
    ):
        # Generate a "Job" object that will be put into the queue.
        discord_first_message = await message.channel.send(
            f"{message.author.mention} Adding image to queue for processing"
        )
        # Does message contain "!upscale"?
        extra_payload = None
        if user_config_override != None:
            extra_payload = user_config_override
        if "!upscale" in message.content:
            # Remove "!upscale" from the contents:
            message.content = message.content.replace("!upscale", "")
            job = ImageUpscalingJob(
                (
                    self.bot,
                    self.config,
                    message,
                    message.content,
                    discord_first_message,
                    attachment.url,
                )
            )
        elif message.content != "" or prompt_override is not None:
            prompt = message.content
            if prompt_override != None:
                prompt = prompt_override
                attachment_url = attachment
            elif hasattr(attachment, "url"):
                attachment_url = attachment.url
            elif "http" in attachment:
                attachment_url = attachment
            job = PromptVariationJob(
                (
                    self.bot,
                    self.config,
                    message,
                    prompt,
                    discord_first_message,
                    attachment_url,
                ),
                extra_payload=extra_payload,
            )
        else:
            # Default to image variation job
            job = PromptlessVariationJob(
                (
                    self.bot,
                    self.config,
                    message,
                    message.content,
                    discord_first_message,
                    attachment.url,
                )
            )
        # Get the worker that will process the job.
        worker = discord_wrapper.worker_manager.find_best_fit_worker(job)
        if worker is None:
            await discord_first_message.edit(
                content="No workers available. Image was **not** added to queue. 😭 aw, how sad. 😭"
            )
            # Wait a few seconds before deleting:
            await discord_first_message.delete(delay=10)
            return
        logging.info("Worker selected for job: " + str(worker.worker_id))
        # Add it to the queue
        await discord_wrapper.queue_manager.enqueue_job(worker, job)
