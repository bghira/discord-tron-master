from typing import Dict
import websocket, discord, base64, logging, time
from hashlib import md5
from websockets.client import WebSocketClientProtocol
from io import BytesIO

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

async def send_image(command_processor, arguments: Dict, data: Dict, websocket: WebSocketClientProtocol):
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            # If "arguments" contains "image", it is base64 encoded. We can send that in the message.
            file=None
            if "image" in arguments:
                if arguments["image"] is not None:
                    base64_decoded_image = base64.b64decode(arguments["image"])
                    buffer = BytesIO(base64_decoded_image)
                    web_root = command_processor.config.get_web_root()
                    url_base = command_processor.config.get_url_base()
                    filename = str(time.time()) + md5(buffer.getvalue()) + ".png"
                    buffer.save(web_root + '/' + filename)
                    arguments['message'] = arguments['message'] + '\n' + url_base + '/' + filename
            await channel.send(content=arguments["message"], file=file)
        except Exception as e:
            logging.error(f"Error sending message to {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "Message sent."}

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
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            await channel.create_thread(name=arguments["name"])
        except Exception as e:
            logging.error(f"Error creating thread in {channel.name} ({channel.id}): {e}")
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