from io import BytesIO
import discord as discord_lib
import requests, logging, asyncio
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.classes.openai.text import GPT
from discord_tron_master.classes.stabilityai.api import StabilityAI
from PIL import ImageDraw, ImageFont, Image
from threading import ThreadPoolExecutor

def retrieve_vlm_caption(image_url) -> str:
    from gradio_client import Client

    client = Client("https://tri-ml-vlm-demo.hf.space/--replicas/fr18a/")
    result = client.predict(
            "Crop",	# str  in 'Preprocess for non-square image' Radio component
            fn_index=1
    )
    client.close()
    return str(result)

def generate_pixart_via_hub(prompt:str, user_id: int = None):
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


def generate_cascade_via_hub(prompt: str, user_id: int = None):
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


def generate_sd3_via_hub(prompt: str, model: str = None, user_id: int = None):
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


def generate_terminus_via_hub(prompt: str, model: str = "velocity", user_id: int = None):
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
    
    user_config["resolution"] = {"width": 1024, "height": 1024}
    
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor()

    async def fetch_image(url):
        return await loop.run_in_executor(executor, lambda: Image.open(BytesIO(requests.get(url).content)))

    async def generate_dalle_image():
        dalle_image_data = await loop.run_in_executor(executor, lambda: GPT().dalle_image_generate(prompt=prompt, user_config=user_config))
        if not hasattr(dalle_image_data, 'size'):
            dalle_image_data = BytesIO(dalle_image_data)
            dalle_image_data = Image.open(dalle_image_data)
        return dalle_image_data

    async def generate_pollinations_image():
        url = generate_sd3_via_hub(prompt, user_id=user_id)
        try:
            return await fetch_image(url)
        except:
            return Image.new('RGB', (1024, 1024), (0, 0, 0))
    
    async def generate_extra_images():
        extra_images = []
        try:
            url_terminus = generate_terminus_via_hub(prompt, user_id=user_id)
            extra_image_terminus = await fetch_image(url_terminus)
            extra_images.append({"label": "Terminus XL Velocity V2 (WIP)", "data": extra_image_terminus})
        except:
            pass
        try:
            url_pixart = generate_pixart_via_hub(prompt, user_id=user_id)
            extra_image_pixart = await fetch_image(url_pixart)
            extra_images.append({"label": "PixArt 900M (WIP)", "data": extra_image_pixart})
        except:
            pass
        return extra_images

    try:
        dalle_image, pollinations_image, extra_images = await asyncio.gather(
            generate_dalle_image(),
            generate_pollinations_image(),
            generate_extra_images()
        )

        # Add "DALLE-3" and "Stable Diffusion 3" labels to upper left corner
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 40)
        except IOError:
            font = ImageFont.load_default()

        draw = ImageDraw.Draw(dalle_image)
        draw.text((10, 10), "DALL-E", (255, 255, 255), font=font, stroke_fill=(0,0,0), stroke_width=4)

        draw = ImageDraw.Draw(pollinations_image)
        draw.text((10, 10), "SD3 2B", (255, 255, 255), font=font, stroke_fill=(0,0,0), stroke_width=4)

        width, height = dalle_image.size
        new_width = width * 2

        for extra_img in extra_images:
            extra_img["data"] = extra_img["data"].resize((1024, 1024))
            extra_img_vertical_position = int((height - extra_img["data"].size[1]) / 2)
            extra_img["position"] = (new_width, extra_img_vertical_position)
            new_width += extra_img["data"].size[0]

        new_image = Image.new('RGB', (new_width, height))
        new_image.paste(pollinations_image, (0, 0))
        new_image.paste(dalle_image, (width, 0))

        for extra_img in extra_images:
            draw = ImageDraw.Draw(extra_img["data"])
            draw.text((10, 10), extra_img["label"], (255, 255, 255), font=font, stroke_fill=(0,0,0), stroke_width=4)
            new_image.paste(extra_img["data"], extra_img["position"])

        output = BytesIO()
        new_image.save(output, format="PNG")
        output.seek(0)

        if hasattr(ctx, 'channel'):
            await ctx.channel.send(file=discord_lib.File(output, "comparison.png"))
        else:
            await ctx.send(file=discord_lib.File(output, "comparison.png"))

    except Exception as e:
        await ctx.send(f"Error generating image: {e}")