from discord.ext import commands
from asyncio import Lock
from io import BytesIO
import discord as discord_lib
from discord_tron_master.classes.openai.text import GPT
from discord_tron_master.cogs.image import generate_image
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.models.user_history import UserHistory
import discord_tron_master.classes.discord.message_helpers as helper
import logging, traceback
logger = logging.getLogger("discord_tron.image.generate")
logger.setLevel('DEBUG')
from discord_tron_master.bot import DiscordBot
from discord_tron_master.classes.jobs.image_generation_job import ImageGenerationJob
from discord_tron_master.bot import clean_traceback
# For queue manager, etc.
from threading import Thread
discord = DiscordBot.get_instance()

from discord_tron_master.classes.guilds import Guilds

guild_config = Guilds()

# Commands used for Stable Diffusion image gen.
class Generate(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = AppConfig()

    @commands.command(name="generate-random-x", help="Generates images based on a random prompt, x number of times at once.")
    async def generate_range_random(self, ctx, arg_count = None, *, theme = None):
        if guild_config.is_channel_banned(ctx.guild.id, ctx.channel.id):
            return
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
            logger.info(f"Random prompt generated by GPT: {prompt}")
            await self.generate(ctx, prompt=prompt)


    @commands.command(name="generate-x", help="Generates an image based on the given prompt, x number of times at once.")
    async def generate_range(self, ctx, count, *, prompt):
        if guild_config.is_channel_banned(ctx.guild.id, ctx.channel.id):
            return
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

    @commands.command(name="compare", help="Generate an SD3 and DALLE3 image, side by side for comparison.")
    async def generate_sd3_dalle_comparison(self, ctx, *, prompt):
        if guild_config.is_channel_banned(ctx.guild.id, ctx.channel.id):
            return
        await generate_image(ctx, prompt)


    @commands.command(name="dalle", help="Generates an image based on the given prompt using DALL-E.")
    async def generate_dalle(self, ctx, *, prompt):
        if guild_config.is_channel_banned(ctx.guild.id, ctx.channel.id):
            return
        from discord_tron_master.classes.openai.text import GPT
        gpt = GPT()
        image_output = await gpt.dalle_image_generate(prompt=prompt, user_config=self.config.get_user_config(user_id=ctx.author.id))
        await ctx.channel.send(file=discord_lib.File(BytesIO(image_output), "image.png"))

    @commands.command(name="sd3", help="Generates an image based on the given prompt using Stable Diffusion 3.")
    async def generate_sd3(self, ctx, *, prompt):
        if guild_config.is_channel_banned(ctx.guild.id, ctx.channel.id):
            logging.warning("Not generating image in channel. Channel is banned from gen.")
            return
        logging.info("Generating Stable Diffusion 3 image.")
        from discord_tron_master.classes.stabilityai.api import StabilityAI
        stabilityai = StabilityAI()
        prompt = prompt.replace('--sd3', '').strip()
        user_config = self.config.get_user_config(user_id=ctx.author.id)
        try:
            image = stabilityai.generate_image(prompt, user_config, model="sd3-turbo")
            logging.info("Sending SD3 image to channel.")
            await ctx.channel.send(file=discord_lib.File(BytesIO(image), "image.png"))
        except Exception as e:
            await ctx.channel.send(f"Error generating image: {e}")

    @commands.command(name="generate", help="Generates an image based on the given prompt.")
    async def generate(self, ctx, *, prompt):
        if guild_config.is_channel_banned(ctx.guild.id, ctx.channel.id):
            return
        # If prompt has \n, we split:
        if '\n' in prompt and '--multiline' not in prompt and '!multiline' not in prompt:
            # send a message signifying to the user they can use --multiline or !multiline as a flag to use the prompt as-is.
            await DiscordBot.send_large_message(ctx=ctx, text=f"{ctx.author.mention}: You can use `--multiline` or `!multiline` as a flag to use the prompt as-is. I will split your prompt into multiple images without that.")
            prompts = prompt.split('\n')
            # Remove blank prompts
            prompts = [p for p in prompts if p != '']
        elif '--sd3' in prompt:
            from discord_tron_master.classes.stabilityai.api import StabilityAI
            stabilityai = StabilityAI()
            prompt = prompt.replace('--sd3', '').strip()
            user_config = self.config.get_user_config(user_id=ctx.author.id)
            try:
                image = stabilityai.generate_image(prompt, user_config, model="sd3-turbo")
                await ctx.channel.send(file=discord_lib.File(BytesIO(image), "image.png"))
            except Exception as e:
                await ctx.send(f"Error generating image: {e}")
        elif '--sd3-full' in prompt:
            from discord_tron_master.classes.stabilityai.api import StabilityAI
            stabilityai = StabilityAI()
            prompt = prompt.replace('--sd3-full', '').strip()
            # remove any other -- params
            prompt = prompt.split('--')[0]
            user_config = self.config.get_user_config(user_id=ctx.author.id)
            try:
                image = stabilityai.generate_image(prompt, user_config, model="sd3")
                await ctx.channel.send(file=discord_lib.File(BytesIO(image), "image.png"))
            except Exception as e:
                await ctx.send(f"Error generating image: {e}")
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
                    logger.info(f"Auto-model selected by GPT: {auto_model}")
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
                        logger.warning(f"Had trouble adding the user history entry: {e}")
                # Generate a "Job" object that will be put into the queue.
                await discord_first_message.edit(content=f"Job {job.id} queued on {worker.worker_id}: `" + _prompt + "`")
                logger.info("Worker selected for job: " + str(worker.worker_id))
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
                    frequent_prompts = user_statistics.get("frequent_prompts", None)
                    if not common_terms:
                        common_terms = "None"
                sentiment_analysis = "No sentiment analysis was available."
                try:
                    gpt = GPT()
                    sentiment_analysis = await gpt.sentiment_analysis(UserHistory.get_user_most_common_prompts(user=user_id, limit=1000))
                except:
                    pass
                await DiscordBot.send_large_message(
                    ctx,
                    text=(
                        f"{ctx.author.mention}"
                        f"\n -> Total generations: {total_generations}"
                        f"\n -> Unique prompts: {unique_generations}"
                        f"\n -> {frequent_prompts}"
                        f"\n -> {common_terms}"
                        f"\n {sentiment_analysis}"
                    )
                )
        except Exception as e:
            logger.error("Caught error when getting user history: " + str(e))
            await ctx.send(
                f"{ctx.author.mention} Statistics are not currently available at this time, try again later."
            )
            
    @commands.command(name='search', help='Search for a prompt in your history. Supports wildcards (*, ?) and exclusion via --not TERM.')
    async def search_prompts(self, ctx, *, search_string: str):
        """
        Usage Examples:
        1) !search cat
        -> finds prompts containing the substring "cat"
        2) !search cat dog
        -> finds prompts containing "cat dog" exactly, as a substring
        3) !search cat* --not dog
        -> finds prompts with "cat" as a prefix and excludes any containing "dog"
        4) !search cat* --not dog --not "pink"
        -> excludes dog, pink, etc.
        5) !search cat?milk
        -> replaces ? with '_' so effectively "cat_milk"
        """
        try:
            app = AppConfig.flask
            with app.app_context():
                # 1) Split out any --not terms. Keep them in a list of excludes.
                import shlex

                # We'll do a minimal parse of the search_string
                tokens = shlex.split(search_string)
                excludes = []
                included_tokens = []
                
                skip_next = False
                for i, token in enumerate(tokens):
                    # If we've already used this token (i.e., after we see `--not`), skip it
                    if skip_next:
                        skip_next = False
                        continue
                    
                    if token.lower() == "--not":
                        # The next token should be the term to exclude
                        if i+1 < len(tokens):
                            excludes.append(tokens[i+1])
                            skip_next = True
                    else:
                        included_tokens.append(token)

                # The main search string becomes what’s left (joined).
                # This is flexible. You can choose to do more advanced logic
                # such as "AND" logic for multiple tokens, etc.
                # For simplicity, we'll treat the remainder as one big string.
                included_str = " ".join(included_tokens)
                
                # 2) We pass that to our updated search_all_prompts
                discovered_prompts = UserHistory.search_all_prompts(
                    search_term=included_str,
                    excludes=excludes
                )
            filtered_prompts = []
            for prompt in discovered_prompts:
                # strip outer whitespace:
                prompt = prompt[0].strip()
                # remove the 2nd half of the string after any --commands if they exist
                if '--' in prompt:
                    try:
                        prompt = prompt.split('--')[0]
                    except:
                        pass
                # check if the lowercase version is in a lowercase version if the filtered_prompts already and skip it
                if prompt.lower() in [x.lower() for x in filtered_prompts]:
                    # this performs like shit for huge sets but we don't have them that large yet, so YOLO(n).
                    continue
                # add to list
                filtered_prompts.append(prompt)
            # Shuffle or do anything else with discovered_prompts as you like:
            import random
            random.shuffle(filtered_prompts)

            if not filtered_prompts:
                return await ctx.send(
                    f"{ctx.author.mention} I couldn't find any prompts matching your query."
                )
            
            found_string = f"{len(filtered_prompts)} prompt{'s' if len(filtered_prompts)>1 else ''}"
            output_string = (
                f"{ctx.author.mention} I found {found_string} matching your search: `{search_string}`"
            )

            # Print up to 10 results
            for prompt in filtered_prompts[:10]:
                # prompt is a tuple (UserHistory.prompt,) so index [0].
                output_string += f"\n- `{prompt}`"

            await DiscordBot.send_large_message(ctx=ctx, text=output_string)

        except Exception as e:
            logger.error("Caught error when searching prompts: " + str(e))
            await ctx.send(
                f"{ctx.author.mention} Search is not currently available, please try again later."
            )

    @commands.command(name='random', help='Search for random prompts. Returns up to 10 prompts, unless a number is provided.')
    async def random_prompts(self, ctx, count: int = 10, raw: str = None):
        try:
            app = AppConfig.flask
            with app.app_context():
                discovered_prompts = UserHistory.get_all_prompts()
                # Shuffle the list:
                import random
                random.shuffle(discovered_prompts)
                logger.info(f"Discovered prompts: {discovered_prompts}")
                if not discovered_prompts:
                    # We didn't discover any prompts. Let the user know their search was bonkers.
                    return await ctx.send(
                        f"{ctx.author.mention} I couldn't find any prompts. Drat!"
                    )
                found_string = "a prompt"
                if len(discovered_prompts) > 1:
                    found_string = f"{len(discovered_prompts)} prompts"
                if raw is None:
                    output_string = f"{ctx.author.mention} I found {found_string} for you:"
                else:
                    output_string = ""
                for prompt in discovered_prompts[:int(count)]:
                    if raw is not None:
                        output_string = f"{output_string}\n{prompt[0]}"
                    else:
                        output_string = f"{output_string}\n- `{prompt[0]}`"
                await DiscordBot.send_large_message(ctx=ctx, text=output_string)
        except Exception as e:
            logger.error("Caught error when searching prompts: " + str(e))
            await ctx.send(
                f"{ctx.author.mention} Search is not currently available at this time, try again later."
            )

    @commands.command(name="invite", help="Invites the user to the latest thread in the channel.")
    async def invite_to_thread(self, ctx):
        if guild_config.is_channel_banned(ctx.guild.id, ctx.channel.id):
            return
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
            logger.error("Caught error when inviting to thread: " + str(e))
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
                        logger.warning(f"Had trouble adding the user history entry: {e}")
                logger.info("Worker selected for job: " + str(worker.worker_id))
                # Add it to the queue
                await discord.queue_manager.enqueue_job(worker, job)
            except Exception as e:
                await ctx.send(
                    f"Error generating image: {e}\n\nStack trace:\n{await clean_traceback(traceback.format_exc())}"
                )