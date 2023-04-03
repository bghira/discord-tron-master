import threading, logging, time
from typing import Callable, Dict, Any, List
from queue import Queue
from discord_tron_master.classes.job import Job
from discord_tron_master.classes.worker import Worker


class Worker:
    def __init__(self, worker_id: str, supported_job_types: List[str], hardware_limits: Dict[str, Any], hardware: Dict[str, Any], hostname: str = "Amnesiac"):
        self.worker_id = worker_id
        self.supported_job_types = supported_job_types
        self.hardware_limits = hardware_limits
        self.hardware = hardware
        self.hostname = hostname
        self.job_queue = Queue()

        # For monitoring the Worker.
        self.running = True
        self.terminate = False
        self.worker_thread = None

    def add_job(self, job: Job):
        if job.job_type not in self.supported_job_types:
            raise ValueError(f"Unsupported job type: {job.job_type}")
        self.job_queue.put(job)

    def process_jobs(self):
        while not self.terminate:
            try:
                job = self.job_queue.get()
                if job is None:
                    break
                job.execute()
            except Exception as e:
                logging.error(f"An error occurred while processing jobs for worker {self.worker_id}: {e}")
                time.sleep(1)
    def stop(self):
        self.terminate = True

    def start(self):
        self.worker_thread = threading.Thread(target=self.process_jobs)
        self.worker_thread.start()

    def monitor_worker(worker: Worker, worker_thread: threading.Thread, restart_condition: Callable[[], bool]):
        while True:
            # Check if the thread is alive and the worker is running
            if not worker_thread.is_alive() or not worker.running:
                if restart_condition():
                    # Stop the worker if it's still running
                    if worker.running:
                        worker.stop()

                    # Wait for the thread to finish
                    worker_thread.join()

                    # Create a new worker instance and start a new thread
                    worker = Worker(...)
                    worker_thread = threading.Thread(target=worker.process_jobs)
                    worker_thread.start()

            # Sleep for a while before checking again
            time.sleep(MONITOR_INTERVAL)