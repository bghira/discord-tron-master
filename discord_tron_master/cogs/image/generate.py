from discord.ext import commands
from asyncio import Lock
from discord_tron_master.classes.app_config import AppConfig
import logging
from discord_tron_master.bot import DiscordBot

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
            discord_first_message = await ctx.send(f"Adding prompt to queue for processing: " + prompt)

            # Put the context and prompt in a tuple before adding it to the queue
            await discord.queue_manager.put((ctx, prompt, discord_first_message))

            # Check if there are any running tasks
            if not hasattr(bot, "image_generation_tasks"):
                bot.image_generation_tasks = []

            # Remove any completed tasks
            bot.image_generation_tasks = [t for t in bot.image_generation_tasks if not t.done()]

            # If there are fewer tasks than allowed slots, create new tasks
            while len(bot.image_generation_tasks) < concurrent_slots:
                task = bot.loop.create_task(generate_image_from_queue())
                bot.image_generation_tasks.append(task)

        except Exception as e:
            await ctx.send(
                f"Error generating image: {e}\n\nStack trace:\n{traceback.format_exc()}"
            )
