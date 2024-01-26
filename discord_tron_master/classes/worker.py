import threading, logging, time, json
logging.basicConfig(level=logging.INFO)
from typing import Callable, Dict, Any, List
from asyncio import Queue
import asyncio
from discord_tron_master.classes.job import Job
from discord_tron_master.exceptions.registration import RegistrationError

class Worker:
    def __init__(self, worker_id: str, supported_job_types: List[str], hardware_limits: Dict[str, Any], hardware: Dict[str, Any], hostname: str = "Amnesiac"):
        self.worker_id = worker_id
        self.supported_job_types = supported_job_types
        self.hardware_limits = hardware_limits
        self.hardware = hardware
        self.hostname = hostname
        # For monitoring the Worker.
        self.running = True
        # For stopping the Worker.
        self.terminate = False
        # Initialize as placeholders.
        self.worker_thread = None
        self.monitor_thread = None
        self.worker_task = None
        # Jobs to assign
        self.job_queue = None
        # Jobs we have assigned (by job type)
        self.assigned_jobs = {}
        self.websocket = None

    def assign_job(self, job: Job):
        if job.job_type not in self.assigned_jobs:
            self.assigned_jobs[job.job_type] = []
        self.assigned_jobs[job.job_type].append(job)

    def complete_job(self, job: Job):
        if job.job_type not in self.assigned_jobs:
            return
        self.assigned_jobs[job.job_type].remove(job)

    def complete_job_by_id(self, job_id: str):
        for job_type, jobs in self.assigned_jobs.items():
            for job in jobs:
                if job.id == job_id:
                    self.assigned_jobs[job_type].remove(job)
                    self.job_queue.done(job_id)
                    return

    def list_assigned_jobs_by_type(self, job_type: str):
        if job_type not in self.assigned_jobs:
            return []
        return self.assigned_jobs[job_type]

    def can_assign_job_by_type(self, job_type: str):
        if job_type not in self.assigned_jobs:
            return True
        return False

    async def set_job_queue(self, job_queue: Queue):
        if str(self.worker_id) == "":
            raise RegistrationError("RegistrationError: Worker ID must be a string.")
        logging.info(f"Setting job queue for worker {self.worker_id}")
        self.job_queue = job_queue

    def set_websocket(self, websocket: Callable):
        self.websocket = websocket

    async def send_websocket_message(self, message: str):
        # If it's an array, we'll have to JSON dump it first:
        if isinstance(message, list):
            message = json.dumps(message)
        elif not isinstance(message, str):
            raise ValueError("Message must be a string or array.")
        logging.info("Sending job to worker")
        logging.debug(message)
        try:
            await self.websocket.send(message)
        except Exception as e:
            logging.error("Error sending websocket message: " + str(e))
            raise e

    def add_job(self, job: Job):
        if not self.job_queue:
            raise ValueError("Job queue not initialised yet.")
        if job.job_type not in self.supported_job_types:
            raise ValueError(f"Unsupported job type: {job.job_type}")
        logging.info("Adding " + job.job_type + " job to worker queue: " + job.id)
        
        self.job_queue.put(job)
        logging.info(f"Job queue size for worker {self.worker_id}: {self.job_queue.qsize()}")

    async def stop(self):
        self.terminate = True
        await self.job_queue.stop()
        await self.websocket.close(code=4002, reason="Worker is stopping due to deregistration request.")

    async def process_jobs(self):
        while not self.terminate:
            try:
                job = await self.job_queue.get()  # Use 'await' instead of synchronous call
                if self.can_assign_job_by_type(job_type=job.job_type):
                    self.assign_job(job)
                else:
                    # Wait async until we can assign
                    while not self.can_assign_job_by_type(job_type=job.job_type):
                        logging.info(f"Worker {self.worker_id} is busy. Waiting for job to be assigned.")
                        await asyncio.sleep(1)
                if job is None:
                    logging.info("Empty job submitted to worker!?")
                    break
                logging.info(f"Processing job {job.id} for worker {self.worker_id}")
                await job.execute()
            except Exception as e:
                import traceback
                from discord_tron_master.bot import clean_traceback
                logging.error(f"An error occurred while processing jobs for worker {self.worker_id}: {e}, traceback: {await clean_traceback(traceback.format_exc())}")
                await asyncio.sleep(1)  # Use 'await' for asynchronous sleep

    async def monitor_worker(self):
        logging.debug(f"Beginning worker monitoring for worker {self.worker_id}")
        while True:
            if self.worker_task is None or self.worker_task.done() and not self.terminate:
                # Task completed, and worker is not set to terminate
                self.worker_task = asyncio.create_task(self.process_jobs())
            elif self.terminate:
                logging.info("Worker is set to exit, and the time has come.")
                break
            # Sleep for a while before checking again
            await asyncio.sleep(10)

    async def start_monitoring(self):
        # Use 'asyncio.create_task' to run the 'process_jobs' and 'monitor_worker' coroutines
        self.worker_task = asyncio.create_task(self.process_jobs())
        self.monitor_task = asyncio.create_task(self.monitor_worker())
