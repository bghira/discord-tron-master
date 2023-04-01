from typing import Dict, Any, List

class WorkerManager:
    def __init__(self):
        self.workers = {}  # {"worker_id": {"supported_job_types": [...], "hardware_limits": {...}}, ...}

    def register_worker(self, worker_id, supported_job_types: List[str], hardware_limits: Dict[str, Any]):
        self.workers[worker_id] = {"supported_job_types": supported_job_types, "hardware_limits": hardware_limits}

    def unregister_worker(self, worker_id):
        del self.workers[worker_id]

    def get_worker_supported_job_types(self, worker_id: str) -> List[str]:
        return self.workers[worker_id]["supported_job_types"]

    def get_worker_hardware_limits(self, worker_id: str) -> Dict[str, Any]:
        return self.workers[worker_id]["hardware_limits"]

    def find_best_fit_worker(self, job_type: str) -> str:
        # Logic to find the best fit worker based on job type and worker limits
        pass