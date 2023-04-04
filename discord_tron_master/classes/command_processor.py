from discord_tron_master.classes.queue_manager import QueueManager
from discord_tron_master.classes.worker_manager import WorkerManager
from discord_tron_master.classes.message import WebsocketMessage
from typing import Dict, Any
from discord_tron_master.classes.command_processors import hardware
import logging, json, websocket

class CommandProcessor:
    def __init__(self, queue_manager: QueueManager, worker_manager: WorkerManager):
        self.queue_manager = queue_manager
        self.queue_manager.set_worker_manager(worker_manager)
        self.worker_manager = worker_manager
        self.command_handlers = {
            "worker": {
                "register": worker_manager.register,
                "unregister": worker_manager.unregister,
            },
            "system": {
                "update": hardware.Hardware.update
            }
            # Add more command handlers as needed
        }

    async def process_command(self, message: WebsocketMessage, websocket: websocket) -> None:
        try:
            logging.info("Entered process_command via WebSocket")
            command = message["module_command"]
            handler = self.command_handlers.get(message["module_name"]).get(command)
            if handler is None:
                # No handler found for the command
                logging.error(f"No handler found in module " + str(message["module_name"]) + " for command " + command + ", payload: " + str(message["arguments"]))
                return
            logging.info("Executing incoming " + str(handler) + " for module " + str(message["module_name"]) + ", command " + command + ", payload: " + str(message["arguments"]))
            return await handler(message["arguments"], websocket)
        except Exception as e:
            logging.error("Error processing command: " + str(e))
            
            return json.dumps({"error": str(e)})
    # Add more command handler methods as needed
