from discord.ext import commands
from asyncio import Lock
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.models.transformers import Transformers
from discord_tron_master.classes.resolution import ResolutionHelper
from discord_tron_master.classes.text_replies import return_random as random_fact
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
            if same_type is False:
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
        strength = self.config.get_user_setting(user_id, "strength")
        sag_scale = self.config.get_user_setting(user_id, "sag_scale")
        guidance_scaling = self.config.get_user_setting(user_id, "guidance_scaling")
        enable_sag = self.config.get_user_setting(user_id, "enable_sag")
        seed = self.config.get_user_setting(user_id, "seed", None)
        gpt_role = self.config.get_user_setting(user_id, "gpt_role")
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
            f"🟠 **Seed**: `{seed}` **Default**: `None`\n❓ When None, it defaults to the current timestamp at the time of image generation. Can be used to reproduce images.\n"
            f"🟠 **Steps**: `{steps}` **Default**: `100`\n❓ This represents how many denoising iterations the model will do on your image. Less is more.\n"
            f"🟠 **Scaling**: guidance: `{guidance_scaling}` **Default**: `7.5`, **SAG**: {sag_scale} **Default**: `0.75`\n❓ How closely the image follows the prompt. Below 1 = no prompts.\n"
            f"🟠 **Strength**: `{strength}` **Default**: `0.5`\n❓ The higher the strength, the more random the img2img becomes. Lower values become more deterministic.\n"
            f"🟠 **Self-Assisted Guidance (SAG)**: `{enable_sag}` **Default**: `False`\n❓ Use SAG scaling to make higher quality images. Requires a square aspect ratio on non-SAG models.\n"
            f"🟠 **Negative Prompt:**:\n➡️    `{negative_prompt}`\n❓ Images featuring these keywords are less likely to be generated. Set via `{self.config.get_command_prefix()}negative`.\n"
            f"🟠 **Positive Prompt:**:\n➡️    `{positive_prompt}`\n❓ Added to the end of every prompt, which has a limit of 77 tokens. This can become truncated. Set via `{self.config.get_command_prefix()}positive`.\n"
            f"🟠 **GPT Role:**:\n➡️    `{gpt_role}`\n❓ Defines how this bot will respond to you when chatting. Use `{self.config.get_command_prefix()}settings gpt_role [new role]`.\n"
            f"🟠 **Resolution:** `{resolution['width']}x{resolution['height']}`\n❓ Lower resolutions render more quickly, and has a relationship with `steps` that can really influence the output. See **{self.config.get_command_prefix()}help resolution** for more information."
        )
        if hasattr(ctx, "message"):
            await ctx.message.delete()
        elif hasattr(ctx, "delete"):
            await ctx.delete()
        await self.send_large_message(ctx, message)

    @commands.command(name="sag", help="Enable or disable self-assisted guidance pipeline that uses a self-reference routine to improve image quality. Default is True.")
    async def toggle_sag(self, ctx):
        user_id = ctx.author.id
        enable_sag = config.get_user_setting(user_id, "enable_sag")
        try:
            if enable_sag:
                config.set_user_setting(user_id, "enable_sag", False)
                response = await ctx.send(
                    f"{ctx.author.mention} Self-assisted guidance has been disabled. Did you know {random_fact()}?"
                )
            else:
                config.set_user_setting(user_id, "enable_sag", True)
                response = await ctx.send(
                    f"{ctx.author.mention} Self-assisted guidance has been enabled. You're welcome."
                )
            await ctx.delete(delay=15)
            await response.delete(delay=15)
        except Exception as e:
            logging.error("Caught error when toggling user SAG property: " + str(e))

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
        if "none" in seed.lower() or seed == "random":
            seed = None
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
    async def send_large_message(self, ctx, text, max_chars=2000):
        if len(text) <= max_chars:
            await ctx.channel.send(text)
            return

        lines = text.split("\n")
        buffer = ""
        for line in lines:
            if len(buffer) + len(line) + 1 > max_chars:
                await ctx.channel.send(buffer)
                buffer = ""
            buffer += line + "\n"
        if buffer:
            await ctx.channel.send(buffer)
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