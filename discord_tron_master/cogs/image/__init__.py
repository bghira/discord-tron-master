from io import BytesIO
import discord as discord_lib
import requests, logging
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.classes.openai.text import GPT
from discord_tron_master.classes.stabilityai.api import StabilityAI
from PIL import ImageDraw, ImageFont, Image

def retrieve_vlm_caption(image_url) -> str:
    from gradio_client import Client

    client = Client("https://tri-ml-vlm-demo.hf.space/--replicas/fr18a/")
    result = client.predict(
            "Crop",	# str  in 'Preprocess for non-square image' Radio component
            fn_index=1
    )
    client.close()
    return str(result)

async def generate_pixart_via_hub(prompt:str, user_id: int = None):
    from gradio_client import Client
    user_config = AppConfig().get_user_config(user_id=user_id)
    resolution = user_config.get("resolution", {"width": 1024, "height": 1024})
    res_str = f"{resolution['width']}x{resolution['height']}"

    client = Client("ptx0/PixArt-900M")
    result = client.predict(
            prompt=prompt,
            guidance_scale=user_config.get("guidance_scale", 4.4),
            num_inference_steps=28,
            resolution=res_str,
            negative_prompt=user_config.get("negative_prompt", "blurry, ugly, cropped"),
            api_name="/predict"
    )
    # close the connection
    client.close()
    split_pieces = result.split('/')
    return f"https://ptx0-pixart-900m.hf.space/file=/tmp/gradio/{split_pieces[-2]}/image.webp"


async def generate_cascade_via_hub(prompt: str, user_id: int = None):
    from gradio_client import Client

    client = Client("multimodalart/stable-cascade")
    result = client.predict(
            prompt,	# str  in 'Prompt' Textbox component
            'blurry, cropped, ugly',	# str  in 'Negative prompt' Textbox component
            0,	# float (numeric value between 0 and 2147483647) in 'Seed' Slider component
            1024,	# float (numeric value between 1024 and 1536) in 'Width' Slider component
            1024,	# float (numeric value between 1024 and 1536) in 'Height' Slider component
            10,	# float (numeric value between 10 and 30) in 'Prior Inference Steps' Slider component
            0,	# float (numeric value between 0 and 20) in 'Prior Guidance Scale' Slider component
            4,	# float (numeric value between 4 and 12) in 'Decoder Inference Steps' Slider component
            0,	# float (numeric value between 0 and 0) in 'Decoder Guidance Scale' Slider component
            1,	# float (numeric value between 1 and 2) in 'Number of Images' Slider component
            api_name="/run"
    )
    # close the connection
    client.close()
    split_pieces = result.split('/')
    return f"https://multimodalart-stable-cascade.hf.space/file=/tmp/gradio/{split_pieces[-2]}/image.png"


async def generate_sd3_via_hub(prompt: str, model: str = None, user_id: int = None):
    from gradio_client import Client
    user_config = AppConfig().get_user_config(user_id=user_id)

    client = Client("ameerazam08/SD-3-Medium-GPU")
    result = client.predict(
            prompt=prompt,
            negative_prompt=user_config.get("negative_prompt", "deformed, distorted, disfigured, poorly drawn, bad anatomy, wrong anatomy, extra limb, missing limb, floating limbs, mutated hands and fingers, disconnected limbs, mutation, mutated, ugly, disgusting, blurry, amputation, NSFW"),
            use_negative_prompt=True,
            seed=0,
            width=1024,
            height=1024,
            guidance_scale=7,
            randomize_seed=True,
            num_inference_steps=50,
            NUM_IMAGES_PER_PROMPT=1,
            api_name="/run"
    )
    client.close()
    split_pieces = result[0]['image'].split('/')
    return f"https://ameerazam08-sd-3-medium-gpu.hf.space/file=/tmp/gradio/{split_pieces[-2]}/image.webp"


async def generate_terminus_via_hub(prompt: str, model: str = "velocity", user_id: int = None):
    from gradio_client import Client
    available_models = {
        "velocity": "ptx0/ptx0-terminus-xl-velocity-v2",
        "gamma": "ptx0/ptx0-terminus-xl-gamma-v2-1"
    }
    client = Client(available_models[model])
    user_config = AppConfig().get_user_config(user_id=user_id)
    result = client.predict(
            prompt=prompt,
            guidance_scale=11.5,
            guidance_rescale=0.7,
            num_inference_steps=25,
            negative_prompt=user_config.get("negative_prompt", "underexposed, blurry, ugly, washed-out"),
            api_name="/predict"
    )
    client.close()
    split_pieces = result[0]['image'].split('/')
    return f"https://ptx0-ptx0-terminus-xl-velocity-v2.hf.space/file=/tmp/gradio/{split_pieces[-2]}/image.webp"

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
    client.close()
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
        # Create a new image with the two images side by side.
        from PIL import Image
        if not hasattr(dalle_image, 'size'):
            dalle_image = BytesIO(dalle_image)
            dalle_image = Image.open(dalle_image)
        # # Add "DALLE-3" and "Stable Diffusion 3" labels to upper left corner
        try:
            # Attempt to use a specific font
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 40
            )
        except IOError:
            # Fallback to default font
            font = ImageFont.load_default(size=40)
        # sd3_image = await stabilityai.generate_image(prompt, user_config, model="sd3-turbo")
        # if not hasattr(sd3_image, 'size'):
        #     try:
        #         sd3_image = BytesIO(sd3_image)
        #         sd3_image = Image.open(sd3_image)
        #     except:
        #         # make black image, we had an error.
        #         sd3_image = Image.new('RGB', dalle_image.size, (0, 0, 0))

        # draw = ImageDraw.Draw(sd3_image)
        # draw.text((10, 10), "Stable Diffusion 3", (255, 255, 255), font=font, stroke_fill=(0,0,0), stroke_width=4)
        # Retrieve https://pollinations.ai/prompt/{prompt}?seed={seed}&width={user_config['resolution']['width']}&height={user_config['resolution']['height']}
        # pollinations_image = Image.open(BytesIO(requests.get(f"https://pollinations.ai/prompt/{prompt}?seed={user_config['seed']}&width={user_config['resolution']['width']}&height={user_config['resolution']['height']}").content))
        try:
            pollinations_image = Image.open(BytesIO(requests.get(await generate_sd3_via_hub(prompt, user_id=user_id)).content))
        except:
            pollinations_image = Image.new('RGB', dalle_image.size, (0, 0, 0))
        try:
            extra_image = {
                "label": "Terminus XL Velocity V2 (WIP)",
                "data": Image.open(BytesIO(requests.get(await generate_terminus_via_hub(prompt, user_id=user_id)).content))
            }
        except:
            extra_image = None
        try:
            extra_image_2 = {
                "label": "PixArt 900M (WIP)",
                "data": Image.open(BytesIO(requests.get(await generate_pixart_via_hub(prompt, user_id=user_id)).content))
            }
        except:
            extra_image_2 = None
        draw = ImageDraw.Draw(pollinations_image)
        draw.text((10, 10), "SD3 2B", (255, 255, 255), font=font, stroke_fill=(0,0,0), stroke_width=4)

        draw = ImageDraw.Draw(dalle_image)
        draw.text((10, 10), "DALL-E", (255, 255, 255), font=font, stroke_fill=(0,0,0), stroke_width=4)
        width, height = dalle_image.size
        new_width = width * 2
        if extra_image is not None:
            extra_image_vertical_position = int((height - extra_image["data"].size[1]) / 2)
            extra_image_position = (new_width, extra_image_vertical_position)
            new_width = new_width + extra_image["data"].size[0]
        if extra_image_2 is not None:
            extra_image_vertical_position_2 = int((height - extra_image_2["data"].size[1]) / 2)
            extra_image_position_2 = (new_width, extra_image_vertical_position_2)
            new_width = new_width + extra_image_2["data"].size[0]

        new_image = Image.new('RGB', (new_width, height))
        new_image.paste(pollinations_image, (0, 0))
        new_image.paste(dalle_image, (width, 0))
        # Do we have an extra_image?
        if extra_image is not None:
            draw = ImageDraw.Draw(extra_image["data"])
            draw.text((10, 10), extra_image["label"], (255, 255, 255), font=font, stroke_fill=(0,0,0), stroke_width=4)
            width, height = extra_image["data"].size
            new_image.paste(extra_image["data"], extra_image_position)
        if extra_image_2 is not None:
            draw = ImageDraw.Draw(extra_image_2["data"])
            draw.text((10, 10), extra_image_2["label"], (255, 255, 255), font=font, stroke_fill=(0,0,0), stroke_width=4)
            width, height = extra_image_2["data"].size
            new_image.paste(extra_image_2["data"], extra_image_position_2)
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
