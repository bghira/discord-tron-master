import logging, json
import websockets
from discord_tron_master.auth import Auth
from discord_tron_master.models import User, OAuthToken
from discord_tron_master.classes.command_processor import CommandProcessor

class WebSocketHub:
    def __init__(self, auth_instance: Auth, command_processor: CommandProcessor, discord_bot):
        self.connected_clients = set()
        self.auth = auth_instance
        self.command_processor = command_processor
        self.queue_manager = None
        self.worker_manager = None
        self.discord = discord_bot

    async def set_queue_manager(self, queue_manager):
        self.queue_manager = queue_manager
        
    async def set_worker_manager(self, worker_manager):
        self.worker_manager = worker_manager

    async def handler(self, websocket, path):
        access_token = websocket.request_headers.get("Authorization")
        token_type, access_token = access_token.split(' ', 1)
        if token_type.lower() != "bearer":
            # Invalid token type
            return

        if not access_token or not self.auth.validate_access_token(access_token):
            await websocket.close(code=4001, reason="Invalid access token")
            return
        # Add the client to the set of clients
        self.connected_clients.add(websocket)
        try:
            # Process incoming messages
            async for message in websocket:
                decoded = json.loads(message)
                logging.info(f"Received message from {websocket.remote_address}: {decoded}")
                if "worker_id" in decoded["arguments"]:
                    worker_id = decoded["arguments"]["worker_id"]
                    logging.info("Worker ID found in message. Updating worker ID to " + str(worker_id) + ".")
                print("Command processor instance:", self.command_processor)
                raw_result = await self.command_processor.process_command(decoded, websocket)
                result = json.dumps(raw_result)
                # Did result error? If so, close the websocket connection:
                if raw_result is None or "error" in raw_result:
                    if raw_result is None:
                        raw_result = "No result was received. No execution occurred. Fuck right off!"
                    await websocket.close(code=4002, reason=raw_result)
                    return
                logging.info(f"Sending message to {websocket.remote_address}: {result}")
                await websocket.send(result)
        finally:
            # Remove the client from the set of clients
            logging.info("Removing client from connected clients")
            self.connected_clients.remove(websocket)
            # Check if the worker is registered, and if so, unregister it
            if worker_id:
                self.queue_manager.unregister_worker(worker_id)
                self.worker_manager.unregister_worker(worker_id)


    async def broadcast(self, message):
        for client in self.connected_clients:
            await client.send(message)

    async def run(self, host="0.0.0.0", port=6789):
        server = websockets.serve(self.handler, host, port)
        await server