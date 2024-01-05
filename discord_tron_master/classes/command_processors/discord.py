from typing import Dict
import websocket, discord, base64, logging, time, hashlib, gzip, os
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

async def send_message(command_processor, arguments: Dict, data: Dict, websocket: WebSocketClientProtocol):
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
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
                    logging.debug(f"Incoming message to send, has an image url list.")
                    embeds = []
                    for image_url in arguments["image_url_list"]:
                        logging.debug(f"Adding {image_url} to embed")
                        embed = discord.Embed(url="http://tripleback.net")
                        embed.set_image(url=image_url)
                        embeds.append(embed)
                    wants_variations = len(arguments["image_url_list"])
                else:
                    logging.debug(f"Incoming message to send, has zero image url list.")
            if "audio_url" in arguments:
                if arguments["audio_url"] is not None:
                    logging.debug(f"Incoming message to send, has an audio url.")
                    arguments["message"] = f"{arguments['message']}\nAudio URL: {arguments['audio_url']}"
                else:
                    logging.debug(f"Incoming message to send, has zero audio url.")
            if "audio_data" in arguments:
                if arguments["audio_data"] is not None:
                    logging.debug(f"Incoming message had audio data. Embedding as a file.")
                    file=await get_audio_file(arguments["audio_data"])
            message = await channel.send(content=arguments["message"], file=file, embeds=embeds)
            # List of number emojis
            number_emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣']

            # Add reactions
            adding_reactions = []
            if wants_variations > 0:
                adding_reactions = [ '♻️', '©️', '🌱', '📜' ]

            # Add the appropriate number of emojis based on wants_variations
            if wants_variations > 0:
                adding_reactions.extend(number_emojis[:wants_variations])

            # Always add the '❌' reaction
            adding_reactions.append('❌')
            await command_processor.discord.attach_default_reactions(message, adding_reactions)
        except Exception as e:
            logging.error(f"Error sending message to {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "Message sent."}

async def send_large_message(command_processor, arguments: Dict, data: Dict, websocket: WebSocketClientProtocol):
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            # If "arguments" contains "image", it is base64 encoded. We can send that in the message.
            await command_processor.discord.send_large_message(channel, arguments["message"])
        except Exception as e:
            logging.error(f"Error sending large message to {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "Large message sent."}


async def send_image(command_processor, arguments: Dict[str, str], data: Dict[str, str], websocket: WebSocketClientProtocol):
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
            logging.error(f"Error sending message to {channel.name} ({channel.id}): {e}")
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
            logging.warn(f"Could not delete message in {channel.name} ({channel.id}), another bot likely got to it already. Dang!")
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
    logging.debug(f"Received command data: {data}")
    logging.debug(f"Received command arguments: {arguments}")
    if "message" not in arguments:
        raise Exception("Missing message argument.")
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            message = await channel.fetch_message(data["message_id"])
            await message.edit(content=arguments["message"])
        except Exception as e:
            logging.error(f"Error editing message in {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "Message edited."}

async def send_embed(command_processor, arguments: Dict, data: Dict, websocket: WebSocketClientProtocol):
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            await channel.send(embed=arguments["embed"])
        except Exception as e:
            logging.error(f"Error sending embed to {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "Embed sent."}

async def send_file(command_processor, arguments: Dict, data: Dict, websocket: WebSocketClientProtocol):
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            await channel.send(file=arguments["file"])
        except Exception as e:
            logging.error(f"Error sending file to {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "File sent."}

async def send_files(command_processor, arguments: Dict, data: Dict, websocket: WebSocketClientProtocol):
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            await channel.send(files=arguments["files"])
        except Exception as e:
            logging.error(f"Error sending files to {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "Files sent."}

async def create_thread(command_processor, arguments: Dict, data: Dict, websocket: WebSocketClientProtocol):
    logging.debug(f"Entering create_thread: {arguments} {data} {websocket} {command_processor}")
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    logging.debug(f"Found channel? {channel}")
    wants_variations = 0
    if channel is not None:
        try:
            # Maybe channel is already a thread.
            if isinstance(channel, discord.Thread):
                logging.debug(f"Channel is already a thread. Using it.")
                thread = channel
            elif isinstance(channel, discord.TextChannel):
                logging.debug(f"Channel is a text channel. Creating thread.")
                thread = await channel.create_thread(name=arguments["name"])
            else:
                raise Exception(f"Channel is not a text channel or thread. It is a {type(channel)}")
            embed = None
            embeds = None
            if "image" in arguments:
                logging.debug(f"Found image inside message")
                # We want to send any image data into the thread we create.
                pnginfo = PngImagePlugin()
                metadata = arguments["metadata"]
                for key, value in metadata.items():
                    pnginfo.add_text(key, value)
                embed = await get_image_embed(arguments["image"], pnginfo=pnginfo)
                wants_variations = 1
            if "image_url" in arguments:
                logging.debug(f"Found image URL inside arguments: {arguments['image_url']}")
                embed = discord.Embed(url='https://tripleback.net')
                embed.set_image(url=arguments["image_url"])
                wants_variations = 1
            if "image_url_list" in arguments:
                if arguments["image_url_list"] is not None:
                    logging.debug(f"Incoming message to send, has an image url list.")
                    embeds = []
                    wants_variations = len(arguments["image_url_list"])
                    for image_url in arguments["image_url_list"]:
                        logging.debug(f"Adding {image_url} to embed")
                        new_embed = discord.Embed(url="http://tripleback.net")
                        new_embed.set_image(url=image_url)
                        embeds.append(new_embed)
                else:
                    logging.debug(f"Incoming message to send, has zero image url list.")
            logging.debug(f"Sending message to thread: {arguments['message']}")
            if "mention" in arguments:
                logging.debug(f"Mentioning user: {arguments['mention']}")
                arguments["message"] = f"<@{arguments['mention']}> {arguments['message']}"
            message = await thread.send(content=arguments["message"], embeds=embeds)

            # List of number emojis
            number_emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣']

            # Add reactions
            adding_reactions = []
            if wants_variations > 0:
                adding_reactions = [ '♻️', '©️', '🌱', '📜', '💾' ]

            # Add the appropriate number of emojis based on wants_variations
            if wants_variations > 0:
                adding_reactions.extend(number_emojis[:wants_variations])

            # Always add the '❌' reaction
            adding_reactions.append('❌')
            await command_processor.discord.attach_default_reactions(message, adding_reactions)
        except Exception as e:
            logging.error(f"Error creating thread in {channel.name} ({channel.id}): {e}")
    logging.debug(f"Exiting create_thread")
    return {"success": True, "result": "Thread created."}

async def delete_thread(command_processor, arguments: Dict, data: Dict, websocket: WebSocketClientProtocol):
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            await channel.delete()
        except Exception as e:
            logging.error(f"Error deleting thread in {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "Thread deleted."}

async def send_message_to_thread(command_processor, arguments: Dict, data: Dict, websocket: WebSocketClientProtocol):
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            await discord.send(content=arguments["message"])
        except Exception as e:
            logging.error(f"Error sending message to thread in {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "Message sent."}

async def get_image_embed(image_data, pnginfo = None, create_embed: bool = True):
    base64_decoded_image = base64.b64decode(image_data)
    buffer = BytesIO(base64_decoded_image)
    filename = f"{time.time()}{hashlib.md5(buffer.read()).hexdigest()}.png"
    buffer.seek(0)
    image = Image.open(buffer)
    if pnginfo is not None:
        logging.debug(f'Saving with pnginfo: {pnginfo}')
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

async def get_message_txt_file(text):
    # write to file
    with open("result.txt", "w") as file:
        file.write('arg1 = {0}, arg2 = {1}'.format(arg1, arg2))
    
    # send file to Discord in message
    with open("result.txt", "rb") as file:
        await ctx.send("Your file is:", file=discord.File(file, "result.txt"))
