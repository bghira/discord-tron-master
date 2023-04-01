import asyncio
from typing import Dict, List
from discord_tron_master.classes.worker_manager import WorkerManager

class QueueManager:
    def __init__(self, worker_manager: WorkerManager):
        self.queues = {}  # {"worker_id": {"queue": asyncio.Queue(), "supported_job_types": [...]}, ...}
        self.worker_manager = worker_manager

    def register_worker(self, worker_id, supported_job_types: List[str]):
        self.queues[worker_id] = {"queue": asyncio.Queue(), "supported_job_types": supported_job_types}

    def unregister_worker(self, worker_id):
        del self.queues[worker_id]

    def find_best_fit_worker(self, job_type: str) -> str:
        # Logic to find the best fit worker based on job type and worker limits
        pass

    async def enqueue_job(self, worker_id, job):
        await self.queues[worker_id]["queue"].put(job)

    async def dequeue_job(self, worker_id):
        return await self.queues[worker_id]["queue"].get()
