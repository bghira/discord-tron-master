import threading, logging, time, json
from typing import Callable, Dict, Any, List
from queue import Queue
from discord_tron_master.classes.job import Job

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
        # For stopping the Worker.
        self.terminate = False
        # Initialize as placeholders.
        self.worker_thread = None
        self.monitor_thread = None

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

    def monitor_worker(self):
        logging.debug(f"Beginning worker monitoring for worker {self.worker_id}")
        while True:
            if not self.worker_thread.is_alive() and not self.terminate:
                # Thread died, and worker is not set to terminate
                    worker_thread = threading.Thread(target=self.process_jobs)
                    worker_thread.start()
            elif self.terminate:
                logging.info("Worker is set to exit, and the time has come.")
                break
            # Sleep for a while before checking again
            time.sleep(10)

    def start_monitoring_thread(self):
        self.monitor_thread = threading.Thread(target=self.monitor_worker)
        self.monitor_thread.start()
