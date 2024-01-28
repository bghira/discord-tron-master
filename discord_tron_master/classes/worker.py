import threading, logging, time, json
from typing import Callable, Dict, Any, List
from asyncio import Queue
import asyncio
from discord_tron_master.classes.job import Job
from discord_tron_master.exceptions.registration import RegistrationError

logger = logging.getLogger('Worker')
logger.setLevel('DEBUG')
class Worker:
    def __init__(self, worker_id: str, supported_job_types: List[str], hardware_limits: Dict[str, Any], hardware: Dict[str, Any], hostname: str = "Amnesiac"):
        self.worker_id = worker_id
        self.supported_job_types = supported_job_types
        self.hardware_limits = hardware_limits
        self.hardware = hardware
        self.hostname = hostname
        # For monitoring the Worker.
        self.running = True
        # For stopping the Worker.
        self.terminate = False
        # Initialize as placeholders.
        self.worker_thread = None
        self.monitor_thread = None
        self.worker_task = None
        # Jobs to assign
        self.job_queue = None
        # Jobs we have assigned (by job type)
        self.assigned_jobs = {}
        self.websocket = None

    def assign_job(self, job: Job):
        if job.job_type not in self.assigned_jobs:
            self.assigned_jobs[job.job_type] = []
        self.assigned_jobs[job.job_type].append(job)

    async def acknowledge_job(self, job_id: str) -> Job:
        if self.job_queue is None:
            logger.warning("Job queue not initialised yet. Can not acknowledge job.")
            return False
        job = await self.get_assigned_job_by_id(job_id)
        if job is None:
            logger.warning("Job did not exist. Can not acknowledge job.")
            return False
        if job.is_acknowledged()[0]:
            logger.warning("Job is already acknowledged. Can not acknowledge job.")
            return False
        logger.info(f"Job {job_id} is acknowledged by the remote side.")
        job.acknowledge()

        return True

    def complete_job(self, job: Job):
        if job.job_type not in self.assigned_jobs:
            return
        self.assigned_jobs[job.job_type].remove(job)
        self.job_queue.done(job.id)

    def complete_job_by_id(self, job_id: str):
        for job_type, jobs in self.assigned_jobs.items():
            for job in jobs:
                if job.id == job_id:
                    self.assigned_jobs[job_type].remove(job)
                    self.job_queue.done(job_id)
                    return

    async def get_assigned_job_by_id(self, job_id: str) -> Job:
        for job_type, jobs in self.assigned_jobs.items():
            for job in jobs:
                if job.id == job_id:
                    return job
        return None

    def list_assigned_jobs_by_type(self, job_type: str):
        if job_type not in self.assigned_jobs:
            return []
        return self.assigned_jobs[job_type]

    def can_assign_job_by_type(self, job_type: str):
        if (
            self.job_queue is not None
            and self.job_queue.in_progress is not None
            and len(self.job_queue.in_progress) > 0
            ):
            logger.debug(f"Worker {self.worker_id} is busy, in_progress has {len(self.job_queue.in_progress)} items in-flight.")
            return False
        if len(self.assigned_jobs.get(job_type, [])) > 1:
            assigned_jobs_output = [
            {
                "id": job.id,
                "executed": job.executed,
                "executed_date": job.executed_date,
                "migrated": job.migrated,
                "migrated_date": job.migrated_date,
                "acknowledged": job.acknowledged,
                "acknowledged_date": job.acknowledged_date
            } for job in self.assigned_jobs.get(job_type, [])]
            logger.debug(f"Instead of in progress, we detected assigned jobs in Worker: {self.worker_id} for job type {job_type}: {assigned_jobs_output}")
            return False
        return True

    def are_jobs_acknowledged(self):
        """
        Determine whether all of the in_progress jobs have been acknowledge()d.
        
        Returns True if all jobs are acknowledged, False if not.
        """
        return all([job.is_acknowledged()[0] for job in self.job_queue.in_progress.values()])

    async def set_job_queue(self, job_queue: Queue):
        if str(self.worker_id) == "":
            raise RegistrationError("RegistrationError: Worker ID must be a string.")
        logger.info(f"Setting job queue for worker {self.worker_id}")
        self.job_queue = job_queue

    def set_websocket(self, websocket: Callable):
        self.websocket = websocket

    async def send_websocket_message(self, message: str):
        # If it's an array, we'll have to JSON dump it first:
        if isinstance(message, list):
            message = json.dumps(message)
        elif not isinstance(message, str):
            raise ValueError("Message must be a string or array.")
        logger.info("Sending job to worker")
        logger.debug(message)
        try:
            await self.websocket.send(message)
        except Exception as e:
            logger.error("Error sending websocket message: " + str(e))
            raise e

    def add_job(self, job: Job):
        if not self.job_queue:
            raise ValueError("Job queue not initialised yet.")
        if job.job_type not in self.supported_job_types:
            raise ValueError(f"Unsupported job type: {job.job_type}")
        logger.info("Adding " + job.job_type + " job to worker queue: " + job.id)
        
        self.job_queue.put(job)
        logger.info(f"Job queue size for worker {self.worker_id}: {self.job_queue.qsize()}")

    async def stop(self):
        self.terminate = True
        await self.job_queue.stop()
        await self.ack_task.stop()
        await self.monitor_task.stop()
        await self.worker_task.stop()
        await self.websocket.close(code=4002, reason="Worker is stopping due to deregistration request.")

    async def process_jobs(self):
        logger.debug(f"(Worker.process_jobs) Begin function.")
        while not self.terminate:
            try:
                logger.debug(f"(Worker.process_jobs) Begin loop. Fetching preview task.")
                test_job = await self.job_queue.preview()  # Use 'await' instead of synchronous call
                if test_job is None:
                    logger.debug(f"(Worker.process_jobs) No job to process for worker {self.worker_id}")
                    await asyncio.sleep(1)
                    continue
                logger.debug(f"(Worker.process_jobs) Preview task: {test_job}")
                if self.can_assign_job_by_type(job_type=test_job.job_type):
                    logger.debug(f"(Worker.process_jobs) Worker {self.worker_id} can assign job {test_job.id}. Queue type: {type(self.job_queue)}")
                    job = await self.job_queue.get()  # Use 'get()' to pull the job from the queue and pop it out.
                    self.assign_job(job)
                    logger.debug(f"(Worker.process_jobs) Worker {self.worker_id} assigned job {job.id}.")
                elif test_job in self.assigned_jobs.get(test_job.job_type, []):
                    # Job is already assigned. Wait for it to complete.
                    logger.debug(f"(Worker.process_jobs) Worker {self.worker_id} is already processing job {test_job.id}.")
                    await asyncio.sleep(1)
                    continue
                else:
                    # Wait async until we can assign
                    while not self.can_assign_job_by_type(job_type=test_job.job_type):
                        logger.info(f"(Worker.process_jobs) Worker {self.worker_id} is busy. Waiting for job to be assigned.")
                        for job_type, jobs in self.assigned_jobs.items():
                            assigned_jobs_output = [
                            {
                                "id": job.id,
                                "executed": job.executed,
                                "executed_date": job.executed_date,
                                "migrated": job.migrated,
                                "migrated_date": job.migrated_date,
                                "acknowledged": job.acknowledged,
                                "acknowledged_date": job.acknowledged_date
                            } for job in jobs]
                        logger.debug(f"(Worker.process_jobs) Worker {self.worker_id} assigned jobs: {assigned_jobs_output}")
                        if self.are_jobs_acknowledged():
                            logger.debug(f"All jobs have been acknowledged. Waiting cleanly.")
                            await asyncio.sleep(1)
                            continue
                        logger.debug(f"Not all jobs have been acknowledged: {[job.id for job in self.job_queue.in_progress.values() if not job.is_acknowledged()[0]]}")
                        for job in self.assigned_jobs.get(test_job.job_type, []):
                            logger.debug(f"(Worker.process_jobs) Worker {self.worker_id} is executing job: {job.id}, executed: {job.executed}")
                        await asyncio.sleep(1)
                if job is None:
                    logger.info("(Worker.process_jobs) Empty job submitted to worker!?")
                    break
                logger.info(f"(Worker.process_jobs) Processing job {job.id} for worker {self.worker_id}")
                await job.execute()
                logger.info(f"(Worker.process_jobs) Job executed.")
                await asyncio.sleep(0.001)  # Use 'await' for asynchronous sleep
            except Exception as e:
                import traceback
                from discord_tron_master.bot import clean_traceback
                logger.error(f"An error occurred while processing jobs for worker {self.worker_id}: {e}, traceback: {await clean_traceback(traceback.format_exc())}")
                await asyncio.sleep(1)  # Use 'await' for asynchronous sleep

    async def monitor_worker(self):
        logger.debug(f"(monitor_worker) Beginning worker monitoring for worker {self.worker_id}")
        while True:
            if (self.worker_task is None or self.worker_task.done()) and not self.terminate:
                # Task completed, and worker is not set to terminate
                logger.info(f"(monitor_worker) Worker {self.worker_id} task is done, and worker is not set to terminate. Restarting worker task.")
                self.worker_task = asyncio.create_task(self.process_jobs())
            elif self.terminate:
                logger.info("(monitor_worker) Worker is set to exit, and the time has come.")
                break
            # Sleep for a while before checking again
            logger.info(f"(monitor_worker) Worker {self.worker_id} task: {self.worker_task}, terminate: {self.terminate}")
            await asyncio.sleep(10)

    async def monitor_for_unacknowledged_jobs(self):
        logger.debug(f"(monitor_for_unacknowledged_jobs) Beginning monitoring for unacknowledged jobs for worker {self.worker_id}")
        while True:
            if self.terminate:
                logger.info("(monitor_for_unacknowledged_jobs) Worker is set to exit, and the time has come.")
                break
            if self.job_queue is None:
                logger.warning("(monitor_for_unacknowledged_jobs) Job queue not initialised yet.")
                await asyncio.sleep(1)
                continue
            for job in self.job_queue.in_progress.values():
                if not job.is_acknowledged()[0] and job.needs_resubmission():
                    logger.info(f"Job {job.id} has not been acknowledged. Sending message to worker again.")
                    await job.execute()
            # Sleep for a while before checking again
            await asyncio.sleep(10)

    async def start_monitoring(self):
        # Use 'asyncio.create_task' to run the 'process_jobs' and 'monitor_worker' coroutines
        self.worker_task = asyncio.create_task(self.process_jobs())
        self.monitor_task = asyncio.create_task(self.monitor_worker())
        self.ack_task = asyncio.create_task(self.monitor_for_unacknowledged_jobs())
