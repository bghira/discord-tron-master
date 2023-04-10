import asyncio
from collections import deque
from discord_tron_master.classes.job import Job
import logging
class JobQueue:
    def __init__(self):
        self.queue = deque()
        self.in_progress = {}
        logging.debug("JobQueue initialized")

    async def put(self, job: Job):
        self.queue.append(job)
        logging.debug(f"Job {job.id} added to queue, queue size: {len(self.queue)}")

    async def get(self) -> Job:
        while len(self.queue) == 0:
            logging.debug("Queue is empty, waiting...")
            await asyncio.sleep(1)
        job = self.queue.popleft()
        self.in_progress[job.id] = job
        logging.debug(f"Job {job.id} retrieved from queue, now in progress")
        return job

    def done(self, job_id: str):
        if job_id in self.in_progress:
            del self.in_progress[job_id]
            logging.debug(f"Job {job_id} marked as done, removed from in progress")

    def qsize(self) -> int:
        return len(self.queue) + len(self.in_progress)

    def __len__(self) -> int:
        return self.qsize()
