from discord.ext import commands
from asyncio import Lock
from discord_tron_master.classes.openai.text import GPT
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.models.user_history import UserHistory
import discord_tron_master.classes.discord.message_helpers as helper
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

    @commands.command(name="generate-random-x", help="Generates images based on a random prompt, x number of times at once.")
    async def generate_range_random(self, ctx, arg_count = None, *, theme = None):
        worker = discord.worker_manager.find_first_worker("gpu")
        if worker is None:
            discord_first_message = await ctx.send("No workers available. Image was **not** added to queue. 😭 aw, how sad. 😭")
            # Wait a few seconds before deleting:
            await discord_first_message.delete(delay=10)
            return
        if arg_count is None:
            # We have no arg_count. They probably want the default 3 images.
            user_config = self.config.get_user_config(user_id=ctx.author.id)
            has_been_warned_about_count_being_digit = user_config.get("has_been_warned_about_count_being_digit", False)
            if not has_been_warned_about_count_being_digit:
                await ctx.send("Count must be a number. I assume you meant 3 images. Here you go! You'll never see this warning again. It's a sort of 'fuck you'.")
                self.config.set_user_setting(ctx.author.id, "has_been_warned_about_count_being_digit", True);
            count = 3
        out_theme = ''
        if not arg_count.isdigit():
            # We have arg_count, but the value is non-numeric. Set the default to three, and use the arg_count as the theme.
            out_theme = arg_count
            count = 3
        if arg_count.isdigit():
            # We have a numeric arg_count.
            count = arg_count
        if theme is not None:
            out_theme = out_theme + ' ' + theme

        for i in range(0, int(count)):
            worker = discord.worker_manager.find_first_worker("gpu")
            if worker is None:
                discord_first_message = await ctx.send("No workers available. Image was **not** added to queue. 😭 aw, how sad. 😭")
                return
            gpt = GPT()
            prompt = await gpt.random_image_prompt(out_theme)
            logging.info(f"Random prompt generated by GPT: {prompt}")
            await self.generate(ctx, prompt=prompt)


    @commands.command(name="generate-x", help="Generates an image based on the given prompt, x number of times at once.")
    async def generate_range(self, ctx, count, *, prompt):
        if not count.isdigit():
            user_config = self.config.get_user_config(user_id=ctx.author.id)
            has_been_warned_about_count_being_digit = user_config.get("has_been_warned_about_count_being_digit", False)
            if not has_been_warned_about_count_being_digit:
                await ctx.send("Count must be a number. I assume you meant 3 images. Here you go! You'll never see this warning again. It's a sort of, '*au revoir*'.")
                self.config.set_user_setting(ctx.author.id, "has_been_warned_about_count_being_digit", True);
            prompt = count + " " + prompt
            count = 3

        for i in range(0, int(count)):
            await self.generate(ctx, prompt=prompt)

    @commands.command(name="generate", help="Generates an image based on the given prompt.")
    async def generate(self, ctx, *, prompt):
        # If prompt has \n, we split:
        if '\n' in prompt and '--multiline' not in prompt and '!multiline' not in prompt:
            # send a message signifying to the user they can use --multiline or !multiline as a flag to use the prompt as-is.
            await DiscordBot.send_large_message(ctx=ctx, text=f"{ctx.author.mention}: You can use `--multiline` or `!multiline` as a flag to use the prompt as-is. I will split your prompt into multiple images without that.")
            prompts = prompt.split('\n')
            # Remove blank prompts
            prompts = [p for p in prompts if p != '']
        elif prompt == 'unconditional' or prompt == 'blank':
            prompts = ['']
        else:
            prompts = [ prompt ]
        idx = 0
        for _prompt in prompts:
            try:
                self.config.reload_config()
                extra_payload = {
                    "user_config": self.config.get_user_config(user_id=ctx.author.id),
                    "user_id": ctx.author.id
                }
                if extra_payload["user_config"].get("auto_model", True):
                    # We are going to ask OpenAI which model to use for this user.
                    gpt = GPT()
                    auto_resolution, auto_model = await gpt.auto_model_select(_prompt)
                    logging.info(f"Auto-model selected by GPT: {auto_model}")
                    extra_payload["user_config"]["model"] = auto_model
                    extra_payload["user_config"]["resolution"] = auto_resolution
                discord_first_message = await DiscordBot.send_large_message(ctx=ctx, text=f"Job queuing: `" + _prompt + "`")
                job = ImageGenerationJob(ctx.author.id, (self.bot, self.config, ctx, _prompt, discord_first_message), extra_payload=extra_payload)
                # Get the worker that will process the job.
                worker = discord.worker_manager.find_best_fit_worker(job)
                if worker is None:
                    await discord_first_message.edit(content="No workers available. Image was **not** added to queue. 😭 aw, how sad. 😭")
                    # Wait a few seconds before deleting:
                    await discord_first_message.delete(delay=10)
                    return
                app = AppConfig.flask
                with app.app_context():
                    try:
                        user_history = UserHistory.add_entry(user=ctx.author.id, message=int(f"{ctx.id if hasattr(ctx, 'id') else ctx.message.id}{idx}"), prompt=_prompt, config_blob=extra_payload["user_config"])
                        idx += 1
                    except Exception as e:
                        logging.warning(f"Had trouble adding the user history entry: {e}")
                # Generate a "Job" object that will be put into the queue.
                await discord_first_message.edit(content=f"Job {job.id} queued on {worker.worker_id}: `" + _prompt + "`")
                logging.info("Worker selected for job: " + str(worker.worker_id))
                # Add it to the queue
                await discord.queue_manager.enqueue_job(worker, job)
            except Exception as e:
                await ctx.send(
                    f"Error generating image: {e}\n\nStack trace:\n{await clean_traceback(traceback.format_exc())}"
                )

    @commands.command(name="stats", help="View user generation statistics.")
    async def get_statistics(self, ctx, user_id = None):
        if user_id is None:
            user_id = ctx.author.id
        try:
            app = AppConfig.flask
            with app.app_context():
                user_history = UserHistory.get_by_user(user_id)
                if user_history is None:
                    await ctx.send(
                        f"{ctx.author.mention} I have no history for that user."
                    )
                    return
                user_statistics = UserHistory.get_user_statistics(user_id)
                output_string = "I have no stats available for that user."
                if user_statistics is not None:
                    total_generations = user_statistics.get("total", 0)
                    unique_generations = user_statistics.get("unique", 0)
                    common_terms = user_statistics.get("common_terms", None)
                    if not common_terms:
                        common_terms = "None"
                await ctx.send(
                    f"{ctx.author.mention}"
                    f"\n -> Total generations: {total_generations}"
                    f"\n -> Unique prompts: {unique_generations}"
                    f"\n -> {common_terms}"
                )
        except Exception as e:
            logging.error("Caught error when getting user history: " + str(e))
            await ctx.send(
                f"{ctx.author.mention} Statistics are not currently available at this time, try again later."
            )

    @commands.command(name="invite", help="Invites the user to the latest thread in the channel.")
    async def invite_to_thread(self, ctx):
        try:
            channel = ctx.channel
            thread = await helper.most_recently_active_thread(channel)
            if thread is None:
                await ctx.send(
                    f"{ctx.author.mention} There are no threads in this channel. You can create one by using !generate <prompt>."
                )
                return
            # Ping the user in the thread.
            await thread.send(
                f"{ctx.author.mention} is now in the party."
            )
        except Exception as e:
            logging.error("Caught error when inviting to thread: " + str(e))
            await ctx.send(
                f"{ctx.author.mention} {self.generic_error}."
            )

    async def generate_from_user_config(self, ctx, user_config, user_id, prompt):
        # If prompt has \n, we split:
        if '\n' in prompt:
            prompts = prompt.split('\n')
        else:
            prompts = [ prompt ]
        idx = 0
        for _prompt in prompts:
            try:
                # Generate a "Job" object that will be put into the queue.
                discord_first_message = await DiscordBot.send_large_message(ctx=ctx, text="Queued: `" + _prompt + "`")
                self.config.reload_config()
                job = ImageGenerationJob(user_id, (self.bot, self.config, ctx, _prompt, discord_first_message), {"user_config": user_config, "user_id": user_id })
                # Get the worker that will process the job.
                worker = discord.worker_manager.find_best_fit_worker(job)
                if worker is None:
                    await discord_first_message.edit(content="No workers available. Image was **not** added to queue. 😭 aw, how sad. 😭")
                    # Wait a few seconds before deleting:
                    await discord_first_message.delete(delay=10)
                    return
                app = AppConfig.flask
                with app.app_context():
                    try:
                        user_history = UserHistory.add_entry(user=user_id, message=int(f"{ctx.id if hasattr(ctx, 'id') else ctx.message.id}{idx}"), prompt=_prompt, config_blob=user_config)
                        idx += 1
                    except Exception as e:
                        logging.warning(f"Had trouble adding the user history entry: {e}")
                logging.info("Worker selected for job: " + str(worker.worker_id))
                # Add it to the queue
                await discord.queue_manager.enqueue_job(worker, job)
            except Exception as e:
                await ctx.send(
                    f"Error generating image: {e}\n\nStack trace:\n{await clean_traceback(traceback.format_exc())}"
                )