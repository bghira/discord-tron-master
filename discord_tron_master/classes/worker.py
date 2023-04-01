import threading
from queue import Queue
from discord_tron_master.classes.job import Job

class Worker:
    def __init__(self, worker_id: str, supported_job_types: list[str]):
        self.worker_id = worker_id
        self.supported_job_types = supported_job_types
        self.job_queue = Queue()

    def add_job(self, job: Job):
        if job.job_type not in self.supported_job_types:
            raise ValueError(f"Unsupported job type: {job.job_type}")
        self.job_queue.put(job)

    def process_jobs(self):
        while True:
            job = self.job_queue.get()
            if job is None:
                break
            job.execute()

    def start(self):
        worker_thread = threading.Thread(target=self.process_jobs)
        worker_thread.start()
