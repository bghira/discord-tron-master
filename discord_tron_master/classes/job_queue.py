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
        self.item_added_event = asyncio.Event()  # Added an asyncio.Event
        logging.debug("JobQueue initialized")

    async def put(self, job: Job):
        self.queue.append(job)
        self.item_added_event.set()  # Set the event when an item is added
        logging.debug(f"Job {job.id} added to queue, queue size: {len(self.queue)}")

    async def stop(self):
        self.terminate = True

    async def get(self) -> Job:
        if self.terminate:
            logging.debug(f"Job Queue Terminating: {self.worker_id}")
            return None

        await self.item_added_event.wait()  # Wait for the event to be set
        self.item_added_event.clear()  # Clear the event after it's set

        if self.terminate:
            logging.debug(f"Job Queue Terminating: {self.worker_id}")
            return None

        logging.debug(f"Got job! Queue size: {len(self.queue)}")
        job = self.queue.popleft()
        self.in_progress[job.id] = job
        logging.debug(f"Job {job.id} retrieved from queue, now kept as self.in_progress: {self.in_progress}")
        return job

    # A function to view the current jobs in the queue, without removing them from the queue:
    def view(self) -> List[Job]:
        return list(self.queue) + list(self.in_progress.values())

    async def view_payloads(self) -> List[dict]:
        return [await job.format_payload() for job in self.view()]
    
    async def view_payload_prompts(self, truncate_length = 40):
        payload_prompts = ['`' + job['image_prompt'][:truncate_length] + '`' for job in await self.view_payloads()]
        # Return a string form of the payload_prompts list, with each item on a new line:
        if len(payload_prompts) == 0:
            return 'No jobs in queue.'
        output = ''
        for prompt in payload_prompts:
            output += prompt + '\n';
        return output

    def done(self, job_id: int):
        if job_id in self.in_progress:
            del self.in_progress[job_id]
            logging.debug(f"Job {job_id} marked as done, removed from in progress")

    def qsize(self) -> int:
        return len(self.queue) + len(self.in_progress)

    def __len__(self) -> int:
        return self.qsize()
