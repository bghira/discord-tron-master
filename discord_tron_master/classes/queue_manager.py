import asyncio, logging
from asyncio import Queue
logging.basicConfig(level=logging.DEBUG)
from typing import Dict, List
from discord_tron_master.classes.worker_manager import WorkerManager
from discord_tron_master.classes.worker import Worker
from discord_tron_master.classes.job import Job

class QueueManager:
    def __init__(self, worker_manager: WorkerManager):
        self.queues = {}  # {"worker_id": {"queue": asyncio.Queue(), "supported_job_types": [...]}, ...}
        self.worker_manager = worker_manager

    def set_worker_manager(self, worker_manager):
        self.worker_manager = worker_manager

    def get_all_queues(self) -> Dict[str, List[str]]:
        return {worker_id: self.get_queue_contents_by_worker(worker_id) for worker_id in self.queues}

    # Remove all jobs from a given worker
    def remove_jobs_by_worker(self, worker_id):
        self.queues[worker_id]["queue"].queue.clear()

    # Get supported job types that can currently be processed by registered workers
    def get_supported_job_types(self) -> List[str]:
        job_types = set()
        for worker_info in self.queues.values():
            job_types.update(worker_info["supported_job_types"])
        return list(job_types)

    # Get all queued tasks by job type
    def get_queues_by_job_type(self, job_type) -> List[str]:
        return [worker_id for worker_id, worker_info in self.queues.items() if job_type in worker_info["supported_job_types"]]

    # Remove all jobs of a given type from all workers
    def remove_jobs_by_job_type(self, job_type):
        for worker_info in self.queues.values():
            if job_type in worker_info["supported_job_types"]:
                worker_info["queue"].queue.clear()

    def register_worker(self, worker_id, supported_job_types: List[str]):
        self.queues[worker_id] = {"queue": asyncio.Queue(), "supported_job_types": supported_job_types}

    def worker_queue_length(self, worker: Worker):
        try:
            worker_id = worker.worker_id
            return self.queues[worker_id]["queue"].qsize()
        except:
            logging.error("Error retrieving the queue length for worker '" + str(worker_id) + "'")
            return -1

    def unregister_worker(self, worker_id):
        del self.queues[worker_id]

    def queue_by_worker(self, worker: Worker) -> Queue:
        return self.queues.get(worker.worker_id, None).get("queue", asyncio.Queue())

    def queue_contents_by_worker(self, worker_id):
        return self.queues.get(worker_id, None).get("queue", asyncio.Queue())

    async def enqueue_job(self, worker: Worker, job: Job):
        worker_id = worker.worker_id
        job.set_worker(worker)
        await self.queues[worker_id]["queue"].put(job)

    async def dequeue_job(self, worker: Worker):
        worker_id = worker.worker_id
        return await self.queues[worker_id]["queue"].get()
