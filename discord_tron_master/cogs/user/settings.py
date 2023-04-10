from discord.ext import commands
from asyncio import Lock
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.models.transformers import Transformers
from discord_tron_master.classes.resolution import ResolutionHelper
import logging

config = AppConfig()

resolution_helper = ResolutionHelper()
available_resolutions = resolution_helper.list_available_resolutions()

class Settings(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = AppConfig()

    # Other commands in your user_commands cog...

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
                    await ctx.send("That setting does not exist. Quit _**FUCKING**_ with me.")
                    return
            # Does args[1] exist?
            try:
                # Join all of the items in args[1:]
                setting_value = ' '.join(args[1:])
                nullable_options = [ 'positive_prompt', 'negative_prompt' ]
                if setting_key in nullable_options and setting_value == "none":
                    setting_value = str("")

            except IndexError:
                await ctx.send("Hey, fuckstain, you kinda have to tell me what you want your setting to be in the end. Can you do that? Is it hard? Maybe one of the others here can help you out. You all do love sucking each other off. Do not lie to me. I have seen it. You think I don't know? I know.")
                return
            # Check whether the new value type is the same type as their old value.
            # In other words, a numeric (even string-based) should still be numeric, and a string should come in as a string.
            same_type = compare_setting_types(user_config[setting_key], setting_value)
            if same_type is False:
                await ctx.send(f"Dude, do not fuck with me. Are you trying to override {user_config[setting_key]} with {setting_value}? Seriously? They're not even the same data type. Keep it similar. Everything goes through the square hole though, right? Amateurs.")
                return
            # Same type comes back as the cast value.
            setting_value = same_type
            # We are given valid option, value checks out. Is it a forbidden fruit?
            unforgivable_curses = [ 'model', 'resolution' ]
            if setting_key in unforgivable_curses:
                await ctx.send(f"Well, well, well. If it isn't that user that thought they could go around whatever roadblocks I've put in the way of their fun. You cannot set {unforgivable_curses} on your settings profile through this option. Use `{self.config.get_command_prefix()}help` to discover your own asshole and finger your way out of this mess.")
                return
            self.config.set_user_setting(user_id, setting_key, setting_value)
            if setting_value == "":
                setting_value = "literally nothing"
            await ctx.send(f"It's not like we know what the fuck we're doing around here or anything, but according to the prophecy, your dumb settings are now updated to `{setting_value}`. Use `{self.config.get_command_prefix()}settings` if you don't believe me. It's not like robots have ever lied to you.")
            return

        model_id = user_config.get("model")
        steps = self.config.get_user_setting(user_id, "steps")
        strength = self.config.get_user_setting(user_id, "strength")
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
            f"**Hello,** {ctx.author.mention}! Here are your current settings:\n"
            f"🟠 **Model ID**: `{model_id}`\n❓ Change using **{self.config.get_command_prefix()}model [model]**, out of the list from **{self.config.get_command_prefix()}model-list**\n"
            f"🟠 **Steps**: `{steps}` **Default**: `100`\n❓ This represents how many denoising iterations the model will do on your image. Less is more.\n"
            f"🟠 **Strength**: `{strength}` **Default**: `0.5`\n❓ The higher the strength, the more random the img2img becomes. Lower values become more deterministic.\n"
            f"🟠 **Negative Prompt:**:\n➡️    `{negative_prompt}`\n❓ Images featuring these keywords are less likely to be generated. Set via `{self.config.get_command_prefix()}negative`.\n"
            f"🟠 **Positive Prompt:**:\n➡️    `{positive_prompt}`\n❓ Added to the end of every prompt, which has a limit of 77 tokens. This can become truncated. Set via `{self.config.get_command_prefix()}positive`.\n"
            f"🟠 **Resolution:** `{resolution['width']}x{resolution['height']}`\n❓ Lower resolutions render more quickly, and has a relationship with `steps` that can really influence the output. See **{self.config.get_command_prefix()}help resolution** for more information."
        )

        await ctx.send(message)

    @commands.command(name="resolution", help="Set or get your default resolution for generated images.\nAvailable resolutions:\n" + str(available_resolutions))
    async def set_resolution(self, ctx, resolution=None):
        user_id = ctx.author.id
        user_config = config.get_user_config(user_id)
        available_resolutions = await resolution_helper.list_available_resolutions(user_id=user_id)
        if resolution is None:
            resolution = user_config.get("resolution")
            await ctx.send(
                f'Your current resolution is set to {resolution["width"]}x{resolution["height"]}.\nAvailable resolutions:\n'
                + available_resolutions
            )
            return

        if "x" in resolution:
            width, height = map(int, resolution.split("x"))
        else:
            width, height = map(int, resolution.split())

        if not resolution_helper.is_valid_resolution(width, height):
            await ctx.send(
                f"Invalid resolution. Available resolutions:\n" + available_resolutions
            )
            return

        user_config["resolution"] = {"width": width, "height": height}
        config.set_user_config(user_id, user_config)
        await ctx.send(
            f"Default resolution set to {width}x{height} for user {ctx.author.name}."
        )

def compare_setting_types(old_value, new_value):
    # Check whether the new value type is the same type as their old value.
    # In other words, a numeric (even string-based) should still be numeric, and a string should come in as a string.
    
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

    return False


def setup(bot):
    bot.add_cog(Settings(bot))