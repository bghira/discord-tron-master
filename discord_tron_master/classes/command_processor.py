from discord_tron_master.classes.queue_manager import QueueManager
from discord_tron_master.classes.worker_manager import WorkerManager
from discord_tron_master.classes.message import WebsocketMessage
from discord_tron_master.exceptions.registration import RegistrationError
from typing import Dict, Any
from discord_tron_master.classes.command_processors import hardware
from discord_tron_master.classes.command_processors import discord as discord_module
import logging, json, websocket

class CommandProcessor:
    def __init__(self, queue_manager: QueueManager, worker_manager: WorkerManager, discord_bot):
        self.queue_manager = queue_manager
        self.queue_manager.set_worker_manager(worker_manager)
        self.worker_manager = worker_manager
        self.discord = discord_bot
        self.command_handlers = {
            "worker": {
                "register": worker_manager.register,
                "unregister": worker_manager.unregister,
            },
            "system": {
                "update": hardware.update
            },
            "message": {
                "send": discord_module.send_message,
                "send_large_message": discord_module.send_large_message,
                "create_thread": discord_module.create_thread,
                "send_image": discord_module.send_image,
                "edit": discord_module.edit_message,
                "delete": discord_module.delete_message,
                "delete_errors": discord_module.delete_previous_errors,
            },
            "job_queue": {
                "finish": self.worker_manager.finish_payload,
                "acknowledge": self.worker_manager.acknowledge_payload,
            }
            # Add more command handlers as needed
        }

    async def process_command(self, message: WebsocketMessage, websocket) -> None:
        try:
            command = message["module_command"]
            handler = self.command_handlers.get(message["module_name"], {}).get(command)
            if handler is None:
                # No handler found for the command
                logging.error(f"No handler found in module " + str(message["module_name"]) + " for command " + command + ", arguments: " + str(message["arguments"]))
                return
            logging.debug("Executing incoming " + str(handler) + " for module " + str(message["module_name"]) + ", command " + command + ", arguments: " + str(message["arguments"]) + ", data: " + str(message["data"]))
            # We pass "self" in so that it has access to our command processor.
            command_result = await handler(self, message["arguments"], message["data"], websocket)
            logging.debug(f"Command returned result, {command_result}")
            return command_result
        except RegistrationError as e:
            raise e
        except Exception as e:
            logging.error("Error processing command: " + str(e), exc_info=True)
            
            return json.dumps({"error": str(e)})
    # Add more command handler methods as needed
