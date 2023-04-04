# Update hardware information for the connecting machine.

class Hardware:
    async def update(self, websocket):
        # Update the hardware spec for this machine.
        return {"success": True, "result": "no-op, currently no update is done."}
        pass