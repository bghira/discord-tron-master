from typing import Dict, Any, List
import logging
from discord_tron_master.classes.worker import Worker
from discord_tron_master.classes.job import Job

class WorkerManager:
    def __init__(self):
        self.workers = {}  # {"worker_id": {"supported_job_types": [...], "hardware_limits": {...}}, ...}
        self.workers_by_capability = {
            "gpu": [],
            "compute": [],
            "memory": [],
        }
        self.queue_manager = None

    def find_worker_with_fewest_queued_tasks(self, job_type):
        min_queued_tasks = float("inf")
        selected_worker_id = None
        for worker_id, worker_data in self.workers.items():
            if job_type in worker_data["supported_job_types"]:
                queued_tasks = self.queue_manager.get_queued_tasks(worker_id)
                if queued_tasks < min_queued_tasks:
                    min_queued_tasks = queued_tasks
                    selected_worker_id = worker_id
        return selected_worker_id

    def find_first_worker(self, job_type: str) -> str:
        capable_workers = self.workers_by_capability.get(job_type)
        if not capable_workers:
            logging.error(f"No workers capable of handling job type {job_type}")
            return None
        return capable_workers[0]

    def register_worker(self, worker_id: str, supported_job_types: List[str], hardware_limits: Dict[str, Any], hardware: Dict[str, Any]):
        logging.info("Run register_worker")
        worker = Worker(worker_id, supported_job_types, hardware_limits, hardware, hardware["hostname"])
        self.workers[worker_id] = worker
        for job_type in supported_job_types:
            self.workers_by_capability[job_type].append(worker)


    def unregister_worker(self, worker_id):
        worker_data = self.workers.pop(worker_id, None)
        if worker_data:
            supported_job_types = worker_data["supported_job_types"]
            for job_type in supported_job_types:
                self.workers_by_capability[job_type].remove(worker_id)
                self.workers.remove(worker_id)

    def get_worker_supported_job_types(self, worker_id: str) -> List[str]:
        return self.workers[worker_id].supported_job_types

    def get_worker_hardware_limits(self, worker: Worker) -> Dict[str, Any]:
        if worker is None:
            return None
        return worker.hardware_limits

    def get_queue_lengths_by_worker(self) -> Dict[str, int]:
        return {worker_id: self.queue_manager.get_queue_length_by_worker(worker_id) for worker_id in self.workers}

    def get_queue_length_by_worker(self, worker_id: str) -> int:
        return self.queue_manager.queues

    def find_best_fit_worker(self, job: Job) -> str:
        # Logic to find the best fit worker based on job type and worker limits
        worker_with_fewest_slots = self.find_worker_with_fewest_queued_tasks(job_type)
        # This is a possibility in the future to use a better system based on the task's resolution requirements, etc.
        #worker_with_best_hardware = self.find_best_hardware_for_job(worker_with_fewest_slots, job_type)

    def find_best_hardware_for_job(self, comparison_hardware: Worker, job_type: str) -> Dict[str, Any]:
        # Logic to find the best hardware for a job based on job type and worker limits
        if comparison_hardware is None:
            return None
        cmp_limits = comparison_hardware.hardware_limits
        for worker in self.workers_by_capability[job_type]:
            current_hw = worker.hardware_limits
            if current_hw["gpu"] >= cmp_limits["gpu"] and current_hw["cpu"] >= cmp_limits["cpu"] and current_hw["memory"] >= cmp_limits["memory"]:
                return worker


    def set_queue_manager(self, queue_manager):
        self.queue_manager = queue_manager

    async def register(self, payload: Dict[str, Any]) -> None:
        logging.info("Registering worker for queued jobs")
        try:
            worker_id = payload["worker_id"]
        except KeyError:
            logging.error(f"Worker ID not provided in payload: {payload}")
            return {"error": "Worker ID not provided in payload"}
        supported_job_types = payload["supported_job_types"]
        hardware_limits = payload["hardware_limits"]
        hardware = payload["hardware"]
        self.register_worker(worker_id, supported_job_types, hardware_limits, hardware)
        self.queue_manager.register_worker(worker_id, supported_job_types)
        return {"success": True, "result": "Worker " + str(worker_id) + " registered successfully"}

    async def unregister(self, payload: Dict[str, Any]) -> None:
        logging.info("Unregistering worker for queued jobs")
        try:
            worker_id = payload["worker_id"]
        except KeyError:
            logging.error("Worker ID not provided in payload")
            return {"error": "Worker ID not provided in payload"}
        self.unregister_worker(worker_id)
        self.queue_manager.unregister_worker(worker_id)
        logging.info("Successfully unregistered worker from queue manager.")
        return {"success": True, "result": "Worker " + str(worker_id) + " unregistered successfully"}
