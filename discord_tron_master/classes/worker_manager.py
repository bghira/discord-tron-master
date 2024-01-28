from typing import Dict, Any, List
import logging, websocket, traceback, asyncio, time
from discord_tron_master.classes.worker import Worker
from discord_tron_master.exceptions.auth import AuthError
from discord_tron_master.exceptions.registration import RegistrationError
from discord_tron_master.classes.job import Job
from threading import Thread
logger = logging.getLogger('WorkerManager')
logger.setLevel('DEBUG')
class WorkerManager:
    def __init__(self):
        self.workers = {}  # {"worker_id": <Worker>, ...}
        self.workers_by_capability = {
            "gpu": [],
            "variation": [],
            "compute": [],
            "memory": [],
            "llama": [],
            "stablelm": [],
            "stablevicuna": [],
            "tts_bark": [],
        }
        self.queue_manager = None
        self.worker_mon_thread = Thread(
            target=self.monitor_worker_queues,
            name=f"worker_mon_thread",
            daemon=True,
        )
        self.worker_mon_thread.start()

    def get_all_workers(self):
        return self.workers

    def get_worker(self, worker_id: str):
        if not worker_id or worker_id not in self.workers or not isinstance(self.workers[worker_id], Worker):
            logger.error(f"Tried accessing invalid worker {self.workers[worker_id]}, traceback: {traceback.format_exc()}")
            raise ValueError(f"Worker '{worker_id}' is not registered. Cannot retrieve details.")
        return self.workers[worker_id]

    def finish_job_for_worker(self, worker_id: str, job: Job):
        worker = self.get_worker(worker_id)
        worker.complete_job(job=job)

    async def finish_payload(self, command_processor, arguments: Dict, data: Dict, websocket):
        worker_id = arguments["worker_id"]
        job_id = arguments["job_id"]
        if worker_id and job_id:
            worker = self.get_worker(worker_id)
            worker.complete_job_by_id(job_id)
            logger.info("Finished job for worker " + worker_id)
            return {"status": "successfully finished job"}
        else:
            logger.error(f"Invalid data received: {data}")
            raise ValueError("Invalid payload received by finish_payload handler. We expect job_id and worker_id.")

    async def acknowledge_payload(self, command_processor, arguments: Dict, data: Dict, websocket):
        worker_id = arguments["worker_id"]
        job_id = arguments["job_id"]
        if worker_id and job_id:
            worker = self.get_worker(worker_id)
            if worker.job_queue is not None:
                result = await worker.acknowledge_job(job_id)
            if result:
                logger.info("acknowledged job for worker " + worker_id)
                return {"status": "successfully acknowledged job"}
            else:
                logger.warning("acknowledgement failed for worker " + worker_id + " and job_id " + job_id)
                return {"status": "failed to acknowledge job"}
        else:
            logger.error(f"Invalid data received: {data}")
            raise ValueError("Invalid payload received by finish_payload handler. We expect job_id and worker_id.")

    def find_worker_with_fewest_queued_tasks(self, job: Job):
        return self.find_worker_with_fewest_queued_tasks_by_job_type(job.job_type)

    def find_worker_with_fewest_queued_tasks_by_job_type(self, job_type: str, exclude_worker_id: str = None):
        job_type = job_type
        min_queued_tasks = float("inf")
        selected_worker = self.find_first_worker(job_type)
        for worker_id, worker in self.workers.items():
            if exclude_worker_id and worker_id == exclude_worker_id:
                logger.debug(f"Skipping worker {worker_id} because it is the excluded worker.")
                selected_worker = None
                continue
            logger.debug(f"worker_id: {worker_id}, worker: {worker}")
            if job_type in worker.supported_job_types and worker.supported_job_types[job_type] is True:
                logger.info(f"Found valid worker for {job_type} job")
                queued_tasks = self.queue_manager.worker_queue_length(worker)
                if queued_tasks < min_queued_tasks:
                    logger.debug(f"Found worker with fewer queued tasks: {queued_tasks} < {min_queued_tasks}")
                    min_queued_tasks = queued_tasks
                    selected_worker = worker
                else:
                    logger.debug(f"Worker {worker_id} has more or same queued tasks than current best: {queued_tasks} >= {min_queued_tasks}")                    
            else:
                logger.warn(f"Worker {worker_id} does not support job type {job_type}: {worker.supported_job_types}")
        
        return selected_worker

    def find_worker_with_zero_queued_tasks_by_job_type(self, job_type: str, exclude_worker_id: str = None):
        job_type = job_type
        # We don't want to return anything if all servers have a job.
        selected_worker = None
        for worker_id, worker in self.workers.items():
            if exclude_worker_id is not None and worker_id == exclude_worker_id:
                continue
            if job_type in worker.supported_job_types and worker.supported_job_types[job_type] is True:
                queued_tasks = self.queue_manager.worker_queue_length(worker)
                if queued_tasks == 0:
                    logger.info(f"(monitor_worker_queues) Found empty worker: {worker_id}")
                    selected_worker = worker
                else:
                    logger.info(f"(monitor_worker_queues) Worker {worker_id} has {queued_tasks} queued tasks.")
            else:
                logger.warn(f"Worker {worker_id} does not support job type {job_type}: {worker.supported_job_types}")
        
        return selected_worker

    def find_first_worker(self, job_type: str) -> Worker:
        capable_workers = self.workers_by_capability.get(job_type)
        if not capable_workers:
            logger.error(f"No workers capable of handling job type {job_type}")
            return None
        return capable_workers[0]

    async def register_worker(self, worker_id: str, supported_job_types: List[str], hardware_limits: Dict[str, Any], hardware: Dict[str, Any]) -> Worker:
        if worker_id in self.workers:
            logger.error(f"Tried to register an already-registered worker: {worker_id}. Forcibly unregistering that worker.")
            await self.unregister_worker(worker_id)
            raise RegistrationError(f"Worker '{worker_id}' is already registered. Cannot register again. Wait a bit, and then try again.")
        if not worker_id or worker_id == "":
            raise RegistrationError("Cannot register worker with blank worker_id.")
        logger.info(f"Registering a new worker, {worker_id}!")
        worker = Worker(worker_id, supported_job_types, hardware_limits, hardware, hardware["hostname"])
        self.workers[worker_id] = worker
        for job_type in supported_job_types:
            if supported_job_types[job_type] is True:
                self.workers_by_capability[job_type].append(worker)
        return worker

    async def unregister_worker(self, worker_id):
        worker = self.workers.pop(worker_id, None)
        if worker:
            supported_job_types = worker.supported_job_types
            for job_type in supported_job_types:
                # Remove worker from the list of workers_by_capability
                self.workers_by_capability[job_type] = [w for w in self.workers_by_capability[job_type] if w.worker_id != worker_id]
                
            await worker.stop()
        logger.info(f"After unregistering worker, we are left with: {self.workers} and {self.workers_by_capability}")


    def get_worker_supported_job_types(self, worker_id: str) -> List[str]:
        return self.workers[worker_id].supported_job_types

    def get_worker_hardware_limits(self, worker: Worker) -> Dict[str, Any]:
        if worker is None:
            return None
        return worker.hardware_limits

    def get_queue_lengths_by_worker(self) -> Dict[str, int]:
        return {worker_id: self.queue_manager.get_queue_length_by_worker(worker_id) for worker_id in self.workers}

    def get_queue_length_by_worker(self, worker: Worker) -> int:
        return worker.job_queue.qsize()

    def find_best_fit_worker(self, job: Job) -> Worker:
        # This is a possibility in the future to use a better system based on the task's resolution requirements, etc.
        #worker_with_best_hardware = self.find_best_hardware_for_job(worker_with_fewest_slots, job)

        # Logic to find the best fit worker based on job type and worker limits
        return self.find_worker_with_fewest_queued_tasks(job)

    def find_best_hardware_for_job(self, comparison_hardware: Worker, job: Job) -> Dict[str, Any]:
        # Logic to find the best hardware for a job based on job type and worker limits
        job_type = job.job_type
        if comparison_hardware is None:
            return None
        cmp_limits = comparison_hardware.hardware_limits
        for worker in self.workers_by_capability[job_type]:
            current_hw = worker.hardware_limits
            if current_hw["gpu"] >= cmp_limits["gpu"] and current_hw["cpu"] >= cmp_limits["cpu"] and current_hw["memory"] >= cmp_limits["memory"]:
                return worker


    def set_queue_manager(self, queue_manager):
        self.queue_manager = queue_manager

    async def register(self, command_processor, payload: Dict[str, Any], data: Dict, websocket: websocket) -> Dict:
        logger.debug("Registering worker via WebSocket")
        try:
            if "worker_id" in payload:
                worker_id = payload["worker_id"]
            elif "worker_id" in payload["arguments"]:
                worker_id = payload["arguments"]["worker_id"]
        except KeyError:
            logger.error(f"Worker ID not provided in payload: {payload}")
            return {"error": "Worker ID not provided in payload"}
        supported_job_types = payload["supported_job_types"]
        hardware_limits = payload["hardware_limits"]
        hardware = payload["hardware"]
        worker = await self.register_worker(worker_id, supported_job_types, hardware_limits, hardware)
        await self.queue_manager.register_worker(worker_id, supported_job_types)
        await worker.set_job_queue(await self.queue_manager.create_queue(worker))
        worker.set_websocket(websocket)
        await worker.start_monitoring()  # Use 'await' to call the async 'start_monitoring' method
        return {"success": True, "result": "Worker " + str(worker_id) + " registered successfully"}

    async def unregister(self, command_processor, payload: Dict[str, Any], data: Dict, websocket: websocket) -> Dict:
        logger.info("Unregistering worker for queued jobs")
        try:
            worker_id = payload["worker_id"]
        except KeyError:
            logger.error("Worker ID not provided in payload")
            return {"error": "Worker ID not provided in payload"}
        worker = self.workers.get(worker_id)
        worker.stop()
        await self.unregister_worker(worker_id)
        await self.queue_manager.unregister_worker(worker_id)
        logger.info("Successfully unregistered worker from queue manager.")
        return {"success": True, "result": "Worker " + str(worker_id) + " unregistered successfully"}

    # A method to watch over worker queues and relocate them to a worker that's less busy, if available:
    def monitor_worker_queues(self):
        while True:
            self.check_job_queue_for_waiting_items()
            self.reorganize_queue_by_user_ids()
            time.sleep(60)  # Sleep for 10 seconds before checking again

    def does_queue_contain_multiple_users(self, worker: Worker):
        """
        We need to check each server's queue to determine whether they are containing more than one user's requests.
                
        The job has an author_id property we can use. It is their numeric Discord user id.
        """
        user_ids = set()
        if worker.job_queue is None:
            return False
        jobs = worker.job_queue.view()
        for job in jobs:
            if job is None:
                continue
            user_ids.add(job.author_id)
        logger.debug(f"Worker {worker} has {len(user_ids)} different users.")
        return len(user_ids) > 1

    def does_queue_contain_a_block_of_user_requests(self, worker: Worker):
        """
        We will check a servers' queue to determine whether they are containing more than one user's requests in sequence.
        
        We will assume more than 2 jobs in a row for a single user constitutes a block of jobs.
        """
        idx = -1
        identical_authors = 0
        for job in worker.job_queue.view():
            idx += 1
            if job is None:
                continue
            if idx > 0 and job.author_id == worker.job_queue.view()[idx - 1].author_id and not job.is_migrated()[0]:
                # This job is from the same user as the previous job
                identical_authors += 1
                if identical_authors > 2:
                    # We have found a block of jobs from the same user.
                    return True
                continue
            else:
                # This job is from a different user.
                return False
        

    def reorganize_queue_by_user_ids(self):
        """
        Sometimes, the queue gets distributed so that a multiple user has gens in order.
        
        We want to reorder it then, so that the user IDs of requests interpolate.
        
        We only need to reorder them if there are a string of jobs with the same author ID.
        """
        for worker_id, worker in self.workers.items():
            does_queue_contain_multiple_users = self.does_queue_contain_multiple_users(worker=worker)
            if not does_queue_contain_multiple_users:
                logger.info(f"(reorganize_queue_by_user_ids) Queue only contains zero or more entries for a single or zero users. No need to reorganize.")
                continue
            logger.info(f"(reorganize_queue_by_user_ids) Queue for worker {worker_id} contains multiple users")
            if not self.does_queue_contain_a_block_of_user_requests(worker):
                logger.info(f"We lack a solid block of jobs from the same user. Not Reorganizing queue.")
            logger.info(f"We found a solid block of jobs from the same user. Not Reorganizing queue.")
            # We need to reorganize the queue.
            jobs = worker.job_queue.view()
            logger.info(f"(reorganize_queue_by_user_ids) Worker {worker_id} jobs before reorganise: {[f'author_id={job.author_id}, id={job.id}' for job in jobs]}")
            # We need to get the user IDs of the jobs in the queue.
            user_ids = set()
            for job in jobs:
                if job is None:
                    continue
                user_ids.add(job.author_id)
            logger.info(f"(reorganize_queue_by_user_ids) User IDs in this queue: {user_ids}")
            # We now have a set of user IDs.
            # We need to get the jobs for each user ID.
            jobs_by_user_id = {}
            for user_id in user_ids:
                jobs_by_user_id[user_id] = [job for job in jobs if job.author_id == user_id]
            logger.info(f"(reorganize_queue_by_user_ids) Jobs by user ID: {jobs_by_user_id}")
            # We now have a dictionary of jobs by user ID.
            # We need to reorganize the queue so that the jobs are interleaved.
            # We need to get the number of jobs for the user with the most jobs.
            max_jobs = 0
            for user_id, jobs in jobs_by_user_id.items():
                if len(jobs) > max_jobs:
                    max_jobs = len(jobs)
            logger.info(f"(reorganize_queue_by_user_ids) Maximum number of jobs for a single user: {max_jobs}")
            # We now have the maximum number of jobs for a single user.
            # We need to iterate over the jobs, and add them to a new list.
            new_jobs = []
            added_job_ids = set()
            for i in range(0, max_jobs):
                logger.info(f"(reorganize_queue_by_user_ids) Adding job {i} from each user to the new queue.")
                for user_id, jobs in jobs_by_user_id.items():
                    logger.info(f"(reorganize_queue_by_user_ids) Checking if {i} < {len(jobs)}")
                    if i < len(jobs) and jobs[i].id not in added_job_ids:
                        logger.info(f"(reorganize_queue_by_user_ids) Adding job {i} (id={jobs[i].id}) from user {user_id} to the new queue.")
                        jobs[i].migrate()
                        new_jobs.append(jobs[i])
                        added_job_ids.add(jobs[i].id)
            # We now have a new list of jobs.
            # We need to replace the existing list of jobs with the new list.
            asyncio.run(worker.job_queue.set_queue_from_list(new_jobs))
            logger.info(f"(reorganize_queue_by_user_ids) Reorganized queue for worker {worker_id} to: {[f'author_id={job.author_id}, id={job.id}' for job in new_jobs]}")

    def check_job_queue_for_waiting_items(self):
        for worker_id, worker in self.workers.items():
            current_time = time.time()
            if worker.job_queue is not None and worker.job_queue.qsize() > 0:
                logger.info(f"(monitor_worker_queues) Checking worker {worker_id} for jobs that have been waiting for more than 30 seconds.")
                # There are jobs in the queue.
                # Have any of the jobs been waiting longer than 30 seconds?
                # We need to retrieve them without disturbing the queue.
                jobs = worker.job_queue.view()
                logger.info(f"(monitor_worker_queues) Discovered jobs: {jobs}")
                for job in jobs:
                    is_migrated, migrated_date = job.is_migrated()
                    if job is None or ((is_migrated and current_time - migrated_date < 300) and current_time - job.date_created < 30):
                        # This job has NOT been waiting for more than 30 seconds.
                        # We do nothing.
                        continue
                    logger.info(f"(monitor_worker_queues) Job {job.id} has been waiting for more than 30 seconds. Checking for a less busy worker.")
                    new_worker = self.find_worker_with_zero_queued_tasks_by_job_type(job.job_type, exclude_worker_id=worker_id)
                    if new_worker is None:
                        logger.info("(monitor_worker_queues) No other workers available to take this job.")
                        continue
                    # Is it the same worker?
                    if new_worker.worker_id == worker_id:
                        logger.info(f"(monitor_worker_queues) We are already on the best worker for {job.job_type} jobs. They will have to wait.")
                        continue
                    else:
                        # Remove the job from its current worker
                        logger.info(f"(monitor_worker_queues) Found a less busy worker {new_worker.worker_id} for job {job.id}.")
                        asyncio.run(worker.job_queue.remove(job))
                        asyncio.run(self.queue_manager.enqueue_job(new_worker, job))
