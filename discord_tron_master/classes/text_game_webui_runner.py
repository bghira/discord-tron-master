from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from discord_tron_master.classes.app_config import AppConfig

logger = logging.getLogger(__name__)

_ZAI_DEFAULT_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
_BACKEND_DEFAULT_MODELS = {
    "zai": "glm-5-turbo",
}


class TextGameWebUIRunner:
    def __init__(self, config: AppConfig):
        self._config = config
        self._process: subprocess.Popen[str] | None = None

    def is_enabled(self) -> bool:
        return self._config.is_text_game_webui_enabled()

    def start(self) -> None:
        if not self.is_enabled():
            return
        if self._process is not None and self._process.poll() is None:
            return

        project_path = Path(self._config.get_text_game_webui_project_path()).expanduser()
        if not project_path.exists():
            raise RuntimeError(f"text-game-webui project path does not exist: {project_path}")
        tge_project_path = Path(self._config.get_text_game_webui_tge_project_path()).expanduser()
        if not tge_project_path.exists():
            raise RuntimeError(
                f"text-game-engine project path does not exist: {tge_project_path}"
            )

        python_bin = self._config.get_text_game_webui_python_bin() or sys.executable
        host = self._config.get_text_game_webui_host()
        port = self._config.get_text_game_webui_port()

        env = os.environ.copy()
        env["TEXT_GAME_WEBUI_GATEWAY_BACKEND"] = "tge"
        env["TEXT_GAME_WEBUI_HOST"] = str(host)
        env["TEXT_GAME_WEBUI_PORT"] = str(port)
        env["TEXT_GAME_WEBUI_TGE_DATABASE_URL"] = self._config.get_text_game_webui_database_url()
        env["TEXT_GAME_WEBUI_DEBUG"] = "1" if self._config.get_text_game_webui_debug() else "0"
        env["TEXT_GAME_WEBUI_ZORK_LOG_ROOT"] = str(
            Path(self._config.project_root).resolve().parent / "zork-logs"
        )
        env["TEXT_GAME_WEBUI_DTM_LINK_AUTH"] = "1"
        env["TEXT_GAME_WEBUI_DTM_LINK_SECRET"] = self._config.get_text_game_webui_link_secret()
        env["TEXT_GAME_WEBUI_DTM_COMMAND_PREFIX"] = str(self._config.get_command_prefix() or "+")
        env["TEXT_GAME_WEBUI_IMAGE_BACKEND"] = self._config.get_text_game_webui_image_backend()
        env["TEXT_GAME_WEBUI_DTM_IMAGE_API_URL"] = (
            self._config.get_text_game_webui_dtm_image_api_url()
        )
        env["TEXT_GAME_WEBUI_TGE_RUNTIME_PROBE_LLM"] = (
            "1" if self._config.get_text_game_webui_runtime_probe_llm() else "0"
        )
        env["TEXT_GAME_WEBUI_TGE_RUNTIME_PROBE_TIMEOUT_SECONDS"] = str(
            self._config.get_text_game_webui_runtime_probe_timeout_seconds()
        )
        existing_pythonpath = [part for part in str(env.get("PYTHONPATH") or "").split(os.pathsep) if part]
        desired_pythonpath = [str(project_path), str(tge_project_path)]
        env["PYTHONPATH"] = os.pathsep.join(desired_pythonpath + existing_pythonpath)
        self._apply_llm_environment(env)

        command = [
            python_bin,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            str(host),
            "--port",
            str(port),
        ]

        logger.info(
            "Starting text-game-webui at http://%s:%s using %s (image backend=%s, dtm image api=%s)",
            host,
            port,
            project_path,
            env["TEXT_GAME_WEBUI_IMAGE_BACKEND"],
            env["TEXT_GAME_WEBUI_DTM_IMAGE_API_URL"],
        )
        self._process = subprocess.Popen(
            command,
            cwd=str(project_path),
            env=env,
            stdout=None,
            stderr=None,
            text=True,
        )

        time.sleep(2.0)
        if self._process.poll() is not None:
            code = self._process.returncode
            self._process = None
            raise RuntimeError(f"text-game-webui exited immediately with code {code}")

    def _apply_llm_environment(self, env: dict[str, str]) -> None:
        sync_with_zork = self._config.get_text_game_webui_sync_zork_backend()
        env["TEXT_GAME_WEBUI_TGE_SYNC_WITH_DTM"] = "1" if sync_with_zork else "0"

        completion_mode = self._config.get_text_game_webui_completion_mode()
        llm_base_url = self._config.get_text_game_webui_llm_base_url()
        llm_api_key = self._config.get_text_game_webui_llm_api_key()
        llm_model = self._config.get_text_game_webui_llm_model()

        if sync_with_zork:
            fallback_backend = "ollama" if self._config.get_ollama_api_key() else "zai"
            backend_config = self._config.get_zork_backend_config(default_backend=fallback_backend)
            backend = str(backend_config.get("backend") or fallback_backend).strip().lower() or fallback_backend
            raw_backend_model = backend_config.get("model")
            # Structured specs (list/dict) are per-campaign overrides; leave the
            # global env var empty and let the campaign state apply at runtime.
            if isinstance(raw_backend_model, (list, dict)):
                backend_model = None
            else:
                backend_model = str(raw_backend_model or "").strip() or None
            completion_mode = completion_mode or backend
            llm_model = llm_model or backend_model or _BACKEND_DEFAULT_MODELS.get(backend)
            if backend == "zai":
                llm_base_url = llm_base_url or _ZAI_DEFAULT_BASE_URL
                llm_api_key = llm_api_key or self._config.get_openai_api_key()
            elif backend == "ollama":
                llm_base_url = self._config.get_ollama_base_url() or llm_base_url
                llm_api_key = self._config.get_ollama_api_key() or llm_api_key

        if completion_mode:
            env["TEXT_GAME_WEBUI_TGE_COMPLETION_MODE"] = completion_mode
        if llm_base_url:
            env["TEXT_GAME_WEBUI_TGE_LLM_BASE_URL"] = llm_base_url
        if llm_api_key:
            env["TEXT_GAME_WEBUI_TGE_LLM_API_KEY"] = llm_api_key
        if llm_model:
            env["TEXT_GAME_WEBUI_TGE_LLM_MODEL"] = llm_model

        # Always pass Ollama Cloud config so the webui can switch at runtime.
        ollama_base_url = self._config.get_ollama_base_url()
        ollama_api_key = self._config.get_ollama_api_key()
        if ollama_base_url and ollama_base_url != "http://127.0.0.1:11434":
            env["TEXT_GAME_WEBUI_TGE_OLLAMA_BASE_URL"] = ollama_base_url
        if ollama_api_key:
            env["TEXT_GAME_WEBUI_TGE_OLLAMA_API_KEY"] = ollama_api_key

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is not None:
            return
        logger.info("Stopping text-game-webui sidecar process")
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("text-game-webui did not stop after SIGTERM; killing it")
            process.kill()
            process.wait(timeout=5)
