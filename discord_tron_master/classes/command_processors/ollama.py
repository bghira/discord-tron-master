from discord_tron_master.classes.remote_ollama_broker import remote_ollama_broker


async def complete_result(command_processor, payload, data, websocket):
    message = data if isinstance(data, dict) else {}
    if not message and isinstance(payload, dict):
        message = payload
    return await remote_ollama_broker.complete_request(message)
