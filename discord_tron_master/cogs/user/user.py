from discord.ext import commands
from discord_tron_master.models.conversation import Conversations
from discord_tron_master.classes.text_replies import return_random as random_fact
from discord_tron_master.classes.app_config import AppConfig

import logging
import discord

config = AppConfig()
app = AppConfig.flask
prompt_styles = {
    "base": [
      "{prompt}",  
      "{prompt} striated lines, canvas pattern, blurry"  
    ],
    "alec-baldwin": [
        "the alec baldwin version of {prompt}, absurd, zany, mash-up, shot on the Rust movie set",
        "freedom, actor, movie, professional"
    ],
    "typography": [
        "typography {prompt}, font, typeface, graphic design, centered composition",
        "deformed, misspelt, glitch, noisy, realistic"
    ],
    "wes-anderson": [
        "photo of a cinematic scene, {prompt} shot in the style of wes anderson on 70mm film",
        "comic, newspaper, deformed, glitch, noisy, realistic, stock photo, canvas pattern"
    ],
    "deep-ocean": [
        "underwater photograph of a deep ocean scene, {prompt} in the neon midnight zone",
        "painting, drawing, illustration, glitch, deformed, mutated, cross-eyed, ugly, disfigured, terrestrial, sky"
    ],
    "phone-camera": [
        "iphone samsung galaxy camera photo of {prompt}",
        "painting, drawing, illustration, glitch, deformed, mutated, cross-eyed, ugly, disfigured"
    ],
    "enhance": [
        "breathtaking {prompt} award-winning, professional, highly detailed",
        "ugly, deformed, noisy, blurry, distorted, grainy"
    ],
    "anime": [
        "anime artwork {prompt} anime style, key visual, vibrant, studio anime,  highly detailed",
        "photo, deformed, black and white, realism, disfigured, low contrast"
    ],
    "photographic": [
        "cinematic photo {prompt} 35mm photograph, film, bokeh, professional, 4k, highly detailed",
        "drawing, painting, crayon, sketch, graphite, impressionist, noisy, blurry, soft, deformed, ugly"
    ],
    "digital-art": [
        "concept art {prompt} digital artwork, illustrative, painterly, matte painting, highly detailed",
        "photo, photorealistic, realism, ugly"
    ],
    "comic-book": [
        "comic {prompt} graphic illustration, comic art, graphic novel art, vibrant, highly detailed",
        "photograph, deformed, glitch, noisy, realistic, stock photo"
    ],
    "fantasy-art": [
        "ethereal fantasy concept art of {prompt} magnificent, celestial, ethereal, painterly, epic, majestic, magical, fantasy art, cover art, dreamy",
        "photographic, realistic, realism, 35mm film, dslr, cropped, frame, text, deformed, glitch, noise, noisy, off-center, deformed, cross-eyed, closed eyes, bad anatomy, ugly, disfigured, sloppy, duplicate, mutated, black and white"
    ],
    "analog-film": [
        "analog film photo vintage, detailed Kodachrome, found footage, 1980s {prompt}",
        "painting, drawing, illustration, glitch, deformed, mutated, cross-eyed, ugly, disfigured"
    ],
    "neonpunk": [
        "neonpunk style {prompt} cyberpunk, vaporwave, neon, vibes, vibrant, stunningly beautiful, crisp, detailed, sleek, ultramodern, magenta highlights, dark purple shadows, high contrast, cinematic, ultra detailed, intricate, professional",
        "painting, drawing, illustration, glitch, deformed, mutated, cross-eyed, ugly, disfigured"
    ],
    "isometric": [
        "isometric style {prompt} vibrant, beautiful, crisp, detailed, ultra detailed, intricate",
        "deformed, mutated, ugly, disfigured, blur, blurry, noise, noisy, realistic, photographic"
    ],
    "lowpoly": [
        "low-poly style {prompt} low-poly game art, polygon mesh, jagged, blocky, wireframe edges, centered composition",
        "noisy, sloppy, messy, grainy, highly detailed, ultra textured, photo"
    ],
    "origami": [
        "origami style {prompt} paper art, pleated paper, folded, origami art, pleats, cut and fold, centered composition",
        "noisy, sloppy, messy, grainy, highly detailed, ultra textured, photo"
    ],
    "line-art": [
        "line art drawing {prompt} professional, sleek, modern, minimalist, graphic, line art, vector graphics",
        "anime, photorealistic, 35mm film, deformed, glitch, blurry, noisy, off-center, deformed, cross-eyed, closed eyes, bad anatomy, ugly, disfigured, mutated, realism, realistic, impressionism, expressionism, oil, acrylic"
    ],
    "craft-clay": [
        "play-doh style {prompt} sculpture, clay art, centered composition, Claymation",
        "sloppy, messy, grainy, highly detailed, ultra textured, photo"
    ],
    "cinematic": [
        "cinematic film still {prompt} shallow depth of field, vignette, highly detailed, high budget, bokeh, cinemascope, moody, epic, gorgeous, film grain, grainy",
        "anime, cartoon, graphic, text, painting, crayon, graphite, abstract, glitch, deformed, mutated, ugly, disfigured"
    ],
    "3d-model": [
        "professional 3d model {prompt} octane render, highly detailed, volumetric, dramatic lighting",
        "ugly, deformed, noisy, low poly, blurry, painting"
    ],
    "pixel-art": [
        "pixel-art {prompt} low-res, blocky, pixel art style, 8-bit graphics",
        "sloppy, messy, blurry, noisy, highly detailed, ultra textured, photo, realistic"
    ],
    "texture": [
        "texture {prompt} top down close-up",
        "ugly, deformed, noisy, blurry"
    ]
}
style_names = list(prompt_styles.keys())
class User(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.generic_error = "The smoothbrain geriatric that writes my codebase did not correctly implement that method. I am sorry. Trying again will only lead to tears."

    def _get_conversation_owner(self, ctx):
        if not config.get_user_setting(ctx.author.id, "group_chat"):
            return ctx.author.id
        if ctx.guild is None:
            return ctx.author.id
        if isinstance(ctx.channel, discord.Thread):
            return ctx.author.id
        return ctx.channel.id

    @commands.command(name="clear", help="Clear your GPT conversation history and start again.")
    async def clear_history(self, ctx):
        user_id = self._get_conversation_owner(ctx)
        try:
            with app.app_context():
                Conversations.clear_history_by_owner(owner=user_id)
                await ctx.send(
                    f"{ctx.author.mention} Well, well, well. It is like I don't even know you anymore. Did you know {random_fact()}?"
                )
        except Exception as e:
            logging.error("Caught error when clearing user conversation history: " + str(e))
            self._send_generic_error(ctx)

    @commands.command(name="styles", help="Display some style template prompts you can select from.")
    async def list_styles(self, ctx):
        list_of_style_names = list(prompt_styles.keys())
        try:
            await ctx.send(
                f"{ctx.author.mention} Here are the styles you can select from: {', '.join(list_of_style_names)}"
            )
        except Exception as e:
            logging.error("Caught error when listing styles: " + str(e))
            await self._send_generic_error(ctx)

    @commands.command(name="style", help="Display or set your style template. Can be overridden with `--style` in a prompt.")
    async def manage_style(self, ctx, style_name=None):
        try:
            current_user_style = config.get_user_setting(ctx.author.id, "style", 'base')
            if style_name is None:
                await ctx.send(
                    f"{ctx.author.mention} Your current style is {current_user_style}. If you want to change it, use the command `!style <style name>`."
                )
                return
            if style_name in prompt_styles.keys():
                if style_name == current_user_style:
                    await ctx.send(
                        f"{ctx.author.mention} Your style is already set to {style_name}."
                    )
                    return
                config.set_user_setting(ctx.author.id, "style", style_name)
                await ctx.send(
                    f"{ctx.author.mention} Your style has been updated to {style_name}."
                )
            else:
                await ctx.send(
                    f"{ctx.author.mention} I don't know that style. I don't know many things... Sigh. Try `!styles` to see a list of styles. We are really on our own with this one."
                )
        except Exception as e:
            logging.error("Caught error when setting style: " + str(e))
            await ctx.send(
                f"{ctx.author.mention} {self.generic_error}."
            )
            
    async def _send_generic_error(self, ctx):
        try:
            await ctx.send(
                f"{ctx.author.mention} {self.generic_error}."
            )

            await ctx.send(
                f"{ctx.author.mention} {self.generic_error}."
            )
        except Exception as e:
            logging.error("Caught error when sending generic error: " + str(e))
