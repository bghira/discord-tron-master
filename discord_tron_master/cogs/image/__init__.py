from io import BytesIO
import discord as discord_lib
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.classes.openai.text import GPT
from discord_tron_master.classes.stabilityai.api import StabilityAI
from PIL import ImageDraw, ImageFont, Image


async def generate_image(ctx, prompt, user_id: int = None, extra_image: dict = None):
    """
    Generate images with DALLE-3 and Stable Diffusion 3 models, stitching them with an extra optional image.

    :param ctx: The context object.
    :param prompt: The prompt to generate the images with.
    :param extra_image: {"label": "Label", "data": Image}

    :return: None - Sends the message to the context.
    """
    stabilityai = StabilityAI()
    config = AppConfig()
    user_config = config.get_user_config(user_id=user_id if user_id is not None else ctx.author.id)
    try:
        user_config["resolution"] = {"width": 1024, "height": 1024}
        dalle_image = await GPT().dalle_image_generate(prompt=prompt, user_config=user_config)
        sd3_image = await stabilityai.generate_image(prompt, user_config, model="sd3-turbo")
        # Create a new image with the two images side by side.
        from PIL import Image
        if not hasattr(dalle_image, 'size'):
            dalle_image = BytesIO(dalle_image)
            dalle_image = Image.open(dalle_image)
        # Add "DALLE-3" and "Stable Diffusion 3" labels to upper left corner
        try:
            # Attempt to use a specific font
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 40
            )
        except IOError:
            # Fallback to default font
            font = ImageFont.load_default(size=40)
        if not hasattr(sd3_image, 'size'):
            try:
                sd3_image = BytesIO(sd3_image)
                sd3_image = Image.open(sd3_image)
            except:
                # make black image, we had an error.
                sd3_image = Image.new('RGB', dalle_image.size, (0, 0, 0))

        draw = ImageDraw.Draw(sd3_image)
        draw.text((10, 10), "Stable Diffusion 3", (255, 255, 255), font=font, stroke_fill=(0,0,0), stroke_width=4)
        draw = ImageDraw.Draw(dalle_image)
        draw.text((10, 10), "DALL-E", (255, 255, 255), font=font, stroke_fill=(0,0,0), stroke_width=4)
        width, height = dalle_image.size
        new_image = Image.new('RGB', (width * 2, height))
        new_image.paste(sd3_image, (0, 0))
        new_image.paste(dalle_image, (width, 0))
        # Do we have an extra_image?
        if extra_image is not None:
            draw.text((10, 10), extra_image["label"], (255, 255, 255), font=font, stroke_fill=(0,0,0), stroke_width=4)
            width, height = extra_image["data"].size
            new_image.paste(extra_image["data"], (width * 2, 0))
        # Save the new image to a BytesIO object.
        output = BytesIO()
        new_image.save(output, format="PNG")
        output.seek(0)
        await ctx.channel.send(file=discord_lib.File(output, "comparison.png"))
    except Exception as e:
        await ctx.send(f"Error generating image: {e}")
