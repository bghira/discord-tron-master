import asyncio
import websockets
from discord_tron_master.auth import Auth
from discord_tron_master.models import User, OAuthToken


class WebSocketHub:
    def __init__(self, auth_instance: Auth):
        self.connected_clients = set()
        self.auth = auth_instance

    async def handler(self, websocket, path):
        access_token = websocket.request_headers.get("Authorization")
        token_type, access_token = access_token.split(' ', 1)
        if token_type.lower() != "bearer":
            # Invalid token type
            return

        if not access_token or not self.auth.validate_access_token(access_token):
            await websocket.close(code=4001, reason="Invalid access token")
            return
        self.connected_clients.add(websocket)
        try:
            async for message in websocket:
                await self.broadcast(message)
        finally:
            self.connected_clients.remove(websocket)

    async def broadcast(self, message):
        for client in self.connected_clients:
            await client.send(message)

    async def run(self, host="0.0.0.0", port=6789):
        server = await websockets.serve(self.handler, host, port)
        await server.wait_closed()
