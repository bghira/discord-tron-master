from discord_tron_master.classes.queue_manager import QueueManager
from discord_tron_master.classes.worker_manager import WorkerManager
from typing import Dict, Any

class CommandProcessor:
    def __init__(self, queue_manager: QueueManager, worker_manager: WorkerManager):
        self.queue_manager = queue_manager
        self.worker_manager = worker_manager
        self.command_handlers = {
            "register_worker": self.register_worker,
            "unregister_worker": self.unregister_worker,
            # Add more command handlers as needed
        }

    async def process_command(self, command: str, payload: Dict[str, Any]) -> None:
        handler = self.command_handlers.get(command)
        if handler is None:
            # No handler found for the command
            return

        await handler(payload)

    async def register_worker(self, payload: Dict[str, Any]) -> None:
        worker_id = payload["worker_id"]
        supported_job_types = payload["supported_job_types"]
        hardware_limits = payload["hardware_limits"]
        self.worker_manager.register_worker(worker_id, supported_job_types, hardware_limits)
        self.queue_manager.register_worker(worker_id)

    async def unregister_worker(self, payload: Dict[str, Any]) -> None:
        worker_id = payload["worker_id"]
        self.worker_manager.unregister_worker(worker_id)
        self.queue_manager.unregister_worker(worker_id)

    # Add more command handler methods as needed
