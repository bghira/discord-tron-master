from typing import Dict
import json
import websocket, discord, base64, logging, time, hashlib, gzip, os, requests
from websockets.client import WebSocketClientProtocol
from io import BytesIO
from discord_tron_master.classes.app_config import AppConfig
from PIL import Image, PngImagePlugin
from websockets import WebSocketClientProtocol
config = AppConfig()
from scipy.io.wavfile import write as write_wav
from scipy.io.wavfile import read as read_wav
BARK_SAMPLE_RATE = 24_000
web_root = config.get_web_root()
url_base = config.get_url_base()
logger = logging.getLogger(__name__)
logger.setLevel(config.get_log_level())
ZORK_SCENE_FLAG_KEYS = {
    "zork_scene",
    "suppress_image_reactions",
    "suppress_image_details",
}

def _is_truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return value != 0
    return False

def _contains_flag(value, flag_keys):
    if value is None:
        return False
    stack = [value]
    visited = 0
    max_nodes = 1200
    while stack and visited < max_nodes:
        current = stack.pop()
        visited += 1
        if isinstance(current, dict):
            for key, nested_value in current.items():
                if str(key).lower() in flag_keys and _is_truthy(nested_value):
                    return True
                stack.append(nested_value)
        elif isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, str):
            # Workers occasionally send nested payload blobs as JSON strings.
            try:
                parsed = json.loads(current)
                if isinstance(parsed, (dict, list)):
                    stack.append(parsed)
            except Exception:
                pass
    return False

def _is_zork_scene_request(arguments, data):
    return _contains_flag(arguments, ZORK_SCENE_FLAG_KEYS) or _contains_flag(data, ZORK_SCENE_FLAG_KEYS)

def _is_zork_enabled_thread_channel(channel, data) -> bool:
    if channel is None or not isinstance(channel, discord.Thread):
        return False
    try:
        guild_id = None
        if isinstance(data, dict):
            guild_id = data.get("guild", {}).get("id")
        if guild_id is None and getattr(channel, "guild", None) is not None:
            guild_id = channel.guild.id
        if guild_id is None:
            return False
        app = AppConfig.get_flask()
        if app is None:
            return False
        from discord_tron_master.models.zork import ZorkChannel
        with app.app_context():
            row = ZorkChannel.query.filter_by(
                guild_id=int(guild_id),
                channel_id=int(channel.id),
                enabled=True,
            ).first()
            return row is not None
    except Exception:
        return False

def _has_media_payload(arguments: Dict) -> bool:
    if not isinstance(arguments, dict):
        return False
    image_url_list = arguments.get("image_url_list")
    if isinstance(image_url_list, list) and len(image_url_list) > 0:
        return True
    for key in ("image", "image_url", "video_url"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if value is not None and not isinstance(value, str):
            return True
    return False

def _should_suppress_zork_image_body(channel, arguments: Dict, data: Dict, zork_scene_mode: bool) -> bool:
    if zork_scene_mode:
        return True
    if not _has_media_payload(arguments):
        return False
    return _is_zork_enabled_thread_channel(channel, data)

def _strip_worker_image_details(message: str) -> str:
    if not isinstance(message, str):
        return ""
    detail_prefixes = (
        "**Prompt**:",
        "**Settings**:",
        "**Model**:",
        "**Hydra-",
        "**GPU-",
    )
    cleaned_lines = []
    for raw_line in message.splitlines():
        line = raw_line.strip()
        if any(line.startswith(prefix) for prefix in detail_prefixes):
            continue
        cleaned_lines.append(raw_line)
    return "\n".join(cleaned_lines).strip()

async def send_message(command_processor, arguments: Dict, data: Dict, websocket: WebSocketClientProtocol):
    logger.debug(f"Entering send_message: {arguments} {data}")
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            zork_scene_mode = _is_zork_scene_request(arguments, data)
            suppress_body = _should_suppress_zork_image_body(channel, arguments, data, zork_scene_mode)
            if zork_scene_mode and "message" in arguments:
                logger.debug("Detected zork scene image payload in send_message; suppressing default reactions and worker detail text.")
                arguments["message"] = _strip_worker_image_details(arguments["message"])
                if not arguments["message"]:
                    mention_id = arguments.get("mention") or data.get("author", {}).get("id")
                    arguments["message"] = f"<@{mention_id}>" if mention_id else "Scene updated."
            # If "arguments" contains "image", it is base64 encoded. We can send that in the message.
            file = None
            embeds = None
            wants_variations = False
            if "image" in arguments:
                if arguments["image"] is not None:
                    base64_decoded_image = base64.b64decode(arguments["image"])
                    buffer = BytesIO(base64_decoded_image)
                    file=discord.File(buffer, "image.png")
                    wants_variations = 1
            if "image_url_list" in arguments:
                if arguments["image_url_list"] is not None:
                    if config.should_compare() and not suppress_body:
                        # Use the comparison tool for DALLE3 and SD3.
                        logger.debug(f"Using comparison tool for DALLE3 and SD3.")
                        from discord_tron_master.cogs.image import generate_image
                        await generate_image(
                            channel,
                            arguments["image_prompt"],
                            extra_image={
                                "label": arguments["image_model"],
                                "data": requests.get(arguments["image_url_list"][0]).content
                            }
                        )

                    logger.debug(f"Incoming message to send, has an image url list.")
                    embeds = []
                    wants_variations = len(arguments["image_url_list"])
                    for image_url in arguments["image_url_list"]:
                        if 'mp4' in image_url:
                            if not suppress_body:
                                arguments["message"] = f"{arguments['message']}\nVideo URL: {image_url}"
                        else:
                            logger.debug(f"Adding {image_url} to embed")
                            embed = discord.Embed(url="http://tripleback.net")
                            embed.set_image(url=image_url)
                            embeds.append(embed)
                else:
                    logger.debug(f"Incoming message to send, has zero image url list.")
            if "audio_url" in arguments:
                if arguments["audio_url"] is not None:
                    logger.debug(f"Incoming message to send, has an audio url.")
                    if not suppress_body:
                        arguments["message"] = f"{arguments['message']}\nAudio URL: {arguments['audio_url']}"
                else:
                    logger.debug(f"Incoming message to send, has zero audio url.")
            if "video_url" in arguments:
                if arguments["video_url"] is not None:
                    logger.debug(f"Incoming message to send, has a video url.")
                    if not suppress_body:
                        arguments["message"] = f"{arguments['message']}\nVideo URL: {arguments['video_url']}"
                    embed = discord.Embed(url='https://tripleback.net')
                    embed.set_image(url=arguments["video_url"])
                else:
                    logger.debug(f"Incoming message to send, has zero video url.")

            if "audio_data" in arguments:
                if arguments["audio_data"] is not None:
                    logger.debug(f"Incoming message had audio data. Embedding as a file.")
                    file=await get_audio_file(arguments["audio_data"])
            content_to_send = arguments.get("message")
            if suppress_body and (file is not None or embeds is not None):
                content_to_send = None
            message = await channel.send(content=content_to_send, file=file, embeds=embeds)
            # List of number emojis
            number_emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣']

            # Add reactions
            adding_reactions = []
            if wants_variations > 0:
                adding_reactions = [ '♻️', '📋', '🌱', '📜' ]

            # Add the appropriate number of emojis based on wants_variations
            if wants_variations > 0:
                adding_reactions.extend(number_emojis[:wants_variations])

            # Always add the '❌' reaction
            adding_reactions.append('❌')
            if not suppress_body:
                await command_processor.discord.attach_default_reactions(message, adding_reactions)
        except Exception as e:
            logger.error(f"Error sending message to {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "Message sent."}

async def send_large_message(command_processor, arguments: Dict, data: Dict, websocket: WebSocketClientProtocol):
    logger.debug(f"Entering send_large_message: {arguments} {data}")
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            # If "arguments" contains "image", it is base64 encoded. We can send that in the message.
            await command_processor.discord.send_large_message(channel, arguments["message"])
        except Exception as e:
            logger.error(f"Error sending large message to {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "Large message sent."}


async def send_image(command_processor, arguments: Dict[str, str], data: Dict[str, str], websocket: WebSocketClientProtocol):
    logger.debug(f"Entering send_image: {arguments} {data}")
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            # If "arguments" contains "image", it is base64 encoded. We can send that in the message.
            image_data = arguments.get("image")
            embed = None
            if image_data is not None:
                embed = get_image_embed(image_data)
            await channel.send(content=arguments["message"], embed=embed)
        except Exception as e:
            logger.error(f"Error sending message to {channel.name} ({channel.id}): {e}")
            return {"success": False, "result": str(e)}
        return {"success": True, "result": "Message sent."}
    return {"success": False, "result": "Channel not found."}


async def delete_message(command_processor, arguments: Dict, data: Dict, websocket: WebSocketClientProtocol):
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            message = await channel.fetch_message(data["message_id"])
            await message.delete()
        except Exception as e:
            logger.warning(f"Could not delete message in {channel.name} ({channel.id}), another bot likely got to it already. Dang!")
            return {"success": True, "result": "Message deleted, but not by us."}
        return {"success": True, "result": "Message deleted."}
    return {"error": "Channel could not be found."}

async def delete_previous_errors(command_processor, arguments: Dict, data: Dict, websocket: WebSocketClientProtocol):
    try:
        await command_processor.discord.delete_previous_errors(data["channel"]["id"], prefix="seems we had an error")
    except Exception as e:
        return {"error": f"Could not delete previous messages: {e}"}
    return {"success": True, "result": "Message deleted."}

async def edit_message(command_processor, arguments: Dict, data: Dict, websocket: WebSocketClientProtocol):
    logger.debug(f"Received command data: {data}")
    logger.debug(f"Received command arguments: {arguments}")
    if "message" not in arguments:
        raise Exception("Missing message argument.")
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            message = await channel.fetch_message(data["message_id"])
            await message.edit(content=arguments["message"])
        except Exception as e:
            logger.error(f"Error editing message in {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "Message edited."}

async def send_embed(command_processor, arguments: Dict, data: Dict, websocket: WebSocketClientProtocol):
    logger.debug(f"Entering send_embed: {arguments} {data}")
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            await channel.send(embed=arguments["embed"])
        except Exception as e:
            logger.error(f"Error sending embed to {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "Embed sent."}

async def send_file(command_processor, arguments: Dict, data: Dict, websocket: WebSocketClientProtocol):
    logger.debug(f"Entering send_file: {arguments} {data} {websocket} {command_processor}")
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            await channel.send(file=arguments["file"])
        except Exception as e:
            logger.error(f"Error sending file to {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "File sent."}

async def send_files(command_processor, arguments: Dict, data: Dict, websocket: WebSocketClientProtocol):
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            await channel.send(files=arguments["files"])
        except Exception as e:
            logger.error(f"Error sending files to {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "Files sent."}

async def create_thread(command_processor, arguments: Dict, data: Dict, websocket: WebSocketClientProtocol):
    logger.debug(f"Entering create_thread: {arguments} {data} {websocket} {command_processor}")
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    logger.debug(f"Found channel? {channel}")
    wants_variations = 0
    zork_scene_mode = _is_zork_scene_request(arguments, data)
    if channel is not None:
        try:
            suppress_body = _should_suppress_zork_image_body(channel, arguments, data, zork_scene_mode)
            if zork_scene_mode and "message" in arguments:
                logger.debug("Detected zork scene image payload in create_thread; suppressing default reactions and worker detail text.")
                arguments["message"] = _strip_worker_image_details(arguments["message"])
                if not arguments["message"]:
                    mention_id = arguments.get("mention") or data.get("author", {}).get("id")
                    arguments["message"] = f"<@{mention_id}>" if mention_id else "Scene updated."
            # Maybe channel is already a thread.
            if isinstance(channel, discord.Thread):
                logger.debug(f"Channel is already a thread. Using it.")
                thread = channel
            elif isinstance(channel, discord.TextChannel):
                logger.debug(f"Channel is a text channel. Creating thread.")
                thread = await channel.create_thread(name=arguments["name"], type=discord.ChannelType.public_thread)
            else:
                raise Exception(f"Channel is not a text channel or thread. It is a {type(channel)}")
            embed = None
            embeds = None
            if "image" in arguments:
                logger.debug(f"Found image inside message")
                # We want to send any image data into the thread we create.
                pnginfo = PngImagePlugin()
                metadata = arguments["metadata"]
                for key, value in metadata.items():
                    pnginfo.add_text(key, value)
                embed = await get_image_embed(arguments["image"], pnginfo=pnginfo)
                wants_variations = 1
            if "video_url" in arguments:
                if arguments["video_url"] is not None:
                    logger.debug(f"Incoming message to send, has a video url.")
                    arguments["message"] = f"{arguments['message']}\nVideo URL: {arguments['video_url']}"
                    embed = discord.Embed(url='https://tripleback.net')
                    embed.set_image(url=arguments["video_url"])
                else:
                    logger.debug(f"Incoming message to send, has zero video url.")
            if "image_url" in arguments:
                logger.debug(f"Found image URL inside arguments: {arguments['image_url']}")
                embed = discord.Embed(url='https://tripleback.net')
                embed.set_image(url=arguments["image_url"])
                wants_variations = 1
            if "image_url_list" in arguments:
                if arguments["image_url_list"] is not None:
                    logger.debug(f"Incoming message to send, has an image url list.")
                    if config.should_compare() and not suppress_body:
                        # Use the comparison tool for DALLE3 and SD3.
                        logger.debug(f"Using comparison tool for DALLE3 and SD3, arguments: {arguments}")
                        from discord_tron_master.cogs.image import generate_image
                        try:
                            await generate_image(
                                channel,
                                arguments["image_prompt"],
                                user_id=arguments["user_id"],
                                extra_image={
                                    "label": arguments["image_model"],
                                    "data": Image.open(BytesIO(requests.get(arguments["image_url_list"][0]).content))
                                }
                            )
                        except Exception as e:
                            logger.error(f"Error comparing images: {e}")
                    embeds = []
                    wants_variations = len(arguments["image_url_list"])
                    for image_url in arguments["image_url_list"]:
                        if 'mp4' in image_url:
                            if not suppress_body:
                                arguments["message"] = f"{arguments['message']}\nVideo URL: {image_url}"
                        else:
                            logger.debug(f"Adding {image_url} to embed")
                            new_embed = discord.Embed(url="http://tripleback.net")
                            new_embed.set_image(url=image_url)
                            embeds.append(new_embed)
                else:
                    logger.debug(f"Incoming message to send, has zero image url list.")
            logger.debug(f"Sending message to thread: {arguments['message']}")
            if not suppress_body and "mention" in arguments:
                logger.debug(f"Mentioning user: {arguments['mention']}")
                arguments["message"] = f"<@{arguments['mention']}> {arguments['message']}"
            content_to_send = arguments.get("message")
            if suppress_body and embeds is not None:
                content_to_send = None
            message = await thread.send(content=content_to_send, embeds=embeds)

            # List of number emojis
            number_emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣']

            # Add reactions
            adding_reactions = []
            if wants_variations > 0:
                adding_reactions = [ '♻️', '📋', '🌱', '📜', '💾' ]

            # Add the appropriate number of emojis based on wants_variations
            if wants_variations > 0:
                adding_reactions.extend(number_emojis[:wants_variations])

            # Always add the '❌' reaction
            adding_reactions.append('❌')
            if not suppress_body:
                await command_processor.discord.attach_default_reactions(message, adding_reactions)
        except Exception as e:
            logger.error(f"Error creating thread in {channel.name} ({channel.id}): {e}")
    logger.debug(f"Exiting create_thread")
    return {"success": True, "result": "Thread created."}

async def delete_thread(command_processor, arguments: Dict, data: Dict, websocket: WebSocketClientProtocol):
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            await channel.delete()
        except Exception as e:
            logger.error(f"Error deleting thread in {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "Thread deleted."}

async def send_message_to_thread(command_processor, arguments: Dict, data: Dict, websocket: WebSocketClientProtocol):
    logger.debug(f"Entering send_message_to_thread: {arguments} {data}")
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            await discord.send(content=arguments["message"])
        except Exception as e:
            logger.error(f"Error sending message to thread in {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "Message sent."}

async def get_image_embed(image_data, pnginfo = None, create_embed: bool = True):
    base64_decoded_image = base64.b64decode(image_data)
    buffer = BytesIO(base64_decoded_image)
    filename = f"{time.time()}{hashlib.md5(buffer.read()).hexdigest()}.png"
    buffer.seek(0)
    image = Image.open(buffer)
    if pnginfo is not None:
        logger.debug(f'Saving with pnginfo: {pnginfo}')
        image.save(f"{web_root}/{filename}", format="PNG", pnginfo=pnginfo)
    else:
        image.save(f"{web_root}/{filename}")
    image_url = f"\n{url_base}/{filename}"
    if create_embed:
        embed = discord.Embed()
        embed.set_image(url=image_url)
        return embed
    return image_url

async def get_audio_file(audio_data):
    base64_decoded_audio = base64.b64decode(audio_data)
    buffer = BytesIO(base64_decoded_audio)
    buffer.seek(0)
    file = discord.File(filename='audio.mp3', fp=buffer, spoiler=False)
    return file

async def get_audio_url(audio_data):
    sample_rate, audio_array = extract_wav(audio_data)
    filename = f"{time.time()}{hashlib.md5(audio_data).hexdigest()}.wav"
    # Save the audio file and get its URL
    write_wav(os.path.join(web_root, filename), sample_rate, audio_array)
    audio_url = f"\n{url_base}/{filename}"
    return audio_url

def extract_wav(audio_data):
    wav_binary_stream = BytesIO(audio_data)
    return read_wav(wav_binary_stream)

async def get_audio_url_from_numpy(audio_array):
    audio_base64 = base64.b64encode(audio_array)
    return get_audio_url(audio_base64)

async def get_video_url(video_data):
    filename = f"{time.time()}{hashlib.md5(video_data).hexdigest()}.mp4"
    # Save the video mp4 file and get its URL
    os.makedirs(web_root, exist_ok=True)
    with open(os.path.join(web_root, filename), 'wb') as f:
        f.write(video_data)
    
    video_url = f"\n{url_base}/{filename}"
    return video_url

async def get_message_txt_file(text):
    # write to file
    with open("result.txt", "w") as file:
        file.write('arg1 = {0}, arg2 = {1}'.format(arg1, arg2))
    
    # send file to Discord in message
    with open("result.txt", "rb") as file:
        await ctx.send("Your file is:", file=discord.File(file, "result.txt"))
