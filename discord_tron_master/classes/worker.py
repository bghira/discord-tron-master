import threading, logging, time, json
from typing import Callable, Dict, Any, List
from asyncio import Queue
import asyncio
from discord_tron_master.classes.job import Job

class Worker:
    def __init__(self, worker_id: str, supported_job_types: List[str], hardware_limits: Dict[str, Any], hardware: Dict[str, Any], hostname: str = "Amnesiac"):
        self.worker_id = worker_id
        self.supported_job_types = supported_job_types
        self.hardware_limits = hardware_limits
        self.hardware = hardware
        self.hostname = hostname

        self.job_queue = None

        # For monitoring the Worker.
        self.running = True
        # For stopping the Worker.
        self.terminate = False
        # Initialize as placeholders.
        self.worker_thread = None
        self.monitor_thread = None
        self.worker_task = None

    def set_job_queue(self, job_queue: Queue):
        self.job_queue = job_queue

    def set_websocket(self, websocket: Callable):
        self.websocket = websocket

    def send_websocket_message(self, message: str):
        # If it's an array, we'll have to JSON dump it first:
        if isinstance(message, list):
            message = json.dumps(message)
        elif not isinstance(message, str):
            raise ValueError("Message must be a string or array.")
        logging.debug("Worker object yeeting a websocket message to oblivion: " + message)
        try:
            self.websocket.send(message)
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

    def stop(self):
        self.terminate = True

    async def process_jobs(self):
        while not self.terminate:
            try:
                job = await self.job_queue.get()  # Use 'await' instead of synchronous call
                if job is None:
                    break
                job.execute()
            except Exception as e:
                logging.error(f"An error occurred while processing jobs for worker {self.worker_id}: {e}")
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
