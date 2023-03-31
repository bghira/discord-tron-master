import asyncio
import websockets

class WebSocketHub:
    def __init__(self):
        self.connected_clients = set()

    async def handler(self, websocket, path):
        self.connected_clients.add(websocket)
        try:
            async for message in websocket:
                await self.broadcast(message)
        finally:
            self.connected_clients.remove(websocket)

    async def broadcast(self, message):
        for client in self.connected_clients:
            await client.send(message)

    def run(self, host='0.0.0.0', port=6789):
        start_server = websockets.serve(self.handler, host, port)
        asyncio.get_event_loop().run_until_complete(start_server)
        asyncio.get_event_loop().run_forever()
