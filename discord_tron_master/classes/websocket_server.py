import asyncio
import websockets
from typing import Dict, Any
from discord_tron_master.classes.queue_manager import QueueManager
from discord_tron_master.classes.worker_manager import WorkerManager
from discord_tron_master.classes.command_processor import CommandProcessor

class WebSocketServer:
    def __init__(self, host: str, port: int, ssl_context, command_processor: CommandProcessor):
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.command_processor = command_processor

    async def handler(self, websocket, path):
        try:
            async for message in websocket:
                import json
                data = json.loads(message)
                command = data["command"]
                payload = data.get("payload", {})
                await self.command_processor.process_command(command, payload)
        except websockets.exceptions.ConnectionClosedError:
            # Handle connection closed errors, if needed
            pass

    async def start(self):
        server = await websockets.serve(self.handler, self.host, self.port, ssl=self.ssl_context)
        await server.wait_closed()


async def main():
    # Initialize instances of required classes and start the WebSocket server
    queue_manager = QueueManager()
    worker_manager = WorkerManager()
    command_processor = CommandProcessor(queue_manager, worker_manager)
    websocket_server = WebSocketServer("0.0.0.0", 8080, None, command_processor)
    await websocket_server.start()


if __name__ == "__main__":
    asyncio.run(main())
