# Discord cog for watching on_message events with a mention of itself and an attached image, so that we can run an image variation job on it:

# Path: discord_tron_master/cogs/image/img2img.py
# Compare this snippet from discord_tron_master/cogs/image/img2img.py:
from io import BytesIO
import asyncio
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
from discord_tron_master.adapters.emulator_bridge import EmulatorBridge as ZorkEmulator
from discord_tron_master.classes.discord.url_helpers import (
    find_urls,
    is_direct_image_url,
    remove_url,
    suppress_url_embeds,
)
from discord_tron_master.classes.discord.attachment_helpers import (
    build_attachment_context,
    is_image_attachment,
)
from discord_tron_master.classes.discord_memory import DiscordMemory

# For queue manager, etc.
discord_wrapper = DiscordBot.get_instance()
from discord_tron_master.classes.openai.text import GPT


# Commands used for Stable Diffusion image gen.
class Img2img(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = AppConfig()

    def _get_conversation_owner(self, message):
        if not self.config.get_user_setting(message.author.id, "group_chat"):
            return message.author.id
        if message.guild is None:
            return message.author.id
        if isinstance(message.channel, discord.Thread):
            return message.author.id
        return message.channel.id

    @commands.Cog.listener()
    async def on_message(self, message):
        self.config.reload_config()

        # Ignore bot messages
        if (
            "SimpleTuner has launched. Hold onto your butts!" not in message.content
            and (message.author == self.bot.user or message.author.bot)
        ):
            logging.debug("Ignoring message from this or another bot.")
            return
        elif (
            "SimpleTuner has launched. Hold onto your butts!" in message.content
            and message.author.bot
        ):
            # An application has posted a ST update. Let's remove previous updates and return.
            logging.info(
                f"Found a SimpleTuner update message from {message.author}. Removing previous messages."
            )
            await self._clear_previous_simpletuner_messages(message)

            return

        if message.guild is not None:
            app = AppConfig.get_flask()
            if app is not None:
                with app.app_context():
                    if ZorkEmulator.is_channel_enabled(
                        message.guild.id, message.channel.id
                    ):
                        return

        if (
            isinstance(message.channel, discord.Thread)
            and message.channel.owner_id == self.bot.user.id
            and not self.bot.user in message.mentions
        ):
            await self._handle_thread_message(message)
        elif self.bot.user in message.mentions:
            await self._handle_mentioned_message(message)

    async def _clear_previous_simpletuner_messages(self, message):
        # Look for the contents inside the `.*` at the beginning of the string. this is the search identifier.
        import re

        logging.info(f"Running regex identifier search")
        search_identifier = re.search(r"`.*`", message.content)
        logging.info(f"Search identifier: {search_identifier}")
        # Now, let's search for messages that contain the search identifier in the message channel.
        async for msg in message.channel.history(limit=1000):
            if search_identifier.group(0) in msg.content:
                logging.info(f"Found message with search identifier: {msg.content}")
                # Is it the first message? if so, don't delete.
                if msg.id == message.id:
                    continue
                await msg.delete()

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
        message.content = (
            message.content.replace(f"<@{self.bot.user.id}>", "")
            .replace(f"<@!{self.bot.user.id}>", "")
            .strip()
        )
        direct_url_source = message.content
        # Log the content
        logging.debug(f"Message content: {message.content}")
        # Read document attachments into chat context. Image-only messages retain
        # the existing variation/upscale workflow.
        if message.attachments:
            attachment_context = await build_attachment_context(message.attachments)
            if attachment_context:
                message.content = (
                    f"{message.content}\n\n{attachment_context}".strip()
                )
            else:
                attachment = next(
                    (
                        item
                        for item in message.attachments
                        if is_image_attachment(item)
                    ),
                    None,
                )
                if attachment is None:
                    await message.channel.send(
                        f"{message.author.mention} I couldn't read that attachment. "
                        "Try a text, Markdown, code, JSON, YAML, CSV, or config file."
                    )
                    return
                try:
                    return await self._handle_image_attachment(message, attachment)
                except Exception as e:
                    await message.channel.send(
                        f"Error generating image: {e}\n\nStack trace:\n{await clean_traceback(traceback.format_exc())}"
                    )
                    return

        # Direct image URLs are consumed by the image path. Other URLs must remain
        # in the prompt so the chat model and its web/repository tools can read them.
        for url in find_urls(direct_url_source):
            if not is_direct_image_url(url):
                continue
            message.content = remove_url(message.content, url)
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
                conversation_owner = self._get_conversation_owner(message)
                user_conversation = Conversations.create(
                    conversation_owner,
                    self.config.get_user_setting(message.author.id, "gpt_role"),
                    Conversations.get_new_history(),
                )
                chat_ml = ChatML(user_conversation, config_user_id=message.author.id)
            await chat_ml.add_user_reply(message.content)
            response = await gpt.discord_bot_response(
                prompt=await chat_ml.get_prompt(),
                ctx=message,
                memory_scope_id=conversation_owner,
            )
            response = GPT.ensure_requested_discord_mentions(message.content, response)
            response = suppress_url_embeds(response)
            await chat_ml.add_assistant_reply(response)
            sent_message = await DiscordBot.send_large_message(
                message, message.author.mention + " " + ChatML.clean(response)
            )
            try:
                await asyncio.to_thread(
                    DiscordMemory.store_exchange,
                    conversation_id=conversation_owner,
                    user_message_id=message.id,
                    assistant_message_id=getattr(sent_message, "id", None),
                    guild_id=getattr(message.guild, "id", None),
                    channel_id=getattr(message.channel, "id", None),
                    author_id=message.author.id,
                    author_name=getattr(message.author, "display_name", str(message.author)),
                    user_text=message.content,
                    assistant_text=response,
                )
            except Exception:
                logging.exception("Failed to store Discord conversation memory")
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
        mention_string = message.author.mention
        author_id = message.author.id
        if user_config_override is not None and "user_id" in user_config_override:
            mention_string = f"<@{user_config_override['user_id']}>"
            author_id = user_config_override["user_id"]
        discord_first_message = await message.channel.send(
            f"{mention_string} Adding image to queue for processing"
        )
        # Does message contain "!upscale"?
        extra_payload = None
        if user_config_override != None:
            extra_payload = user_config_override
        if "!upscale" in message.content:
            # Remove "!upscale" from the contents:
            message.content = message.content.replace("!upscale", "")
            job = ImageUpscalingJob(
                author_id,
                (
                    self.bot,
                    self.config,
                    message,
                    message.content,
                    discord_first_message,
                    attachment.url,
                ),
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
                author_id,
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
                author_id,
                (
                    self.bot,
                    self.config,
                    message,
                    message.content,
                    discord_first_message,
                    attachment.url,
                ),
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
