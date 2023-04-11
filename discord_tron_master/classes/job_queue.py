import asyncio
from collections import deque
from discord_tron_master.classes.job import Job
from typing import List
import logging
class JobQueue:
    def __init__(self, worker_id: str):
        self.queue = deque()
        self.in_progress = {}
        self.worker_id = worker_id
        self.terminate = False
        logging.debug("JobQueue initialized")

    async def put(self, job: Job):
        self.queue.append(job)
        logging.debug(f"Job {job.id} added to queue, queue size: {len(self.queue)}")

    async def stop(self):
        self.terminate = True

    async def get(self) -> Job:
        if self.terminate == True:
            logging.debug(f"Job Queue Terminating: {self.worker_id}")
            pass
        while len(self.queue) == 0:
            await asyncio.sleep(1)
            if self.terminate == True:
                logging.debug(f"Job Queue Terminating: {self.worker_id}")
                return
        logging.debug(f"Got job! Queue size: {len(self.queue)}")
        job = self.queue.popleft()
        self.in_progress[job.id] = job
        logging.debug(f"Job {job.id} retrieved from queue, now kept as self.in_progress: {self.in_progress}")
        return job

    # A function to view the current jobs in the queue, without removing them from the queue:
    def view(self) -> List[Job]:
        return list(self.queue)

    def done(self, job_id: int):
        if job_id in self.in_progress:
            del self.in_progress[job_id]
            logging.debug(f"Job {job_id} marked as done, removed from in progress")

    def qsize(self) -> int:
        return len(self.queue) + len(self.in_progress)

    def __len__(self) -> int:
        return self.qsize()
