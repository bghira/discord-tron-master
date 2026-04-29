"""Image generation job submitted from the web UI (no Discord context).

GPU workers process this identically to ``ImageGenerationJob``.  The
difference is that the result is routed back to the web UI via an HTTP
callback rather than being posted to a Discord channel.
"""
import json
import logging
import time
import uuid
from typing import Any, Dict

from discord_tron_master.classes.app_config import AppConfig

logger = logging.getLogger(__name__)

DEFAULT_WEBUI_IMAGE_MODEL = "black-forest-labs/flux.2-klein-4b"


class WebUIImageGenerationJob:
    """Minimal job that satisfies the queue_manager / worker protocol."""

    def __init__(
        self,
        *,
        prompt: str,
        model: str = DEFAULT_WEBUI_IMAGE_MODEL,
        steps: int = 12,
        guidance_scaling: float = 2.5,
        ref_type: str = "scene",
        campaign_id: str | None = None,
        room_key: str | None = None,
        actor_id: str = "webui",
        callback_url: str = "",
        callback_secret: str = "",
        webui_job_id: str | None = None,
        reference_images: list[str] | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> None:
        self.id = str(uuid.uuid4())
        self.job_id = self.id
        self.job_type = "gpu"
        self.module_name = "image_generation"
        self.module_command = "generate_image"

        self.prompt = prompt
        self.model = str(model or DEFAULT_WEBUI_IMAGE_MODEL).strip() or DEFAULT_WEBUI_IMAGE_MODEL
        self.steps = steps
        self.guidance_scaling = guidance_scaling
        self.ref_type = ref_type
        self.campaign_id = campaign_id
        self.room_key = room_key
        self.actor_id = actor_id
        self.callback_url = callback_url
        self.callback_secret = callback_secret
        self.webui_job_id = webui_job_id
        self.reference_images = reference_images or []
        self.extra_metadata = dict(metadata or {})

        # Job lifecycle state expected by queue_manager / worker.
        self.worker = None
        self.author_id = actor_id
        self.date_created = time.time()
        self.payload = None  # No Discord payload tuple.
        self.migrated = False
        self.migrated_date = None
        self.executed = False
        self.executed_date = None
        self.acknowledged = False
        self.acknowledged_date = None

    # ------------------------------------------------------------------
    # Queue / worker protocol helpers
    # ------------------------------------------------------------------

    def set_worker(self, worker):
        self.worker = worker

    def acknowledge(self):
        self.acknowledged = True
        self.acknowledged_date = time.time()

    def is_acknowledged(self):
        return (self.acknowledged, self.acknowledged_date)

    def needs_resubmission(self):
        if not all(self.is_acknowledged()) and self.executed:
            if (time.time() - self.executed_date) > 15:
                self.executed = False
                self.executed_date = None
                return True

    def is_migrated(self):
        return (self.migrated, self.migrated_date)

    def migrate(self):
        self.migrated = True
        self.migrated_date = time.time()

    def payload_text(self):
        return self.prompt or "(no prompt)"

    def _build_user_config(self) -> dict:
        try:
            user_config = AppConfig().get_user_config(user_id=self.actor_id)
        except Exception:
            user_config = {}

        user_config["auto_model"] = False
        user_config["model"] = self.model
        user_config["steps"] = self.steps
        user_config["guidance_scaling"] = self.guidance_scaling
        if not isinstance(user_config.get("resolution"), dict):
            user_config["resolution"] = {"width": 1024, "height": 1024}
        if self.ref_type == "avatar":
            user_config["resolution"] = {"width": 768, "height": 768}
        return user_config

    # ------------------------------------------------------------------
    # Format the WebSocket message sent to the GPU worker.
    # ------------------------------------------------------------------

    async def format_payload(self) -> dict:
        message_flags: Dict[str, Any] = {
            "zork_scene": True,
            "suppress_image_reactions": True,
            "suppress_image_details": True,
            "zork_campaign_id": self.campaign_id,
            "zork_room_key": self.room_key,
            "zork_user_id": self.actor_id,
            # WebUI callback metadata — the command processor uses these
            # to POST the result back to the web UI.
            "zork_webui_callback_url": self.callback_url,
            "zork_webui_callback_secret": self.callback_secret,
            "zork_webui_job_id": self.webui_job_id,
        }
        if self.ref_type == "scene":
            message_flags["zork_store_image"] = True
            message_flags["zork_seed_room_image"] = True
        elif self.ref_type == "avatar":
            message_flags["zork_store_avatar"] = True

        # Merge any additional metadata from the webui request.
        for k, v in self.extra_metadata.items():
            message_flags.setdefault(k, v)

        user_config = self._build_user_config()

        return {
            "job_type": self.job_type,
            "job_id": self.id,
            "module_name": self.module_name,
            "module_command": self.module_command,
            "discord_context": {
                "author": {"id": 0, "name": "webui", "discriminator": "0000"},
                "channel": {"id": 0, "name": "webui-image"},
                "guild": {"id": 0, "name": "webui"},
            },
            "overridden_user_id": self.actor_id,
            "image_prompt": self.prompt,
            "prompt": self.prompt,
            "discord_first_message": {
                "author": {"id": 0, "name": "webui", "discriminator": "0000"},
                "channel": {"id": 0, "name": "webui-image"},
                "guild": {"id": 0, "name": "webui"},
                "message_id": 0,
            },
            "config": user_config,
            "model_config": {},
            "message_flags": message_flags,
            **({"image_data": self.reference_images} if self.reference_images else {}),
        }

    async def execute(self):
        if self.executed and not self.needs_resubmission():
            logger.warning("WebUI job %s already executed, ignoring.", self.job_id)
            return
        self.executed = True
        self.executed_date = time.time()
        message = await self.format_payload()
        try:
            await self.worker.send_websocket_message(json.dumps(message))
            logger.info(
                "WebUI image job %s sent to worker %s",
                self.job_id,
                self.worker.worker_id,
            )
        except Exception as exc:
            logger.error("Error sending WebUI image job %s: %s", self.job_id, exc)
            return False

    # No-ops for Discord-specific lifecycle hooks.
    async def job_reassign(self, new_worker, reassignment_stage="begin"):
        logger.info("WebUI job %s reassigned to %s", self.job_id, new_worker)
        return True

    async def job_lost(self):
        logger.warning("WebUI job %s lost (worker disconnected).", self.job_id)
        if not self.callback_url or not self.webui_job_id:
            return True

        payload = {
            "status": "failed",
            "error": "Image generation worker disconnected before completing the job.",
            "prompt": self.prompt or "",
            "ref_type": self.ref_type,
            "actor_id": self.actor_id,
            "room_key": self.room_key,
            "job_id": self.webui_job_id,
        }
        headers = {}
        if self.callback_secret:
            headers["X-DTM-Link-Secret"] = str(self.callback_secret)

        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.callback_url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                    ssl=False,
                ) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        logger.warning(
                            "WebUI lost-job callback failed (%s %s): %s",
                            resp.status,
                            self.callback_url,
                            body[:200],
                        )
                    else:
                        logger.info(
                            "WebUI lost-job callback succeeded: %s",
                            self.callback_url,
                        )
        except Exception as exc:
            logger.warning(
                "WebUI lost-job callback error (%s): %s",
                self.callback_url,
                exc,
            )
        return True
