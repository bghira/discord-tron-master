from typing import Dict, Any, List
import logging, websocket, traceback
from discord_tron_master.classes.worker import Worker
from discord_tron_master.exceptions.auth import AuthError
from discord_tron_master.exceptions.registration import RegistrationError
from discord_tron_master.classes.job import Job

class WorkerManager:
    def __init__(self):
        self.workers = {}  # {"worker_id": <Worker>, ...}
        self.workers_by_capability = {
            "gpu": [],
            "compute": [],
            "memory": [],
            "llama": []
        }
        self.queue_manager = None

    def get_all_workers(self):
        return self.workers

    def get_worker(self, worker_id: str):
        if not worker_id or worker_id not in self.workers or not isinstance(self.workers[worker_id], Worker):
            logging.error(f"Tried accessing invalid worker {self.workers[worker_id]}, traceback: {traceback.format_exc()}")
            raise ValueError(f"Worker '{worker_id}' is not registered. Cannot retrieve details.")
        return self.workers[worker_id]

    def finish_job_for_worker(self, worker_id: str, job: Job):
        worker = self.get_worker(worker_id)
        worker.job_queue.done(job.job_id)

    async def finish_payload(self, command_processor, arguments: Dict, data: Dict, websocket):
        worker_id = arguments["worker_id"]
        job_id = arguments["job_id"]
        if worker_id and job_id:
            worker = self.get_worker(worker_id)
            worker.job_queue.done(job_id)
            logging.info("Finished job for worker " + worker_id)
            return {"status": "successfully finished job"}
        else:
            logging.error(f"Invalid data received: {data}")
            raise ValueError("Invalid payload received by finish_payload handler. We expect job_id and worker_id.")

    def find_worker_with_fewest_queued_tasks(self, job: Job):
        return self.find_worker_with_fewest_queued_tasks_by_job_type(job.job_type)

    def find_worker_with_fewest_queued_tasks_by_job_type(self, job_type: str, exclude_worker_id: str = None):
        job_type = job_type
        min_queued_tasks = float("inf")
        selected_worker = self.find_first_worker(job_type)
        for worker_id, worker in self.workers.items():
            if exclude_worker_id and worker_id == exclude_worker_id:
                logging.debug(f"Skipping worker {worker_id} because it is the excluded worker.")
                selected_worker = None
                continue
            logging.debug(f"worker_id: {worker_id}, worker: {worker}")
            if job_type in worker.supported_job_types and worker.supported_job_types[job_type] is True:
                logging.info(f"Found valid worker for {job_type} job")
                queued_tasks = self.queue_manager.worker_queue_length(worker)
                if queued_tasks < min_queued_tasks:
                    logging.debug(f"Found worker with fewer queued tasks: {queued_tasks} < {min_queued_tasks}")
                    min_queued_tasks = queued_tasks
                    selected_worker = worker
                else:
                    logging.debug(f"Worker {worker_id} has more or same queued tasks than current best: {queued_tasks} >= {min_queued_tasks}")                    
            else:
                logging.warn(f"Worker {worker_id} does not support job type {job_type}: {worker.supported_job_types}")
        
        return selected_worker

    def find_first_worker(self, job_type: str) -> Worker:
        capable_workers = self.workers_by_capability.get(job_type)
        if not capable_workers:
            logging.error(f"No workers capable of handling job type {job_type}")
            return None
        return capable_workers[0]

    def register_worker(self, worker_id: str, supported_job_types: List[str], hardware_limits: Dict[str, Any], hardware: Dict[str, Any]) -> Worker:
        if worker_id in self.workers:
            logging.error(f"Tried to register an already-registered worker: {worker_id}. Forcibly unregistering that worker.")
            self.unregister_worker(worker_id)
            raise RegistrationError(f"Worker '{worker_id}' is already registered. Cannot register again. Wait a bit, and then try again.")
        if not worker_id or worker_id == "":
            raise RegistrationError("Cannot register worker with blank worker_id.")
        logging.info(f"Registering a new worker, {worker_id}!")
        worker = Worker(worker_id, supported_job_types, hardware_limits, hardware, hardware["hostname"])
        self.workers[worker_id] = worker
        for job_type in supported_job_types:
            if supported_job_types[job_type] is True:
                self.workers_by_capability[job_type].append(worker)
        return worker

    async def unregister_worker(self, worker_id):
        worker = self.workers.pop(worker_id, None)
        if worker:
            supported_job_types = worker.supported_job_types
            for job_type in supported_job_types:
                # Remove worker from the list of workers_by_capability
                self.workers_by_capability[job_type] = [w for w in self.workers_by_capability[job_type] if w.worker_id != worker_id]
                
            await worker.stop()
        logging.info(f"After unregistering worker, we are left with: {self.workers} and {self.workers_by_capability}")


    def get_worker_supported_job_types(self, worker_id: str) -> List[str]:
        return self.workers[worker_id].supported_job_types

    def get_worker_hardware_limits(self, worker: Worker) -> Dict[str, Any]:
        if worker is None:
            return None
        return worker.hardware_limits

    def get_queue_lengths_by_worker(self) -> Dict[str, int]:
        return {worker_id: self.queue_manager.get_queue_length_by_worker(worker_id) for worker_id in self.workers}

    def get_queue_length_by_worker(self, worker: Worker) -> int:
        return worker.job_queue.qsize()

    def find_best_fit_worker(self, job: Job) -> Worker:
        # This is a possibility in the future to use a better system based on the task's resolution requirements, etc.
        #worker_with_best_hardware = self.find_best_hardware_for_job(worker_with_fewest_slots, job)

        # Logic to find the best fit worker based on job type and worker limits
        return self.find_worker_with_fewest_queued_tasks(job)

    def find_best_hardware_for_job(self, comparison_hardware: Worker, job: Job) -> Dict[str, Any]:
        # Logic to find the best hardware for a job based on job type and worker limits
        job_type = job.job_type
        if comparison_hardware is None:
            return None
        cmp_limits = comparison_hardware.hardware_limits
        for worker in self.workers_by_capability[job_type]:
            current_hw = worker.hardware_limits
            if current_hw["gpu"] >= cmp_limits["gpu"] and current_hw["cpu"] >= cmp_limits["cpu"] and current_hw["memory"] >= cmp_limits["memory"]:
                return worker


    def set_queue_manager(self, queue_manager):
        self.queue_manager = queue_manager

    async def register(self, command_processor, payload: Dict[str, Any], data: Dict, websocket: websocket) -> Dict:
        logging.debug("Registering worker via WebSocket")
        try:
            if "worker_id" in payload:
                worker_id = payload["worker_id"]
            elif "worker_id" in payload["arguments"]:
                worker_id = payload["arguments"]["worker_id"]
        except KeyError:
            logging.error(f"Worker ID not provided in payload: {payload}")
            return {"error": "Worker ID not provided in payload"}
        supported_job_types = payload["supported_job_types"]
        hardware_limits = payload["hardware_limits"]
        hardware = payload["hardware"]
        worker = self.register_worker(worker_id, supported_job_types, hardware_limits, hardware)
        self.queue_manager.register_worker(worker_id, supported_job_types)
        worker.set_job_queue(self.queue_manager.create_queue(worker))
        worker.set_websocket(websocket)
        await worker.start_monitoring()  # Use 'await' to call the async 'start_monitoring' method
        return {"success": True, "result": "Worker " + str(worker_id) + " registered successfully"}

    async def unregister(self, command_processor, payload: Dict[str, Any], data: Dict, websocket: websocket) -> Dict:
        logging.info("Unregistering worker for queued jobs")
        try:
            worker_id = payload["worker_id"]
        except KeyError:
            logging.error("Worker ID not provided in payload")
            return {"error": "Worker ID not provided in payload"}
        worker = self.workers.get(worker_id)
        worker.stop()
        await self.unregister_worker(worker_id)
        await self.queue_manager.unregister_worker(worker_id)
        logging.info("Successfully unregistered worker from queue manager.")
        return {"success": True, "result": "Worker " + str(worker_id) + " unregistered successfully"}
