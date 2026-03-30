import json
import logging
import time
import uuid


class OllamaCompletionJob:
    def __init__(self, payload: dict):
        self.id = str(uuid.uuid4())
        self.job_id = self.id
        self.job_type = "ollama"
        self.payload = payload
        self.module_name = "ollama"
        self.module_command = "complete"
        self.author_id = str(payload.get("author_id") or "system")
        self.worker = None
        self.date_created = time.time()
        self.migrated = False
        self.migrated_date = None
        self.executed = False
        self.executed_date = None
        self.acknowledged = False
        self.acknowledged_date = None

    def set_worker(self, worker):
        self.worker = worker

    def is_acknowledged(self):
        return (self.acknowledged, self.acknowledged_date)

    def acknowledge(self):
        self.acknowledged = True
        self.acknowledged_date = time.time()

    def needs_resubmission(self):
        if not all(self.is_acknowledged()) and self.executed:
            if (time.time() - self.executed_date) > 15:
                self.executed = False
                self.executed_date = None
                return True
        return False

    async def execute(self):
        if self.executed and not self.needs_resubmission():
            logging.warning(f"Ollama job {self.job_id} has already been executed. Ignoring.")
            return
        self.executed = True
        self.executed_date = time.time()
        message = {
            "job_type": self.job_type,
            "job_id": self.id,
            "module_name": self.module_name,
            "module_command": self.module_command,
            **self.payload,
        }
        await self.worker.send_websocket_message(json.dumps(message))

    async def job_lost(self):
        logging.warning(f"Ollama job {self.job_id} was lost during worker reassignment.")
        return True

    async def job_reassign(self, new_worker: str, reassignment_stage: str = "begin"):
        logging.info(
            f"Ollama job {self.job_id} reassigned to {new_worker} ({reassignment_stage})."
        )
        return True
