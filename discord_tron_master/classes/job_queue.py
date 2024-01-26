import asyncio
from collections import deque
from discord_tron_master.classes.job import Job
from typing import List
import logging

logger = logging.getLogger('JobQueue')
logger.setLevel('DEBUG')
class JobQueue:
    def __init__(self, worker_id: str):
        self.queue = deque()
        self.in_progress = {}
        self.worker_id = worker_id
        self.terminate = False
        self.item_added_event = asyncio.Event()  # Added an asyncio.Event
        logger.debug("JobQueue initialized")

    async def set_queue_from_list(self, job_list: List[Job]):
        self.queue = deque(job_list)
        logger.debug(f"JobQueue set from list, queue size: {len(self.queue)}")

    async def put(self, job: Job):
        self.queue.append(job)
        self.item_added_event.set()  # Set the event when an item is added
        logger.debug(f"Job {job.id} added to queue, queue size: {len(self.queue)}")

    async def stop(self):
        self.terminate = True

    async def get(self) -> Job:
        if self.terminate:
            logger.debug(f"Job Queue Terminating: {self.worker_id}")
            return None

        await self.item_added_event.wait()  # Wait for the event to be set
        self.item_added_event.clear()  # Clear the event after it's set

        if self.terminate:
            logger.debug(f"Job Queue Terminating: {self.worker_id}")
            return None

        logger.debug(f"Got job! Queue size: {len(self.queue)}")
        if len(self.queue) == 0:
            logger.debug("Queue is empty, returning None")
            return None
        job = self.queue.popleft()
        self.in_progress[job.id] = job
        logger.debug(f"Job {job.id} retrieved from queue, now kept as self.in_progress: {self.in_progress}")
        return job

    async def remove(self, job: Job):
        return self.done(job.id)

    # A function to view the current jobs in the queue, without removing them from the queue:
    def view(self) -> List[Job]:
        return list(self.queue) + list(self.in_progress.values())

    async def view_payloads(self) -> List[dict]:
        return [await job.format_payload() for job in self.view()]
    
    async def view_payload_prompts(self, truncate_length = 40):
        payload_prompts = [job["job_type"] + ' job: `' + job["prompt"][:truncate_length] + '`, id: `' + job['job_id'] + '`' for job in await self.view_payloads()]
        # Return a string form of the payload_prompts list, with each item on a new line:
        if len(payload_prompts) == 0:
            return 'No jobs in queue.'
        output = ''
        for prompt in payload_prompts:
            output += prompt + '\n';
        return output

    def done(self, job_id: int):
        logger.info(f"(JobQueue.done) received job_id: {job_id}. We have {len(self.in_progress)} jobs in progress.")
        for job in list(self.queue):
            logger.info(f"(JobQueue.done) Looking for job {job_id} in queue: {job.id}")
            if job_id == job.id:
                self.queue.remove(job)
                logger.debug(f"(JobQueue.done) Job {job.id} removed from queue")
        logger.info(f"(JobQueue.done) Looking for job id {job_id} in in_progress: {self.in_progress}")
        if job_id in self.in_progress:
            del self.in_progress[job_id]
            logger.debug(f"(JobQueue.done) Job {job_id} marked as done, removed from in progress")

    def qsize(self) -> int:
        return len(self.queue) + len(self.in_progress)

    def __len__(self) -> int:
        return self.qsize()
