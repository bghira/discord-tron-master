import asyncio
from collections import deque
from discord_tron_master.classes.job import Job

class JobQueue:
    def __init__(self):
        self.queue = deque()
        self.in_progress = {}

    async def put(self, job: Job):
        self.queue.append(job)

    async def get(self) -> Job:
        while len(self.queue) == 0:
            await asyncio.sleep(1)
        job = self.queue.popleft()
        self.in_progress[job.id] = job
        return job

    def done(self, job_id: str):
        if job_id in self.in_progress:
            del self.in_progress[job_id]

    def qsize(self) -> int:
        return len(self.queue) + len(self.in_progress)

    def __len__(self) -> int:
        return self.qsize()