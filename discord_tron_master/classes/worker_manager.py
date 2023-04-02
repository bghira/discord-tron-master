from typing import Dict, Any, List
import logging

class WorkerManager:
    def __init__(self):
        self.workers = {}  # {"worker_id": {"supported_job_types": [...], "hardware_limits": {...}}, ...}
        # Allow retrieval of the workers by the task at hand.
        self.workers_by_capability = {
            "gpu": [],
            "compute": [],
            "memory": [],
        }
        self.queue_manager = None
    
    # Simply return the first worker for a job type, regardless of its queues.
    def find_first_worker(self, job_type: str) -> str:
        # Logic to find the best fit worker based on job type and worker limits
        capable_workers = self.workers_by_capability[job_type]
        if capable_workers is None:
            # No workers capable of handling this job type. uhoh
            logging.error(f"No workers capable of handling job type {job_type}")
            return None
        return capable_workers.first()

    # Return the worker with the fewest number of jobs in its queue.
    def find_least_busy_worker(self, job_type: str) -> str:
        # Logic to find the best fit worker based on job type and worker limits
        capable_workers = self.workers_by_capability[job_type]
        if capable_workers is None:
            # No workers capable of handling this job type. uhoh
            logging.error(f"No workers capable of handling job type {job_type}")
            return None
        # Find the worker with the fewest jobs in its queue.

    def register_worker(self, worker_id, supported_job_types: List[str], hardware_limits: Dict[str, Any]):
        self.workers[worker_id] = {"supported_job_types": supported_job_types, "hardware_limits": hardware_limits}
        for job_type in supported_job_types:
            self.workers_by_capability[job_type].add(job_type)

    def unregister_worker(self, worker_id):
        del self.workers[worker_id]

    def get_worker_supported_job_types(self, worker_id: str) -> List[str]:
        return self.workers[worker_id]["supported_job_types"]

    def get_worker_hardware_limits(self, worker_id: str) -> Dict[str, Any]:
        return self.workers[worker_id]["hardware_limits"]

    def find_best_fit_worker(self, job_type: str) -> str:
        # Logic to find the best fit worker based on job type and worker limits
        pass
    def set_queue_manager(self, queue_manager):
        self.queue_manager = queue_manager

    # API-friendly variant of register command. Accepts arguments as a dict.
    async def register(self, payload: Dict[str, Any]) -> None:
        logging.info("Registering worker for queued jobs")
        try:
            worker_id = payload["worker_id"]
        except KeyError:
            logging.error(f"Worker ID not provided in payload: {payload}")
            return {"error": "Worker ID not provided in payload"}
        supported_job_types = payload["supported_job_types"]
        hardware_limits = payload["hardware_limits"]
        self.register_worker(worker_id, supported_job_types, hardware_limits)
        self.queue_manager.register_worker(worker_id, supported_job_types)
        return {"success": True, "result": "Worker " + str(worker_id) + " registered successfully"}

    # API-friendly variant of unregister command. Accepts arguments as a dict.
    async def unregister(self, payload: Dict[str, Any]) -> None:
        logging.info("Unregistering worker for queued jobs")
        try:
            from discord_tron_master.classes.app_config import AppConfig
            config = AppConfig()
            worker_id = payload["worker_id"]
        except KeyError:
            logging.error("Worker ID not provided in payload")
            return {"error": "Worker ID not provided in payload"}
        self.unregister_worker(worker_id)
        self.queue_manager.unregister_worker(worker_id)
        logging.info("Successfully unregistered worker from queue manager.")
        return { "success": True, "result": "Worker " + str(worker_id) + " unregistered successfully" }
