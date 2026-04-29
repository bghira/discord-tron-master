from __future__ import annotations

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.classes.jobs.webui_image_job import (
    DEFAULT_WEBUI_IMAGE_MODEL,
    WebUIImageGenerationJob,
)

logger = logging.getLogger(__name__)


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def enqueue_webui_image_job(discord: Any, data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Create and enqueue a web UI image job using the live DTM bot state."""
    prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        return {"error": "prompt is required"}, 400

    if discord is None or discord.worker_manager is None or discord.queue_manager is None:
        return {"error": "Worker system not ready"}, 503

    loop = getattr(discord, "event_loop", None)
    if loop is None or not loop.is_running():
        return {"error": "Discord event loop not ready"}, 503

    job = WebUIImageGenerationJob(
        prompt=prompt,
        model=data.get("model") or DEFAULT_WEBUI_IMAGE_MODEL,
        ref_type=data.get("ref_type", "scene"),
        campaign_id=data.get("campaign_id"),
        room_key=data.get("room_key"),
        actor_id=data.get("actor_id", "webui"),
        callback_url=data.get("callback_url", ""),
        callback_secret=data.get("callback_secret", ""),
        webui_job_id=data.get("job_id"),
        reference_images=data.get("reference_images"),
        metadata=data.get("metadata"),
    )

    worker = discord.worker_manager.find_best_fit_worker(job)
    if worker is None:
        return {"error": "No GPU workers available"}, 503

    try:
        future = asyncio.run_coroutine_threadsafe(
            discord.queue_manager.enqueue_job(worker, job),
            loop,
        )
        future.result(timeout=5)
    except Exception as exc:
        logger.exception("Failed to enqueue WebUI image job")
        return {"error": str(exc)}, 500

    return {"ok": True, "job_id": job.id}, 200


class WebUIImageBridge:
    """Small localhost API that lets the web UI enqueue jobs in the bot process."""

    def __init__(self, config: AppConfig, discord: Any):
        self._config = config
        self._discord = discord
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self._config.is_text_game_webui_enabled():
            return
        if self._config.get_text_game_webui_image_backend() != "dtm":
            return
        if self._server is not None:
            return

        host = self._config.get_text_game_webui_dtm_image_bridge_host()
        port = self._config.get_text_game_webui_dtm_image_bridge_port()
        expected_secret = self._config.get_text_game_webui_link_secret()
        discord = self._discord

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                if self.path.rstrip("/") != "/api/zork/image/generate":
                    self._send_json({"error": "Not found"}, 404)
                    return

                provided_secret = self.headers.get("X-DTM-Link-Secret", "")
                if not provided_secret or provided_secret != expected_secret:
                    self._send_json({"error": "Invalid link secret"}, 403)
                    return

                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._send_json({"error": "Invalid Content-Length"}, 400)
                    return

                try:
                    raw_body = self.rfile.read(length)
                    data = json.loads(raw_body.decode("utf-8") or "{}")
                except Exception:
                    self._send_json({"error": "Invalid JSON body"}, 400)
                    return

                if not isinstance(data, dict):
                    self._send_json({"error": "JSON body must be an object"}, 400)
                    return

                body, status = enqueue_webui_image_job(discord, data)
                self._send_json(body, status)

            def _send_json(self, body: dict[str, Any], status: int) -> None:
                encoded = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: Any) -> None:
                logger.debug("WebUI image bridge: " + format, *args)

        self._server = _ReusableThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="webui_image_bridge",
            daemon=True,
        )
        self._thread.start()
        logger.info("Started WebUI image bridge at http://%s:%s", host, port)

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)
