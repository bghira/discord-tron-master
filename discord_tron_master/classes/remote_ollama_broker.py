import asyncio
import logging
import uuid

from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.classes.jobs.ollama_completion_job import OllamaCompletionJob

logger = logging.getLogger(__name__)


class RemoteOllamaBroker:
    def __init__(self):
        self._pending: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def request_completion(
        self,
        *,
        role: str,
        prompt: str,
        model: str | None,
        temperature: float,
        max_tokens: int,
        keep_alive: str | None = None,
        timeout_seconds: int | None = None,
        author_id: str = "system",
    ) -> str:
        from discord_tron_master.bot import DiscordBot

        discord = DiscordBot.get_instance()
        if discord is None or discord.worker_manager is None or discord.queue_manager is None:
            raise RuntimeError("Discord worker system is not ready for remote Ollama completions.")
        worker = discord.worker_manager.find_worker_with_fewest_queued_tasks_by_job_type("ollama")
        if worker is None:
            raise RuntimeError("No Ollama workers are currently registered.")
        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        async with self._lock:
            self._pending[request_id] = future
        timeout_value = int(timeout_seconds or AppConfig().get_ollama_timeout_seconds())
        try:
            job = OllamaCompletionJob(
                {
                    "request_id": request_id,
                    "author_id": str(author_id or "system"),
                    "role": str(role or ""),
                    "prompt": str(prompt or ""),
                    "model": str(model or "").strip() or None,
                    "temperature": float(temperature),
                    "max_tokens": int(max_tokens),
                    "keep_alive": str(keep_alive or "").strip() or None,
                }
            )
            await discord.queue_manager.enqueue_job(worker, job)
            result = await asyncio.wait_for(future, timeout=timeout_value)
        finally:
            async with self._lock:
                self._pending.pop(request_id, None)
        if not isinstance(result, dict):
            raise RuntimeError("Remote Ollama worker returned an invalid response.")
        if not result.get("ok"):
            raise RuntimeError(str(result.get("detail") or "Remote Ollama worker failed."))
        text = str(result.get("text") or "").strip()
        if not text:
            raise RuntimeError("Remote Ollama worker returned empty content.")
        return text

    async def complete_request(self, payload: dict):
        request_id = str((payload or {}).get("request_id") or "").strip()
        if not request_id:
            return {"ok": False, "detail": "missing_request_id"}
        async with self._lock:
            future = self._pending.get(request_id)
        if future is None:
            return {"ok": False, "detail": "request_not_found"}
        if not future.done():
            future.set_result(payload)
        return {"ok": True}


remote_ollama_broker = RemoteOllamaBroker()
