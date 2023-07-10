from discord.ext import commands
from asyncio import Lock
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.classes.guilds import Guilds as GuildConfig
from discord_tron_master.models.transformers import Transformers
from discord_tron_master.classes.resolution import ResolutionHelper
from discord_tron_master.classes.text_replies import return_random as random_fact
import logging
from discord_tron_master.bot import DiscordBot

config = AppConfig()
guild_config = GuildConfig()
resolution_helper = ResolutionHelper()
available_resolutions = resolution_helper.list_available_resolutions()

class Settings(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = AppConfig()

    # Other commands in your user_commands cog...
    @commands.command(name="home", help="Sets the home guild for this bot.  This is where the bot will have warm and fuzzy feelings.", hidden=True)
    async def home_guild(self, ctx):
        if not guild_config.is_guild_home_defined():
            guild_config.set_guild_home(ctx.guild.id)
            await ctx.send(f"Home guild set to {ctx.guild.name} ({ctx.guild.id}).")
        else:
            await ctx.send(f"Are you fucking lost?")

    @commands.command(name="settings", help="Shows your current settings.", hidden=False)
    async def my_settings(self, ctx, *args):
        user_id = ctx.author.id
        user_config = self.config.get_user_config(user_id=user_id)
        if args:
            logging.info(f"Here are the args: {args}")
            setting_key = args[0]
            if setting_key not in user_config:
                mappable_options = { "positive": "positive_prompt", "negative": "negative_prompt" }
                if setting_key in mappable_options:
                    setting_key = mappable_options[setting_key]
                else:
                    await ctx.send(f"That setting does not exist.  Did you know {random_fact()}?")
                    return
            # Does args[1] exist?
            try:
                # Join all of the items in args[1:]
                setting_value = ' '.join(args[1:])
                nullable_options = [ 'positive_prompt', 'negative_prompt' ]
                if setting_key in nullable_options and setting_value == "none":
                    setting_value = str("")

            except IndexError:
                await ctx.send(f"You did not provide a setting for me to update.  Did you know {random_fact()}?")
                return
            # Check whether the new value type is the same type as their old value.
            # In other words, a numeric (even string-based) should still be numeric, and a string should come in as a string.
            same_type = compare_setting_types(user_config[setting_key], setting_value)
            if same_type is None:
                await ctx.send(f"Dude, do not fuck with me. Are you trying to override {user_config[setting_key]} with {setting_value}? Seriously? They're not even the same data type. Keep it similar. Everything goes through the square hole though, right? Amateurs.")
                return
            # Same type comes back as the cast value.
            setting_value = same_type
            # We are given valid option, value checks out. Is it a forbidden fruit?
            unforgivable_curses = [ 'model', 'resolution', 'seed' ]
            if setting_key in unforgivable_curses:
                await ctx.send(f"Well, well, well. If it isn't that user that thought they could go around whatever roadblocks I've put in the way of their fun. You cannot set {unforgivable_curses} on your settings profile through this option. Use `{self.config.get_command_prefix()}help` to discover your own asshole and finger your way out of this mess.")
                return
            self.config.set_user_setting(user_id, setting_key, setting_value)
            if setting_value == "":
                setting_value = "literally nothing"
            await ctx.send(f"{ctx.author.mention} your setting, `{setting_key}` has been updated to `{setting_value}`.  Did you know {random_fact()}?")
            return

        model_id = user_config.get("model")
        steps = self.config.get_user_setting(user_id, "steps")
        tts_voice = self.config.get_user_setting(user_id, "tts_voice")
        strength = self.config.get_user_setting(user_id, "strength")
        guidance_scaling = self.config.get_user_setting(user_id, "guidance_scaling")

        seed = self.config.get_user_setting(user_id, "seed", None)
        if seed == -1:
            seed = "random"
        elif seed == 0:
            seed = None

        gpt_role = self.config.get_user_setting(user_id, "gpt_role")
        temperature = self.config.get_user_setting(user_id, "temperature")
        max_tokens = self.config.get_user_setting(user_id, "max_tokens")
        repeat_penalty = self.config.get_user_setting(user_id, "repeat_penalty")
        top_p = self.config.get_user_setting(user_id, "top_p")
        top_k = self.config.get_user_setting(user_id, "top_k")
        top_p = self.config.get_user_setting(user_id, "top_p")

        negative_prompt = self.config.get_user_setting(
            user_id,
            "negative_prompt",
            "(child, baby, deformed, distorted, disfigured:1.3), poorly drawn, bad anatomy, wrong anatomy, extra limb, missing limb, floating limbs, (mutated hands and fingers:1.4), disconnected limbs, mutation, mutated, ugly, disgusting, blurry, amputation",
        )
        positive_prompt = self.config.get_user_setting(
            user_id, "positive_prompt"
        )
        resolution = self.config.get_user_setting(
            user_id, "resolution"
        )
        if positive_prompt == "":
            positive_prompt = "literally nothing. fly free, birdie."
        if negative_prompt == "":
            negative_prompt = "literally nothing. live dangerously, bucko."

        message = (
            f"{ctx.author.mention}\n"
            f"🟠 **Model ID**: `{model_id}`\n❓ Change using **{self.config.get_command_prefix()}model [model]**, out of the list from **{self.config.get_command_prefix()}model-list**\n"
            f"🟠 **Seed**: `{seed}` **Default**: `None`\n❓ None sets it to the current timestamp, 'random' or -1 set it to a more random value. Applies to all generation (img, txt).\n"
            f"🟠 **Resolution:** `{resolution['width']}x{resolution['height']}`\n"
            f"🟠 **Steps**: `{steps}` **Default**: `100`\n❓ About 20 to 200 steps will produce good images.\n"
            f"🟠 **Scaling**: guidance: `{guidance_scaling}` **Default**: `7.5`\n❓ How closely the image follows the prompt. Below 1 = no prompts apply.\n"
            f"🟠 **Strength**: `{strength}` **Default**: `0.5`\n❓ Higher values make the img2img more random. Lower values make it deterministic.\n"
            f"🟠 **Negative Prompt:**:\n➡️    `{negative_prompt}`\n❓ Images featuring these keywords are less likely to be generated. Set via `{self.config.get_command_prefix()}settings negative`.\n"
            f"🟠 **Positive Prompt:**:\n➡️    `{positive_prompt}`\n❓ Added to the end of each image prompt. Set via `{self.config.get_command_prefix()}settings positive`.\n"
            f"🟠 **GPT Role:**:\n➡️    `{gpt_role}`\n❓ Set a bot persona. Use `{self.config.get_command_prefix()}settings gpt_role [new role]`.\n"
            f"🟠 **TTS Voice:**:\n➡️  `{tts_voice}`\n❓ `!tts` voice. Use `{self.config.get_command_prefix()}tts-voices` and `{self.config.get_command_prefix()}tts-voice [new voice]`.\n"
            f"🟠 **Temperature**: `{temperature}` **Default**: `1.0`\n❓ The higher the temperature, the more random the txt2txt becomes. Lower values become more deterministic.\n"
            f"🟠 **Repeat penalty**: `{repeat_penalty}` **Default**: `1.1`\n❓ Penalize repeating tokens during text generation. Encourages diverse responses.\n"
            f"🟠 **Max tokens**: `{max_tokens}` **Default**: `2048`\n❓ How many tokens to limit LLM output to. Encourages quicker replies.\n"
            f"🟠 **top_k**: `{top_k}` **Default**: `40`\n❓ Sampling a greater number of possible tokens slows down output while possibly improving the quality, eg. 10 is faster than 40.\n"
            f"🟠 **top_p**: `{top_p}` **Default**: `0.95`\n❓ Can be used to tune the speed vs quality of text generation. Ask GPT to explain this parameter.\n"
        )
        if hasattr(ctx, "message"):
            try:
                await ctx.message.delete()
            except:
                logging.warning(f"Could not delete message, it was likely deleted by another worker or a moderator.")
        elif hasattr(ctx, "delete"):
            await ctx.delete()
        await DiscordBot.send_large_message(ctx, message)
    @commands.command(name="defaults", help="Set defaults for all users (admin only).", hidden=False)
    async def default_settings(self, ctx, *args):
        user_id = ctx.author.id
        user_config = self.config.get_user_config(user_id=user_id)
        default_config = self.config.get_user_config(user_id='default')
        if args:
            setting_key = args[0]
            setting_value = " ".join(args[1:])
            self.config.set_user_setting('default', setting_key, setting_value)
            if setting_value == "":
                setting_value = "literally nothing"
            await ctx.send(f"{ctx.author.mention} the default user setting, `{setting_key}` has been updated to `{setting_value}`.  Did you know {random_fact()}?")
            return

        user_id = 'default'
        model_id = self.config.get_user_setting(user_id, "models")
        steps = self.config.get_user_setting(user_id, "steps")
        tts_voice = self.config.get_user_setting(user_id, "tts_voice")
        strength = self.config.get_user_setting(user_id, "strength")
        guidance_scaling = self.config.get_user_setting(user_id, "guidance_scaling")

        seed = self.config.get_user_setting(user_id, "seed", None)
        if seed == -1:
            seed = "random"
        elif seed == 0:
            seed = None

        gpt_role = self.config.get_user_setting(user_id, "gpt_role")
        temperature = self.config.get_user_setting(user_id, "temperature")
        max_tokens = self.config.get_user_setting(user_id, "max_tokens")
        repeat_penalty = self.config.get_user_setting(user_id, "repeat_penalty")
        top_p = self.config.get_user_setting(user_id, "top_p")
        top_k = self.config.get_user_setting(user_id, "top_k")
        top_p = self.config.get_user_setting(user_id, "top_p")

        negative_prompt = self.config.get_user_setting(
            user_id,
            "negative_prompt",
            "(child, baby, deformed, distorted, disfigured:1.3), poorly drawn, bad anatomy, wrong anatomy, extra limb, missing limb, floating limbs, (mutated hands and fingers:1.4), disconnected limbs, mutation, mutated, ugly, disgusting, blurry, amputation",
        )
        positive_prompt = self.config.get_user_setting(
            user_id, "positive_prompt"
        )
        resolution = self.config.get_user_setting(
            user_id, "resolution"
        )
        if positive_prompt == "":
            positive_prompt = "literally nothing. fly free, birdie."
        if negative_prompt == "":
            negative_prompt = "literally nothing. live dangerously, bucko."

        message = (
            f"{ctx.author.mention}\n"
            f"🟠 **Model ID**: `{model_id}`\n❓ Change using **{self.config.get_command_prefix()}model [model]**, out of the list from **{self.config.get_command_prefix()}model-list**\n"
            f"🟠 **Seed**: `{seed}` **Default**: `None`\n❓ None sets it to the current timestamp, 'random' or -1 set it to a more random value. Applies to all generation (img, txt).\n"
            f"🟠 **Resolution:** `{resolution['width']}x{resolution['height']}`\n"
            f"🟠 **Steps**: `{steps}` **Default**: `100`\n❓ About 20 to 200 steps will produce good images.\n"
            f"🟠 **Scaling**: guidance: `{guidance_scaling}` **Default**: `7.5`\n❓ How closely the image follows the prompt. Below 1 = no prompts apply.\n"
            f"🟠 **Strength**: `{strength}` **Default**: `0.5`\n❓ Higher values make the img2img more random. Lower values make it deterministic.\n"
            f"🟠 **Negative Prompt:**:\n➡️    `{negative_prompt}`\n❓ Images featuring these keywords are less likely to be generated. Set via `{self.config.get_command_prefix()}settings negative`.\n"
            f"🟠 **Positive Prompt:**:\n➡️    `{positive_prompt}`\n❓ Added to the end of each image prompt. Set via `{self.config.get_command_prefix()}settings positive`.\n"
            f"🟠 **GPT Role:**:\n➡️    `{gpt_role}`\n❓ Set a bot persona. Use `{self.config.get_command_prefix()}settings gpt_role [new role]`.\n"
            f"🟠 **TTS Voice:**:\n➡️  `{tts_voice}`\n❓ `!tts` voice. Use `{self.config.get_command_prefix()}tts-voices` and `{self.config.get_command_prefix()}tts-voice [new voice]`.\n"
            f"🟠 **Temperature**: `{temperature}` **Default**: `1.0`\n❓ The higher the temperature, the more random the txt2txt becomes. Lower values become more deterministic.\n"
            f"🟠 **Repeat penalty**: `{repeat_penalty}` **Default**: `1.1`\n❓ Penalize repeating tokens during text generation. Encourages diverse responses.\n"
            f"🟠 **Max tokens**: `{max_tokens}` **Default**: `2048`\n❓ How many tokens to limit LLM output to. Encourages quicker replies.\n"
            f"🟠 **top_k**: `{top_k}` **Default**: `40`\n❓ Sampling a greater number of possible tokens slows down output while possibly improving the quality, eg. 10 is faster than 40.\n"
            f"🟠 **top_p**: `{top_p}` **Default**: `0.95`\n❓ Can be used to tune the speed vs quality of text generation. Ask GPT to explain this parameter.\n"
        )
        if hasattr(ctx, "message"):
            try:
                await ctx.message.delete()
            except:
                logging.warning(f"Could not delete message, it was likely deleted by another worker or a moderator.")
        elif hasattr(ctx, "delete"):
            await ctx.delete()
        await DiscordBot.send_large_message(ctx, message)

    @commands.command(name="steps", help="Set the number of steps for the image generation process. Default is 100.")
    async def set_steps(self, ctx, steps):
        user_id = ctx.author.id
        if not steps.isdigit():
            our_reply = await ctx.send(f"Steps must be a number. You gave me `{steps}`. Try again.")
            try:
                if hasattr(ctx, "message"):
                    await ctx.message.delete(delay=15)
                else:
                    await ctx.delete(delay=15)
                await our_reply.delete(delay=15)
            except:
                logging.error("Failed to delete messages.")
            return
        config.set_user_setting(user_id, "steps", int(steps))
        response = await ctx.send(
            f"{ctx.author.mention} Your steps have been updated. Thank you for flying Air Bizarre."
        )
        await response.delete(delay=15)
        if hasattr(ctx, "message"):
            await ctx.message.delete()
        else:
            logging.debug(f"Received message object for delete, we are not sure how to proceed with: {ctx}")
    @commands.command(name="guidance", help="Set your guidance scaling parameter. It defaults to 7.5.")
    async def set_guidance(self, ctx, guidance_scaling = None):
        user_id = ctx.author.id
        user_config = config.get_user_config(user_id)
        original_guidance_scaling = config.get_user_setting(user_id, "guidance_scaling")
        if guidance_scaling is not None and not "none" in guidance_scaling.lower():
            try:
                scaling_value = float(guidance_scaling)
            except:
                our_reply = await ctx.send(f"Scaling parameter must be a number. Specifically, a float value. You gave me `{guidance_scaling}`. Try again.")
                try:
                    await ctx.delete(delay=5)
                    await our_reply.delete(delay=5)
                except:
                    logging.error("Failed to delete messages.")
                return
        # Allow specifying "None", "none", "NoNe" etc on the cmdline to reset to default.
        if guidance_scaling is not None and "none" in guidance_scaling.lower():
            guidance_scaling = 7.5
        user_config["guidance_scaling"] = guidance_scaling
        config.set_user_config(user_id, user_config)
        response = await ctx.send(
            f"{ctx.author.mention} Your guidance scaling factor has been updated to '{guidance_scaling}', from '{original_guidance_scaling}'. Did you know {random_fact()}?"
        )
        await ctx.delete()
        await response.delete(delay=15)

    @commands.command(name="seed", help="Set or remove your seed value. When set to 'none' or 'random', it defaults to the current timestamp at the time of image generation. Can be used to reproduce images.")
    async def set_seed(self, ctx, seed = None):
        user_id = ctx.author.id
        user_config = config.get_user_config(user_id)
        original_seed = config.get_user_setting(user_id, "seed")
        if not seed.isdigit() and not "none" in seed.lower() and seed is not None and seed != "random":
            our_reply = await ctx.send(f"Seed must be a number. You gave me `{seed}`. Try again.")
            try:
                await ctx.delete(delay=5)
                await our_reply.delete(delay=5)
            except:
                logging.error("Failed to delete messages.")
            return
        # Allow specifying "None", "none", "NoNe" etc on the cmdline and map to None to enable random seeds.
        if "none" in seed.lower():
            seed = None
        elif seed == "random":
            seed = -1
        user_config["seed"] = seed
        config.set_user_config(user_id, user_config)
        response = await ctx.send(
            f"{ctx.author.mention} Your generation seed has been updated to '{seed}', from '{original_seed}'.  Did you know {random_fact()}?"
        )
        await response.delete(delay=15)
        if hasattr(ctx, "delete"):
            await ctx.delete()
        elif hasattr(ctx, "message") and hasattr(ctx.message, "delete"):
            await ctx.message.delete()
        else:
            logging.debug(f"Received message object for delete, we are not sure how to proceed with: {ctx}. Cannot delete.")

    @commands.command(name="resolution", help="Set or get your default resolution for generated images.\nAvailable resolutions:\n" + str(available_resolutions))
    async def set_resolution(self, ctx, resolution=None):
        user_id = ctx.author.id
        user_config = config.get_user_config(user_id)
        available_resolutions = await resolution_helper.list_available_resolutions(user_id=user_id)
        if resolution is None:
            resolution = user_config.get("resolution")
            response = await ctx.send(
                f'Your current resolution is set to {resolution["width"]}x{resolution["height"]}.\nAvailable resolutions:\n'
                + available_resolutions
            )
            return

        if "x" in resolution:
            width, height = map(int, resolution.split("x"))
        else:
            width, height = map(int, resolution.split())

        if not resolution_helper.is_valid_resolution(width, height):
            response = await ctx.send(
                f"Invalid resolution. Available resolutions:\n" + available_resolutions
            )
            return

        user_config["resolution"] = {"width": width, "height": height}
        config.set_user_config(user_id, user_config)
        response = await ctx.send(
            f"Default resolution set to {width}x{height} for user {ctx.author.name}. Did you know {random_fact()}?"
        )
        if hasattr(ctx, "message"):
            await ctx.message.delete()
        else:
            await ctx.delete()
        await response.delete(delay=15)

def compare_setting_types(old_value, new_value):
    # Check whether the new value type is the same type as their old value.
    # In other words, a numeric (even string-based) should still be numeric, and a string should come in as a string.
    if old_value is None:
        return new_value
    # Check for a bool vs a bool string
    if isinstance(old_value, bool) and isinstance(new_value, str) and new_value.lower() in ["true", "false"]:
        # Convert the string to a bool
        if new_value.lower() == "true":
            return True
        elif new_value.lower() == "false":
            return False

    # Check if both values are integers
    if isinstance(old_value, int) and new_value.isdigit():
        return int(new_value)
    
    # Check if both values are floats
    if isinstance(old_value, float):
        try:
            float_value = float(new_value)
            return float_value
        except ValueError:
            pass
    
    # Check if both values are non-numeric strings
    if isinstance(old_value, str) and isinstance(new_value, str) and not new_value.isnumeric():
        return new_value

    # Check for an empty string vs an old non-empty string. we want to allow unsetting things by making them an empty string.
    is_empty_value = new_value == "" or new_value is None
    if isinstance(old_value, str) and is_empty_value:
        logging.info("Special case for empty string.")
        return new_value

    # Log the two types we received:
    logging.error(f"compare_setting_types: old_value: {type(old_value)} new_value: {type(new_value)}")

    return None


def setup(bot):
    bot.add_cog(Settings(bot))