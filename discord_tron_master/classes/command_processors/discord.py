from typing import Dict
import websocket, discord, base64, logging, time, hashlib, gzip
from websockets.client import WebSocketClientProtocol
from io import BytesIO
from discord_tron_master.classes.app_config import AppConfig
from PIL import Image
from websockets import WebSocketClientProtocol
config = AppConfig()

async def send_message(command_processor, arguments: Dict, data: Dict, websocket: WebSocketClientProtocol):
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            # If "arguments" contains "image", it is base64 encoded. We can send that in the message.
            file=None
            if "image" in arguments:
                if arguments["image"] is not None:
                    base64_decoded_image = base64.b64decode(arguments["image"])
                    buffer = BytesIO(base64_decoded_image)
                    file=discord.File(buffer, "image.png")
            await channel.send(content=arguments["message"], file=file)
        except Exception as e:
            logging.error(f"Error sending message to {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "Message sent."}

async def send_image(command_processor, arguments: Dict[str, str], data: Dict[str, str], websocket: WebSocketClientProtocol):
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            # If "arguments" contains "image", it is base64 encoded. We can send that in the message.
            image_data = arguments.get("image")
            embed = None
            if image_data is not None:
                decoded_image = decompress_b64(image_data)
                embed = get_embed(decoded_image)
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
            if "image" in arguments:
                logging.debug(f"Found image inside message")
                # We want to send any image data into the thread we create.
                embed = await get_embed(arguments["image"])
            logging.debug(f"Sending message to thread: {arguments['message']}")
            if "mention" in arguments:
                logging.debug(f"Mentioning user: {arguments['mention']}")
                arguments["message"] = f"<@{arguments['mention']}> {arguments['message']}"
            await thread.send(content=arguments["message"], embed=embed)
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
            await channel.send(content=arguments["message"])
        except Exception as e:
            logging.error(f"Error sending message to thread in {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "Message sent."}

async def get_embed(image_data):
    base64_decoded_image = base64.b64decode(image_data)
    buffer = BytesIO(base64_decoded_image)
    web_root = config.get_web_root()
    url_base = config.get_url_base()
    # Error 2: buffer is already a BytesIO object, so we don't need to call buffer.getvalue() before hashing it.
    filename = f"{time.time()}{hashlib.md5(buffer.read()).hexdigest()}.png"
    # Error 3: buffer.save() is not a valid method. We should use Image.save() instead.
    buffer.seek(0)
    image = Image.open(buffer)
    image.save(f"{web_root}/{filename}")
    image_url = f"\n{url_base}/{filename}"
    embed = discord.Embed()
    embed.set_image(url=image_url)
    return embed

def decompress_b64(compressed_b64: str) -> Image:
    # Decompress the base64-encoded image
    compressed_b64 = compressed_b64.encode('utf-8')
    decompressed_b64 = BytesIO()
    with gzip.GzipFile(fileobj=BytesIO(compressed_b64), mode="rb") as gzip_file:
        decompressed_b64.write(gzip_file.read())
    return decompressed_b64.getvalue()
