from typing import Dict
import websocket

async def send_message(command_processor, arguments: Dict, data: Dict, websocket: websocket):
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            await channel.send(arguments["message"])
        except Exception as e:
            print(f"Error sending message to {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "Message sent."}

async def delete_message(command_processor, arguments: Dict, data: Dict, websocket: websocket):
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            message = await channel.fetch_message(data["message_id"])
            await message.delete()
        except Exception as e:
            print(f"Error deleting message in {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "Message deleted."}

async def delete_previous_errors(command_processor, arguments: Dict, data: Dict, websocket: websocket):
    try:
        await command_processor.discord.delete_previous_errors(data["channel"]["id"], prefix="seems we had an error")
    except Exception as e:
        return {"error": f"Could not delete previous messages: {e}"}
    return {"success": True, "result": "Message deleted."}

async def edit_message(command_processor, arguments: Dict, data: Dict, websocket: websocket):
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            message = await channel.fetch_message(data["message_id"])
            await message.edit(content=arguments["message"])
        except Exception as e:
            print(f"Error editing message in {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "Message edited."}

async def send_embed(command_processor, arguments: Dict, data: Dict, websocket: websocket):
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            await channel.send(embed=arguments["embed"])
        except Exception as e:
            print(f"Error sending embed to {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "Embed sent."}

async def send_file(command_processor, arguments: Dict, data: Dict, websocket: websocket):
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            await channel.send(file=arguments["file"])
        except Exception as e:
            print(f"Error sending file to {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "File sent."}

async def send_files(command_processor, arguments: Dict, data: Dict, websocket: websocket):
    channel = await command_processor.discord.find_channel(data["channel"]["id"])
    if channel is not None:
        try:
            await channel.send(files=arguments["files"])
        except Exception as e:
            print(f"Error sending files to {channel.name} ({channel.id}): {e}")
    return {"success": True, "result": "Files sent."}