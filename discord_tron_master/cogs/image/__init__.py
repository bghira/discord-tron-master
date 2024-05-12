from io import BytesIO
import discord as discord_lib
import requests
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.classes.openai.text import GPT
from discord_tron_master.classes.stabilityai.api import StabilityAI
from PIL import ImageDraw, ImageFont, Image

def generate_lumina_image(prompt: str, use_5b: bool = False):
    from gradio_client import Client

    client_url = "http://106.14.2.150:10022/"
    if use_5b:
        client_url = "http://106.14.2.150:10020/"
    client = Client(client_url)
    result = client.predict(
            param_0=prompt,
            param_1="1024x1024",
            param_2=20,
            param_3=4,
            param_4="euler",
            param_5=6,
            param_6=1,
            param_7=True,
            param_8=True,
            api_name="/on_submit"
    )
    split_pieces = result.split('/')
    return f"{client_url}file=/tmp/gradio/{split_pieces[-2]}/image.png"

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

        # draw = ImageDraw.Draw(sd3_image)
        # draw.text((10, 10), "Stable Diffusion 3", (255, 255, 255), font=font, stroke_fill=(0,0,0), stroke_width=4)
        # Retrieve https://pollinations.ai/prompt/{prompt}?seed={seed}&width={user_config['resolution']['width']}&height={user_config['resolution']['height']}
        # pollinations_image = Image.open(BytesIO(requests.get(f"https://pollinations.ai/prompt/{prompt}?seed={user_config['seed']}&width={user_config['resolution']['width']}&height={user_config['resolution']['height']}").content))
        pollinations_image = Image.open(BytesIO(requests.get(generate_lumina_image(prompt)).content))
        extra_image = {
            "label": "LuminaT2I 5B",
            "data": Image.open(BytesIO(requests.get(generate_lumina_image(prompt, use_5b=True)).content))
        }
        draw = ImageDraw.Draw(pollinations_image)
        draw.text((10, 10), "LuminaT2I 2B", (255, 255, 255), font=font, stroke_fill=(0,0,0), stroke_width=4)

        draw = ImageDraw.Draw(dalle_image)
        draw.text((10, 10), "DALL-E", (255, 255, 255), font=font, stroke_fill=(0,0,0), stroke_width=4)
        width, height = dalle_image.size
        new_width = width * 2
        if extra_image is not None:
            extra_image_vertical_position = int((height - extra_image["data"].size[1]) / 2)
            extra_image_position = (new_width, extra_image_vertical_position)
            new_width = new_width + extra_image["data"].size[0]
        new_image = Image.new('RGB', (new_width, height))
        new_image.paste(pollinations_image, (0, 0))
        new_image.paste(dalle_image, (width, 0))
        # Do we have an extra_image?
        if extra_image is not None:
            draw = ImageDraw.Draw(extra_image["data"])
            draw.text((10, 10), extra_image["label"], (255, 255, 255), font=font, stroke_fill=(0,0,0), stroke_width=4)
            width, height = extra_image["data"].size
            new_image.paste(extra_image["data"], extra_image_position)
        # Save the new image to a BytesIO object.
        output = BytesIO()
        new_image.save(output, format="PNG")
        output.seek(0)
        if hasattr(ctx, 'channel'):
            await ctx.channel.send(file=discord_lib.File(output, "comparison.png"))
        else:
            await ctx.send(file=discord_lib.File(output, "comparison.png"))
    except Exception as e:
        await ctx.send(f"Error generating image: {e}")
