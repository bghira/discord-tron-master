import discord
# Update hardware information for the connecting machine.
async def update(command_processor, payload, data, websocket):
    # Update the hardware spec for this machine.
    message="A new worker is now connected to the websocket hub. Let's hope it sticks!"
    print(f"Discord? {command_processor.discord}")

    return {"success": True, "result": "no-op, currently no update is done."}
    pass