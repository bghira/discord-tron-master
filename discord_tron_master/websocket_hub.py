import logging, json, asyncio
import websockets
from discord_tron_master.auth import Auth
from discord_tron_master.exceptions.auth import AuthError
from discord_tron_master.exceptions.registration import RegistrationError
from discord_tron_master.models import User, OAuthToken
from discord_tron_master.classes.command_processor import CommandProcessor
from discord_tron_master.classes.app_config import AppConfig

class WebSocketHub:
    def __init__(self, auth_instance: Auth, command_processor: CommandProcessor, discord_bot):
        self.connected_clients = set()
        self.auth = auth_instance
        self.config = AppConfig()
        self.command_processor = command_processor
        self.queue_manager = None
        self.worker_manager = None
        self.discord = discord_bot

    async def set_queue_manager(self, queue_manager):
        self.queue_manager = queue_manager
        
    async def set_worker_manager(self, worker_manager):
        self.worker_manager = worker_manager

    async def handler(self, websocket, path):
        logging.debug(f"Hitting WebSocket Handler..")
        worker_id = None
        access_token = websocket.request_headers.get("Authorization")
        token_type, access_token = access_token.split(' ', 1)
        if token_type.lower() != "bearer":
            # Invalid token type
            return

        if not access_token or not self.auth.validate_access_token(access_token):
            await websocket.close(code=4001, reason="Invalid access token")
            logging.error(f"Client provided invalid access token on WebSocket hub: {access_token}")
            return
        # Add the client to the set of clients
        self.connected_clients.add(websocket)
        try:
            # Process incoming messages
            async for message in websocket:
                logging.debug(f"Received message from {websocket.remote_address}: {message}")
                decoded = json.loads(message)
                if "worker_id" in decoded["arguments"]:
                    worker_id = decoded["arguments"]["worker_id"]
                    logging.info("Worker ID found in message. Updating worker ID to " + str(worker_id) + ".")
                raw_result = await self.command_processor.process_command(decoded, websocket)
                result = json.dumps(raw_result)
                logging.debug(f"Raw result: {raw_result}")
                logging.debug(f"JSON result: {result}")
                # Did result error? If so, close the websocket connection:
                if raw_result is not None and ("RegistrationError" in raw_result or "error" in raw_result and "RegistrationError" in raw_result["error"]):
                    await websocket.close(code=4002, reason="RegistrationError:" + raw_result)
                    return
                if raw_result is None or "error" in raw_result:
                    if raw_result is None:
                        raw_result = "No result was received. No execution occurred. Fuck right off!"
                        logging.error(f"Client requested some impossible task: {decoded}\nThe result was: {result}")
                logging.debug(f"Sending message to {websocket.remote_address}: {result}")
                await websocket.send(result)
        except AuthError as e:
            logging.error(f"Client sent invalid auth credentials. Naughty!")
            await websocket.close(code=4002, reason=str(e))
            return
        except RegistrationError as e:
            logging.error(f"Sending registration error to worker: {str(e)}")
            await websocket.close(code=4002, reason=str(e))
            return
        except asyncio.exceptions.IncompleteReadError as e:
            logging.warning(f"IncompleteReadError: {e}")
            # ... handle the situation as needed
        except websockets.exceptions.ConnectionClosedError as e:
            logging.warning(f"ConnectionClosedError: {e}")
            # ... handle the situation as needed
        except Exception as e:
            import traceback
            logging.error(f"Unhandled exception in handler: {e}, traceback: {traceback.format_exc()}")
        finally:
            # Remove the client from the set of clients
            logging.info(f"Removing worker {worker_id} from connected clients")
            self.connected_clients.discard(websocket)
            # Check if the worker is registered, and if so, unregister it
            if worker_id:
                try:
                    logging.warn("Removing worker from the QueueManager")
                    await self.queue_manager.unregister_worker(worker_id)
                except Exception as e:
                    logging.error(f"Error unregistering worker {worker_id} from QueueManager: {e}")
                try:
                    logging.warn("Removing worker from the WorkerManager")
                    await self.worker_manager.unregister_worker(worker_id)
                except Exception as e:
                    logging.error(f"Error unregistering worker {worker_id} from WorkerManager: {e}")
        await asyncio.sleep(5)


    async def broadcast(self, message):
        for client in self.connected_clients:
            await client.send(message)

    async def run(self, host="0.0.0.0", port=6789):
        logging.info(f"Running WebSocket Hub!")
        try:
            import ssl
            ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_context.load_cert_chain(self.config.project_root + '/config/server_cert.pem', self.config.project_root + '/config/server_key.pem')
            # Set the correct SSL/TLS version (You can change PROTOCOL_TLS to the appropriate version if needed)
            ssl_context.options |= ssl.OP_NO_SSLv2 | ssl.OP_NO_SSLv3 | ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
            websocket_logger = logging.getLogger('websockets')
            websocket_logger.setLevel(logging.WARNING)
            server = websockets.serve(self.handler, host, port, max_size=31554432, ssl=ssl_context, ping_timeout=300, ping_interval=2)
            await server
            logging.warn("Server exited.")
        except Exception as e:
            import traceback
            logging.error(f"Error running WebSocket Hub: {e}, traceback: {traceback.print_exc()}")
            exit(1)
