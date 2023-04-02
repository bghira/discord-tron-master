from discord.ext import commands
from asyncio import Lock
from discord_tron_master.classes.app_config import AppConfig

class Settings(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = AppConfig()

    # Other commands in your user_commands cog...

    @commands.command(name="settings", help="Shows your current settings.")
    async def my_settings(self, ctx):
        user_id = ctx.author.id
        user_config = self.config.get_user_config(user_id=user_id)
        model_id = user_config.get("model", "hakurei/waifu-diffusion")
        steps = self.config.get_user_setting(user_id, "steps", 50)
        negative_prompt = self.config.get_user_setting(
            user_id,
            "negative_prompt",
            "(child, baby, deformed, distorted, disfigured:1.3), poorly drawn, bad anatomy, wrong anatomy, extra limb, missing limb, floating limbs, (mutated hands and fingers:1.4), disconnected limbs, mutation, mutated, ugly, disgusting, blurry, amputation",
        )
        positive_prompt = self.config.get_user_setting(
            user_id, "positive_prompt", "beautiful hyperrealistic"
        )
        resolution = self.config.get_user_setting(
            user_id, "resolution", {"width": 800, "height": 456}
        )

        message = (
            f"**Hello,** {ctx.author.mention}! Here are your current settings:\n"
            f"🟠 **Model ID**: `{model_id}`\n❓ Change using **!setmodel [model]**, out of the list from **!listmodels**\n"
            f"🟠 **Steps**: `{steps}`\n❓ This represents how many denoising iterations the model will do on your image. Less is more.\n"
            f"🟠 **Negative Prompt:**:\n➡️    `{negative_prompt}`\n❓ Images featuring these keywords are less likely to be generated. Set via `!negative`.\n"
            f"🟠 **Positive Prompt:**:\n➡️    `{positive_prompt}`\n❓ Added to the end of every prompt, which has a limit of 77 tokens. This can become truncated. Set via `!positive`.\n"
            f"🟠 **Resolution:** `{resolution['width']}x{resolution['height']}`\n❓ Lower resolutions render more quickly, and has a relationship with `steps` that can really influence the output. See **!help resolution** for more information."
        )

        await ctx.send(message)


def setup(bot):
    bot.add_cog(UserSettings(bot))