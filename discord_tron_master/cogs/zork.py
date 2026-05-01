from discord.ext import commands
import asyncio
import datetime
import io
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from urllib import error as urllib_error
from urllib import request as urllib_request
import discord
from sqlalchemy import or_

from discord_tron_master.bot import DiscordBot
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.adapters.emulator_bridge import EmulatorBridge as ZorkEmulator
from discord_tron_master.classes.zork_memory import ZorkMemory
from text_game_engine.core.source_material_memory import SourceMaterialMemory

logger = logging.getLogger(__name__)
logger.setLevel("INFO")


class _WorldTimeSendProxy:
    def __init__(self, target, formatter):
        self._target = target
        self._formatter = formatter

    def __getattr__(self, name):
        return getattr(self._target, name)

    async def send(self, content=None, *args, **kwargs):
        if content is not None:
            content = self._formatter(content)
            return await self._target.send(content, *args, **kwargs)
        if "content" in kwargs and kwargs["content"] is not None:
            kwargs["content"] = self._formatter(kwargs["content"])
        return await self._target.send(*args, **kwargs)


class Zork(commands.Cog):
    QUEUE_PREFIX = "[queue]"
    SMS_NOTICE_REACTIONS = ("🧵", "✉️")
    # Legacy state key. Backend credentials now stay in AppConfig/env only.
    CAMPAIGN_BACKEND_STATE_KEY = "zork_backend_config"
    SESSION_RUNTIME_CONFIG_KEY = "zork_runtime_config"
    CAMPAIGN_STYLE_STATE_KEY = "style_direction"
    THINKING_SUPPORTED_BACKENDS = {"zai", "ollama"}
    AUDIO_TRANSCRIPTION_REACTIONS = ("✅", "❌")
    TURN_BUSY_TEXT = "Another turn is already resolving. Please retry."
    TURN_BUSY_RETRY_DELAY_SECONDS = 0.5
    RESTART_DRAIN_TIMEOUT_SECONDS = 600
    WEBUI_DISCORD_ECHO_POLL_SECONDS = 2.0
    AUDIO_PREVIEW_MAX_CHARS = 1500
    AUDIO_FILE_EXTENSIONS = (".aac", ".flac", ".m4a", ".mp3", ".mp4", ".oga", ".ogg", ".wav", ".weba", ".webm")
    AUDIO_CONTENT_TYPE_PREFIXES = ("audio/", "video/")
    CLOCK_WEEKDAY_ALIASES = {
        "mon": "monday",
        "monday": "monday",
        "tue": "tuesday",
        "tues": "tuesday",
        "tuesday": "tuesday",
        "wed": "wednesday",
        "wednesday": "wednesday",
        "thu": "thursday",
        "thur": "thursday",
        "thurs": "thursday",
        "thursday": "thursday",
        "fri": "friday",
        "friday": "friday",
        "sat": "saturday",
        "saturday": "saturday",
        "sun": "sunday",
        "sunday": "sunday",
    }

    def __init__(self, bot):
        self.bot = bot
        self.config = AppConfig()
        self._turn_queues: dict[tuple[str, str], asyncio.Queue] = {}
        self._turn_queue_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._pending_audio_transcriptions: dict[str, dict[str, object]] = {}
        self._webui_discord_echo_task: asyncio.Task | None = None

    def _prefix(self) -> str:
        return self.config.get_command_prefix()

    def _text_game_webui_link_url(self) -> str | None:
        if not self.config.is_text_game_webui_enabled():
            return None
        host = str(self.config.get_text_game_webui_host() or "127.0.0.1").strip() or "127.0.0.1"
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        port = int(self.config.get_text_game_webui_port() or 8080)
        return f"http://{host}:{port}/api/dtm-link/confirm"

    def _text_game_webui_turn_refresh_url(self, campaign_id: str | int | None) -> str | None:
        if not self.config.is_text_game_webui_enabled() or campaign_id is None:
            return None
        host = str(self.config.get_text_game_webui_host() or "127.0.0.1").strip() or "127.0.0.1"
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        port = int(self.config.get_text_game_webui_port() or 8080)
        return f"http://{host}:{port}/api/internal/campaigns/{campaign_id}/turns/refresh"

    async def _confirm_text_game_webui_link(
        self,
        *,
        code: str,
        actor_id: str,
        display_name: str,
    ) -> dict[str, object]:
        url = self._text_game_webui_link_url()
        if not url:
            raise RuntimeError("text-game-webui is not enabled in config.")
        payload = json.dumps(
            {
                "code": code,
                "actor_id": actor_id,
                "display_name": display_name,
            }
        ).encode("utf-8")
        req = urllib_request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-DTM-Link-Secret": self.config.get_text_game_webui_link_secret(),
            },
        )

        def _send() -> dict[str, object]:
            with urllib_request.urlopen(req, timeout=10) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {"ok": True}

        try:
            return await asyncio.to_thread(_send)
        except urllib_error.HTTPError as exc:
            detail = ""
            try:
                raw = exc.read().decode("utf-8")
                if raw.strip():
                    parsed = json.loads(raw)
                    detail = str(parsed.get("detail") or "").strip()
            except Exception:
                detail = ""
            if detail:
                raise RuntimeError(detail) from exc
            raise RuntimeError(f"Web UI link request failed with HTTP {exc.code}.") from exc
        except urllib_error.URLError as exc:
            raise RuntimeError(f"Could not reach text-game-webui: {exc.reason}") from exc

    async def _notify_text_game_webui_turn_refresh(
        self,
        *,
        campaign_id: str | int | None,
        actor_id: str | int | None = None,
        session_id: str | int | None = None,
    ) -> None:
        url = self._text_game_webui_turn_refresh_url(campaign_id)
        if not url:
            return
        payload = json.dumps(
            {
                "actor_id": str(actor_id or "").strip() or None,
                "session_id": str(session_id or "").strip() or None,
            }
        ).encode("utf-8")
        req = urllib_request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-DTM-Link-Secret": self.config.get_text_game_webui_link_secret(),
            },
        )

        def _send() -> None:
            with urllib_request.urlopen(req, timeout=5) as response:
                response.read()

        try:
            await asyncio.to_thread(_send)
        except urllib_error.HTTPError as exc:
            logger.debug(
                "text-game-webui turn refresh failed for campaign %s with HTTP %s",
                campaign_id,
                exc.code,
                exc_info=True,
            )
        except urllib_error.URLError:
            logger.debug(
                "text-game-webui turn refresh unavailable for campaign %s",
                campaign_id,
                exc_info=True,
            )

    @classmethod
    def _parse_clock_value(
        cls,
        raw_value: str,
        *,
        current_day: int,
    ) -> tuple[dict[str, object] | None, str | None]:
        text = " ".join(str(raw_value or "").strip().split())
        if not text:
            return None, "Provide a time like `18:30`, `138 18:30`, or `friday 138 18:30`."
        parts = text.split()
        weekday = None
        head = parts[0].lower() if parts else ""
        if head in cls.CLOCK_WEEKDAY_ALIASES:
            weekday = cls.CLOCK_WEEKDAY_ALIASES[head]
            parts = parts[1:]
        if parts and parts[0].lower() == "day":
            parts = parts[1:]
        if not parts:
            return None, "Missing time value. Use `18:30`, `138 18:30`, or `friday 138 18:30`."
        if len(parts) == 1:
            day = current_day
            time_token = parts[0]
        elif len(parts) == 2:
            try:
                day = int(parts[0])
            except (TypeError, ValueError):
                return None, "Day must be a positive integer."
            time_token = parts[1]
        else:
            return None, "Use `18:30`, `138 18:30`, or `friday 138 18:30`."
        time_match = re.fullmatch(r"(\d{1,2})(?::(\d{1,2}))?", str(time_token or "").strip())
        if time_match is None:
            return None, "Time must look like `18`, `18:30`, or `06:05`."
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or "0")
        if day < 1:
            return None, "Day must be 1 or greater."
        if hour < 0 or hour > 23:
            return None, "Hour must be between 0 and 23."
        if minute < 0 or minute > 59:
            return None, "Minute must be between 0 and 59."
        return {
            "day": day,
            "hour": hour,
            "minute": minute,
            "day_of_week": weekday,
        }, None

    @staticmethod
    def _queue_key(campaign_id: str | int, actor_id: str | int) -> tuple[str, str]:
        return (str(campaign_id), str(actor_id))

    def _strip_queue_prefix(self, content: str) -> str | None:
        text = str(content or "").strip()
        if not text.lower().startswith(self.QUEUE_PREFIX):
            return None
        stripped = text[len(self.QUEUE_PREFIX):].strip()
        return stripped or None

    async def _add_queue_reaction(self, message) -> None:
        try:
            await message.add_reaction("📥")
        except Exception:
            return

    @staticmethod
    def _looks_like_audio_attachment(attachment) -> bool:
        content_type = str(getattr(attachment, "content_type", "") or "").strip().lower()
        if any(content_type.startswith(prefix) for prefix in Zork.AUDIO_CONTENT_TYPE_PREFIXES):
            return True
        filename = str(getattr(attachment, "filename", "") or "").strip().lower()
        return filename.endswith(Zork.AUDIO_FILE_EXTENSIONS)

    def _first_audio_attachment(self, message):
        for attachment in list(getattr(message, "attachments", []) or []):
            if self._looks_like_audio_attachment(attachment):
                return attachment
        return None

    @staticmethod
    def _render_audio_transcription_preview(text: str) -> str:
        normalized = " ".join(str(text or "").split()).strip()
        if len(normalized) > Zork.AUDIO_PREVIEW_MAX_CHARS:
            normalized = normalized[: Zork.AUDIO_PREVIEW_MAX_CHARS - 3].rstrip() + "..."
        return normalized

    @staticmethod
    def _whisper_cpu_command_template() -> str:
        return str(
            os.getenv(
                "ZORK_WHISPER_CPU_CMD",
                "whisper {input} --output_dir {output_dir} --output_format txt --model base --fp16 False",
            )
        ).strip()

    @staticmethod
    def _whisper_cpu_timeout_seconds() -> int:
        raw = os.getenv("ZORK_WHISPER_CPU_TIMEOUT_SECONDS", "180")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 180
        return max(15, value)

    @classmethod
    def _transcribe_audio_bytes_with_whisper_cpu(
        cls,
        audio_bytes: bytes,
        *,
        filename: str,
    ) -> str:
        if not audio_bytes:
            raise RuntimeError("Empty audio payload.")
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            raise RuntimeError("ffmpeg is not installed.")
        command_template = cls._whisper_cpu_command_template()
        if not command_template:
            raise RuntimeError("ZORK_WHISPER_CPU_CMD is empty.")

        safe_filename = os.path.basename(str(filename or "audio.bin"))
        if not safe_filename:
            safe_filename = "audio.bin"

        with tempfile.TemporaryDirectory(prefix="zork-whisper-") as tmpdir:
            source_path = os.path.join(tmpdir, safe_filename)
            wav_path = os.path.join(tmpdir, "input.wav")
            with open(source_path, "wb") as handle:
                handle.write(audio_bytes)

            ffmpeg_result = subprocess.run(
                [
                    ffmpeg_bin,
                    "-y",
                    "-i",
                    source_path,
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    wav_path,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=cls._whisper_cpu_timeout_seconds(),
                check=False,
            )
            if ffmpeg_result.returncode != 0:
                stderr = " ".join(str(ffmpeg_result.stderr or "").split())
                raise RuntimeError(stderr or "ffmpeg failed to decode the audio attachment.")

            formatted = command_template.format(
                input=shlex.quote(wav_path),
                output_dir=shlex.quote(tmpdir),
            )
            command = shlex.split(formatted)
            if not command:
                raise RuntimeError("Whisper command is empty.")
            if shutil.which(command[0]) is None:
                raise RuntimeError(
                    f"{command[0]} is not installed. Install `openai-whisper` or set ZORK_WHISPER_CPU_CMD."
                )

            whisper_result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=cls._whisper_cpu_timeout_seconds(),
                check=False,
                cwd=tmpdir,
            )
            if whisper_result.returncode != 0:
                stderr = " ".join(str(whisper_result.stderr or "").split())
                raise RuntimeError(stderr or "whisper-cpu failed to transcribe the audio attachment.")

            stdout_text = " ".join(str(whisper_result.stdout or "").split()).strip()
            if stdout_text:
                return stdout_text

            txt_candidates = sorted(
                os.path.join(tmpdir, name)
                for name in os.listdir(tmpdir)
                if name.lower().endswith(".txt")
            )
            for candidate in txt_candidates:
                try:
                    with open(candidate, "r", encoding="utf-8") as handle:
                        text = " ".join(handle.read().split()).strip()
                except Exception:
                    continue
                if text:
                    return text

        raise RuntimeError("whisper-cpu produced no transcription text.")

    async def _handle_audio_transcription_message(
        self,
        message,
        *,
        campaign_id: str,
        attachment,
    ) -> None:
        reaction_added = await ZorkEmulator._add_processing_reaction(message)
        try:
            audio_bytes = await attachment.read()
            try:
                transcript = await asyncio.to_thread(
                    self._transcribe_audio_bytes_with_whisper_cpu,
                    audio_bytes,
                    filename=getattr(attachment, "filename", "audio.bin"),
                )
            except Exception as exc:
                logger.warning("Audio transcription failed for Zork message %s", getattr(message, "id", None), exc_info=True)
                await self._send_large_message(
                    message.channel,
                    f"{message.author.mention}\nAudio transcription failed: {exc}",
                    campaign_id=campaign_id,
                )
                return

            transcript = " ".join(str(transcript or "").split()).strip()
            if not transcript:
                await self._send_large_message(
                    message.channel,
                    f"{message.author.mention}\nAudio transcription produced no text.",
                    campaign_id=campaign_id,
                )
                return

            preview_text = self._render_audio_transcription_preview(transcript)
            preview_message = await self._send_large_message(
                message.channel,
                (
                    f"{message.author.mention}\n"
                    "[Audio transcription preview]\n"
                    f"{preview_text}\n\n"
                    "React with ✅ to use this as your turn text, or ❌ to discard it."
                ),
                campaign_id=campaign_id,
            )
            if preview_message is None:
                return
            self._pending_audio_transcriptions[str(preview_message.id)] = {
                "campaign_id": str(campaign_id),
                "actor_id": str(message.author.id),
                "source_message": message,
                "transcript_text": transcript,
            }
            for emoji in self.AUDIO_TRANSCRIPTION_REACTIONS:
                try:
                    await preview_message.add_reaction(emoji)
                except Exception:
                    logger.debug(
                        "Failed adding audio transcription reaction %s on %s",
                        emoji,
                        getattr(preview_message, "id", None),
                        exc_info=True,
                    )
        finally:
            if reaction_added:
                await ZorkEmulator._remove_processing_reaction(message)

    async def handle_audio_transcription_reaction(self, message, emoji: str, user) -> bool:
        if str(emoji or "") not in self.AUDIO_TRANSCRIPTION_REACTIONS:
            return False
        pending = self._pending_audio_transcriptions.get(str(getattr(message, "id", "")))
        if not isinstance(pending, dict):
            return False
        if user is None or getattr(user, "bot", False):
            return True
        owner_id = str(pending.get("actor_id") or "")
        campaign_id = str(pending.get("campaign_id") or "")
        if owner_id != str(getattr(user, "id", "")):
            await self._send_message(
                message.channel,
                f"{user.mention} only the original speaker can accept or reject that transcription."
                ,
                campaign_id=campaign_id,
            )
            return True

        transcript_text = str(pending.get("transcript_text") or "").strip()
        source_message = pending.get("source_message")
        self._pending_audio_transcriptions.pop(str(message.id), None)

        if str(emoji) == "❌":
            await message.edit(
                content=self._with_world_time(
                    f"{user.mention}\n❌ Audio transcription discarded.",
                    campaign_id,
                    ctx_like=message,
                )
            )
            return True

        preview_text = self._render_audio_transcription_preview(transcript_text)
        await message.edit(
            content=self._with_world_time(
                (
                    f"{user.mention}\n"
                    "✅ Using this audio transcription as turn text:\n"
                    f"{preview_text}"
                ),
                campaign_id,
                ctx_like=message,
            )
        )
        if source_message is None or not transcript_text:
            return True
        await self._process_campaign_message(
            source_message,
            campaign_id=campaign_id,
            content=transcript_text,
        )
        return True

    @classmethod
    def _is_turn_busy_text(cls, text: object) -> bool:
        return " ".join(str(text or "").split()) == cls.TURN_BUSY_TEXT

    def _get_turn_queue(self, key: tuple[str, str]) -> asyncio.Queue:
        queue = self._turn_queues.get(key)
        if queue is None:
            queue = asyncio.Queue()
            self._turn_queues[key] = queue
        return queue

    def _ensure_turn_queue_worker(self, key: tuple[str, str]) -> None:
        task = self._turn_queue_tasks.get(key)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._drain_turn_queue(key))
        self._turn_queue_tasks[key] = task

    async def _enqueue_turn_message(
        self,
        message,
        *,
        campaign_id: str,
        content: str,
    ) -> None:
        key = self._queue_key(campaign_id, message.author.id)
        queue = self._get_turn_queue(key)
        await queue.put(
            {
                "message": message,
                "campaign_id": str(campaign_id),
                "content": str(content or "").strip(),
            }
        )
        self._ensure_turn_queue_worker(key)
        await self._add_queue_reaction(message)

    async def _claim_turn_for_message(
        self,
        message,
        campaign_id: str,
        *,
        retry_if_busy: bool,
    ) -> tuple[str | None, str | None]:
        timed_event_notice_sent = False
        while True:
            claimed_campaign_id, error_text = await ZorkEmulator.begin_turn_for_campaign(
                message,
                campaign_id,
            )
            if error_text is not None:
                return None, error_text
            if claimed_campaign_id is not None:
                return claimed_campaign_id, None
            timed_event_notice = ZorkEmulator.get_timed_event_in_progress_notice(
                campaign_id,
                message.author.id,
            )
            if timed_event_notice and not timed_event_notice_sent:
                await self._send_large_message(
                    message.channel,
                    f"{message.author.mention}\n{timed_event_notice}",
                    campaign_id=campaign_id,
                )
                timed_event_notice_sent = True
            if not retry_if_busy:
                return None, None
            await asyncio.sleep(0.5)

    async def _process_campaign_message(
        self,
        message,
        *,
        campaign_id: str,
        content: str,
        retry_if_busy: bool = False,
    ) -> None:
        app = AppConfig.get_flask()
        if app is None:
            return

        if getattr(message, "guild", None) is not None and getattr(message, "channel", None) is not None:
            with app.app_context():
                channel_session = ZorkEmulator.get_or_create_channel(
                    message.guild.id,
                    message.channel.id,
                )
                channel_name = str(
                    getattr(message.channel, "name", "")
                    or getattr(message.channel, "topic", "")
                    or ""
                ).strip()
                if channel_name:
                    ZorkEmulator.set_channel_label(channel_session, channel_name)

        claimed_campaign_id, error_text = await self._claim_turn_for_message(
            message,
            campaign_id,
            retry_if_busy=retry_if_busy,
        )
        if error_text is not None:
            await self._send_message(
                message.channel,
                error_text,
                campaign_id=campaign_id,
            )
            return
        if claimed_campaign_id is None:
            timed_event_notice = ZorkEmulator.get_timed_event_in_progress_notice(
                campaign_id,
                message.author.id,
            )
            if timed_event_notice:
                await self._send_large_message(
                    message.channel,
                    f"{message.author.mention}\n{timed_event_notice}",
                    campaign_id=campaign_id,
                )
            elif not retry_if_busy:
                await self._enqueue_turn_message(
                    message,
                    campaign_id=campaign_id,
                    content=content,
                )
            return

        # Setup mode intercept — route to setup handler instead of play_action.
        with app.app_context():
            _setup_campaign = ZorkEmulator.query_campaign(claimed_campaign_id)
            if _setup_campaign is not None:
                self._sync_campaign_backend_state(
                    _setup_campaign,
                    channel_id=getattr(message.channel, "id", None),
                    sync_style_direction=getattr(message, "guild", None) is not None,
                )
            _in_setup = _setup_campaign and ZorkEmulator.is_in_setup_mode(
                _setup_campaign
            )
        if _in_setup:
            reaction_added = await ZorkEmulator._add_processing_reaction(message)
            try:
                with app.app_context():
                    _setup_campaign = ZorkEmulator.query_campaign(claimed_campaign_id)
                    response = await ZorkEmulator.handle_setup_message(
                        message, content, _setup_campaign, command_prefix=self._prefix()
                    )
                    if response:
                        await self._send_large_message(
                            message,
                            response,
                            campaign_id=claimed_campaign_id,
                        )
                        await self._notify_text_game_webui_turn_refresh(
                            campaign_id=claimed_campaign_id,
                            actor_id=message.author.id,
                        )
            finally:
                if reaction_added:
                    await ZorkEmulator._remove_processing_reaction(message)
                ZorkEmulator.end_turn(claimed_campaign_id, message.author.id)
            return

        shortcut_kind = self._bare_shortcut_kind(content)
        if shortcut_kind is not None:
            handled = await self._send_bare_shortcut_reply(
                message,
                shortcut_kind=shortcut_kind,
                campaign_id=claimed_campaign_id,
            )
            if handled:
                ZorkEmulator.end_turn(claimed_campaign_id, message.author.id)
                return

        reaction_added = await ZorkEmulator._add_processing_reaction(message)
        try:
            while True:
                narration = await ZorkEmulator.play_action(
                    message,
                    content,
                    command_prefix=self._prefix(),
                    campaign_id=claimed_campaign_id,
                    manage_claim=False,
                )
                if narration is None:
                    return
                if self._is_turn_busy_text(narration):
                    if retry_if_busy:
                        await asyncio.sleep(self.TURN_BUSY_RETRY_DELAY_SECONDS)
                        continue
                    await self._enqueue_turn_message(
                        message,
                        campaign_id=claimed_campaign_id,
                        content=content,
                    )
                    return
                notices = ZorkEmulator.pop_turn_ephemeral_notices(
                    claimed_campaign_id, message.author.id
                )
                msg = await self._send_action_reply(
                    message, narration, campaign_id=claimed_campaign_id, notices=notices
                )
                await self._notify_text_game_webui_turn_refresh(
                    campaign_id=claimed_campaign_id,
                    actor_id=message.author.id,
                )
                if msg is not None:
                    with app.app_context():
                        ZorkEmulator.record_turn_message_ids(
                            claimed_campaign_id, message.id, msg.id
                        )
                break
        finally:
            if reaction_added:
                await ZorkEmulator._remove_processing_reaction(message)
            ZorkEmulator.end_turn(claimed_campaign_id, message.author.id)

    async def _drain_turn_queue(self, key: tuple[str, str]) -> None:
        queue = self._turn_queues.get(key)
        if queue is None:
            return
        campaign_id, actor_id = key
        try:
            while True:
                item = await queue.get()
                try:
                    while True:
                        timed_event_notice = ZorkEmulator.get_timed_event_in_progress_notice(
                            campaign_id,
                            actor_id,
                        )
                        if not timed_event_notice:
                            break
                        await asyncio.sleep(0.5)
                    await self._process_campaign_message(
                        item["message"],
                        campaign_id=item["campaign_id"],
                        content=item["content"],
                        retry_if_busy=True,
                    )
                finally:
                    queue.task_done()
                if queue.empty():
                    break
        finally:
            task = self._turn_queue_tasks.get(key)
            current = asyncio.current_task()
            if queue.empty():
                self._turn_queues.pop(key, None)
                if task is current:
                    self._turn_queue_tasks.pop(key, None)
            else:
                self._turn_queues[key] = queue
                if task is current:
                    self._turn_queue_tasks.pop(key, None)
                self._ensure_turn_queue_worker(key)
            if task is current and key in self._turn_queue_tasks and self._turn_queue_tasks[key] is current:
                self._turn_queue_tasks.pop(key, None)

    def _ensure_guild(self, ctx) -> bool:
        if ctx.guild is None:
            return False
        return True

    def _sync_campaign_backend_state(
        self,
        campaign,
        *,
        channel_id: int | str | None = None,
        backend: str | None = None,
        model=None,
        thinking_enabled: bool | None = None,
        sync_style_direction: bool = True,
        style_direction: str | None = None,
    ) -> dict[str, object]:
        # Start from config defaults, then override with any explicit parameters.
        resolved = self.config.get_zork_backend_config(channel_id, default_backend="zai")
        if backend is not None:
            resolved["backend"] = str(backend or "").strip().lower() or "zai"
        if model is not None:
            resolved["model"] = self.config.normalize_zork_model_spec(model)
        if isinstance(thinking_enabled, bool):
            resolved["thinking_enabled"] = thinking_enabled
        state = ZorkEmulator.get_campaign_state(campaign)
        if not isinstance(state, dict):
            state = {}
        desired: dict[str, object] = {
            "backend": str(resolved.get("backend") or "zai").strip().lower() or "zai",
        }
        desired_model = self.config.normalize_zork_model_spec(resolved.get("model"))
        if desired_model:
            desired["model"] = desired_model
        desired["thinking_enabled"] = bool(resolved.get("thinking_enabled", True))
        state_changed = False
        if self.CAMPAIGN_BACKEND_STATE_KEY in state:
            state.pop(self.CAMPAIGN_BACKEND_STATE_KEY, None)
            state_changed = True
        self._sync_session_runtime_config(
            campaign,
            channel_id=channel_id,
            runtime_config=desired,
        )

        desired_style = None
        if sync_style_direction:
            desired_style = AppConfig.normalize_zork_style(
                style_direction
                if style_direction is not None
                else self.config.get_zork_style(
                    channel_id,
                    default_value=AppConfig.DEFAULT_ZORK_STYLE,
                ),
                default=AppConfig.DEFAULT_ZORK_STYLE,
            )
            if state.get(self.CAMPAIGN_STYLE_STATE_KEY) != desired_style:
                state[self.CAMPAIGN_STYLE_STATE_KEY] = desired_style
                state_changed = True

        if state_changed:
            campaign.state_json = ZorkEmulator._dump_json(state)
            ZorkEmulator.commit_model(campaign)
        return {
            "backend": desired["backend"],
            "model": desired.get("model"),
            "thinking_enabled": desired.get("thinking_enabled"),
            "style_direction": desired_style,
        }

    def _sync_session_runtime_config(
        self,
        campaign,
        *,
        channel_id: int | str | None,
        runtime_config: dict[str, object],
    ) -> None:
        from text_game_engine.persistence.sqlalchemy.models import Session as GameSession

        ZorkEmulator._ensure_init()
        with ZorkEmulator._session_factory() as session:
            query = session.query(GameSession).filter(GameSession.campaign_id == str(campaign.id))
            if channel_id is not None:
                channel_text = str(channel_id)
                query = query.filter(
                    or_(
                        GameSession.id == channel_text,
                        GameSession.surface_channel_id == channel_text,
                        GameSession.surface_thread_id == channel_text,
                    )
                )
            rows = query.all()
            changed = False
            for row in rows:
                try:
                    metadata = json.loads(row.metadata_json or "{}")
                except Exception:
                    metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}
                if metadata.get(self.SESSION_RUNTIME_CONFIG_KEY) == runtime_config:
                    continue
                metadata[self.SESSION_RUNTIME_CONFIG_KEY] = runtime_config
                row.metadata_json = json.dumps(metadata, ensure_ascii=True)
                row.updated_at = ZorkEmulator.utcnow()
                changed = True
            if changed:
                session.commit()

    def _infer_campaign_id(self, ctx_like) -> str | int | None:
        if ctx_like is None:
            return None
        app = AppConfig.get_flask()
        if app is None:
            return None
        with app.app_context():
            guild = getattr(ctx_like, "guild", None)
            channel = getattr(ctx_like, "channel", None)
            if channel is None and getattr(ctx_like, "id", None) is not None:
                channel = ctx_like
            guild_id = getattr(guild, "id", None)
            channel_id = getattr(channel, "id", None)
            if guild_id is not None and channel_id is not None:
                channel_rec = ZorkEmulator.get_or_create_channel(guild_id, channel_id)
                if getattr(channel_rec, "campaign_id", None):
                    return channel_rec.campaign_id
            if guild is None:
                author = getattr(ctx_like, "author", None)
                actor_id = getattr(author, "id", None)
                if actor_id is not None:
                    binding = self._get_private_dm_binding(actor_id)
                    if binding and binding.get("enabled") and binding.get("campaign_id"):
                        return binding.get("campaign_id")
        return None

    def _with_world_time(
        self,
        text: str,
        campaign_id: str | int | None = None,
        *,
        ctx_like=None,
    ) -> str:
        resolved_campaign_id = campaign_id
        resolved_actor_id = None
        if resolved_campaign_id is None:
            resolved_campaign_id = self._infer_campaign_id(ctx_like)
        author = getattr(ctx_like, "author", None) if ctx_like is not None else None
        if author is not None:
            resolved_actor_id = getattr(author, "id", None)
        if resolved_campaign_id is None:
            return str(text or "").strip()
        return ZorkEmulator.prepend_world_time_header(
            text,
            resolved_campaign_id,
            actor_id=resolved_actor_id,
        )

    @staticmethod
    def _looks_like_sms_notice(text: str) -> bool:
        body = str(text or "").strip().lower()
        if body.startswith("-# world time:"):
            body = "\n".join(body.splitlines()[1:]).strip()
        return "unread sms" in body or (
            "unread" in body and any(token in body for token in ("sms", "text", "message"))
        )

    async def _ensure_sms_notice_reactions(self, message, text: str) -> None:
        if message is None or not self._looks_like_sms_notice(text):
            return
        existing = {
            str(reaction.emoji): reaction
            for reaction in getattr(message, "reactions", []) or []
        }
        for emoji in self.SMS_NOTICE_REACTIONS:
            reaction = existing.get(emoji)
            if reaction is not None and getattr(reaction, "me", False):
                continue
            try:
                await message.add_reaction(emoji)
            except Exception:
                logger.debug(
                    "Failed adding SMS notice reaction %s on %s",
                    emoji,
                    getattr(message, "id", None),
                    exc_info=True,
                )

    def _wrap_send(self, target, *, campaign_id: str | int | None = None):
        resolved_campaign_id = campaign_id
        if resolved_campaign_id is None:
            resolved_campaign_id = self._infer_campaign_id(target)
        return _WorldTimeSendProxy(
            target,
            lambda text: self._with_world_time(text, resolved_campaign_id),
        )

    def cog_unload(self):
        task = self._webui_discord_echo_task
        self._webui_discord_echo_task = None
        if task is not None:
            task.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        task = self._webui_discord_echo_task
        if task is None or task.done():
            self._webui_discord_echo_task = asyncio.create_task(
                self._webui_discord_echo_loop()
            )

    async def _send_large_message(
        self,
        ctx_like,
        text: str,
        *,
        campaign_id: str | int | None = None,
        max_chars: int = 2000,
        delete_delay=None,
    ):
        payload = self._with_world_time(text, campaign_id, ctx_like=ctx_like)
        msg = await DiscordBot.send_large_message(
            ctx_like,
            payload,
            max_chars=max_chars,
            delete_delay=delete_delay,
        )
        await self._ensure_sms_notice_reactions(msg, payload)
        return msg

    async def _send_message(
        self,
        ctx_like,
        text: str,
        *,
        campaign_id: str | int | None = None,
        **kwargs,
    ):
        payload = self._with_world_time(text, campaign_id, ctx_like=ctx_like)
        msg = await ctx_like.send(payload, **kwargs)
        await self._ensure_sms_notice_reactions(msg, payload)
        return msg

    async def _webui_discord_echo_loop(self):
        while True:
            try:
                await self._drain_webui_discord_echo_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Failed draining webui Discord mirror outbox")
            await asyncio.sleep(self.WEBUI_DISCORD_ECHO_POLL_SECONDS)

    async def _drain_webui_discord_echo_once(self):
        from text_game_engine.persistence.sqlalchemy.models import (
            OutboxEvent,
            Session as GameSession,
        )

        ZorkEmulator._ensure_init()
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        with ZorkEmulator._session_factory() as session:
            pending = (
                session.query(OutboxEvent)
                .filter(
                    OutboxEvent.event_type == "webui_discord_echo",
                    OutboxEvent.status == "pending",
                    or_(
                        OutboxEvent.next_attempt_at.is_(None),
                        OutboxEvent.next_attempt_at <= now,
                    ),
                )
                .order_by(OutboxEvent.created_at.asc())
                .limit(10)
                .all()
            )
            work = [
                {
                    "id": str(row.id),
                    "campaign_id": str(row.campaign_id),
                    "payload_json": str(row.payload_json or ""),
                    "attempts": int(row.attempts or 0),
                }
                for row in pending
            ]

        for item in work:
            status = "consumed"
            next_attempt_at = None
            try:
                payload = json.loads(item["payload_json"] or "{}")
                if not isinstance(payload, dict):
                    payload = {}
                actor_id = str(payload.get("actor_id") or "").strip()
                aware_actor_ids = [
                    str(raw_actor_id or "").strip()
                    for raw_actor_id in list(payload.get("aware_actor_ids") or [])
                    if str(raw_actor_id or "").strip()
                ]
                other_discord_aware_actor_ids = [
                    aware_actor_id
                    for aware_actor_id in aware_actor_ids
                    if aware_actor_id != actor_id and self._is_probable_discord_actor_id(aware_actor_id)
                ]
                if not other_discord_aware_actor_ids:
                    continue
                scope = str(
                    ((payload.get("turn_visibility") or {}) if isinstance(payload.get("turn_visibility"), dict) else {}).get("scope")
                    or "public"
                ).strip().lower()
                if scope not in {"", "public", "local"}:
                    continue
                channel_id = self._resolve_campaign_discord_channel_id(item["campaign_id"])
                if not channel_id:
                    continue
                bot_instance = DiscordBot.get_instance()
                channel = await bot_instance.find_channel(int(channel_id)) if bot_instance is not None else None
                if channel is None:
                    continue
                source_name = (
                    str(payload.get("actor_display_name") or "").strip()
                    or str(payload.get("actor_id") or "").strip()
                    or "unknown"
                )
                narration = str(payload.get("narration") or "").strip()
                scene_output = payload.get("scene_output")
                rendered = self._format_scene_output_for_discord(narration, scene_output)
                rendered = self._filter_narration(rendered)
                rendered = self._prepend_webui_actor_input(
                    rendered,
                    source_name=source_name,
                    action_text=str(payload.get("action_text") or "").strip(),
                )
                if not rendered:
                    continue
                text = f"-# webui event from {source_name}\n{rendered}"
                await self._send_large_message(
                    channel,
                    text,
                    campaign_id=item["campaign_id"],
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Failed sending webui Discord mirror for campaign=%s event=%s",
                    item["campaign_id"],
                    item["id"],
                )
                status = "pending"
                next_attempt_at = datetime.datetime.now(datetime.timezone.utc).replace(
                    tzinfo=None
                ) + datetime.timedelta(seconds=30)
            finally:
                with ZorkEmulator._session_factory() as session:
                    row = session.get(OutboxEvent, item["id"])
                    if row is None:
                        continue
                    row.attempts = int(row.attempts or 0) + 1
                    row.status = status
                    row.next_attempt_at = next_attempt_at
                    session.commit()

    def _resolve_campaign_discord_channel_id(self, campaign_id: str | int | None) -> str | None:
        from text_game_engine.persistence.sqlalchemy.models import (
            Session as GameSession,
        )

        if campaign_id is None:
            return None
        ZorkEmulator._ensure_init()
        with ZorkEmulator._session_factory() as session:
            rows = (
                session.query(GameSession)
                .filter(
                    GameSession.campaign_id == str(campaign_id),
                    GameSession.enabled == True,  # noqa: E712
                )
                .order_by(GameSession.created_at.asc(), GameSession.id.asc())
                .all()
            )
        preferred = []
        fallback = []
        for row in rows:
            channel_id = str(
                getattr(row, "surface_thread_id", None)
                or getattr(row, "surface_channel_id", None)
                or ""
            ).strip()
            if not channel_id:
                continue
            surface = str(getattr(row, "surface", "") or "").strip().lower()
            target = preferred if surface.startswith("discord") else fallback
            target.append(channel_id)
        if preferred:
            return preferred[0]
        if fallback:
            return fallback[0]
        return None

    async def _is_image_admin(self, ctx) -> bool:
        user_roles = getattr(ctx.author, "roles", [])
        for role in user_roles:
            if role.name == "Image Admin":
                return True
        return False

    def _parse_thread_options(
        self, raw: str | None
    ) -> tuple[str | None, bool | None, str | None, bool]:
        if not raw:
            return None, None, None, False
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = str(raw).split()

        name_tokens: list[str] = []
        use_imdb: bool | None = None
        summary_instructions: str | None = None
        create_empty = False
        i = 0
        while i < len(tokens):
            token = str(tokens[i] or "")
            low = token.lower()
            if low == "--empty":
                create_empty = True
                i += 1
                continue
            if low == "--imdb":
                use_imdb = True
                i += 1
                continue
            if low in ("--no-imdb", "--noimdb"):
                use_imdb = False
                i += 1
                continue
            if low.startswith("--summary-instructions=") or low.startswith("--summary="):
                summary_instructions = token.split("=", 1)[1]
                i += 1
                continue
            if low in ("--summary-instructions", "--summary"):
                if i + 1 < len(tokens):
                    summary_instructions = str(tokens[i + 1] or "")
                    i += 2
                else:
                    i += 1
                continue
            name_tokens.append(token)
            i += 1

        parsed_name = " ".join(name_tokens).strip() or None
        if summary_instructions:
            summary_instructions = " ".join(summary_instructions.strip().split())[:600]
            if not summary_instructions:
                summary_instructions = None
        return parsed_name, use_imdb, summary_instructions, create_empty

    def _parse_source_material_options(
        self, raw: str | None
    ) -> tuple[str, str | None]:
        if not raw:
            return "ingest", None
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = str(raw).split()
        if not tokens:
            return "ingest", None

        label_tokens: list[str] = []
        i = 0
        while i < len(tokens):
            token = str(tokens[i] or "")
            low = token.lower()
            if low == "--clear":
                return "clear", None
            if low.startswith("--remove="):
                value = token.split("=", 1)[1].strip() or None
                return "remove", value
            if low == "--remove":
                value = str(tokens[i + 1] or "").strip() if i + 1 < len(tokens) else ""
                return "remove", value or None
            label_tokens.append(token)
            i += 1
        label = " ".join(label_tokens).strip() or None
        return "ingest", label

    def _parse_campaign_rules_options(
        self, raw: str | None
    ) -> tuple[str, str | None, str | None]:
        if not raw:
            return "list", None, None
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = str(raw).split()
        if not tokens:
            return "list", None, None

        first = str(tokens[0] or "").strip()
        first_low = first.lower()
        if first_low.startswith("--add="):
            return "add", first.split("=", 1)[1].strip() or None, " ".join(tokens[1:]).strip() or None
        if first_low.startswith("--upsert="):
            return "upsert", first.split("=", 1)[1].strip() or None, " ".join(tokens[1:]).strip() or None
        if first_low == "--add":
            key = str(tokens[1] or "").strip() if len(tokens) > 1 else None
            value = " ".join(tokens[2:]).strip() or None
            return "add", key, value
        if first_low == "--upsert":
            key = str(tokens[1] or "").strip() if len(tokens) > 1 else None
            value = " ".join(tokens[2:]).strip() or None
            return "upsert", key, value
        return "get", " ".join(tokens).strip() or None, None

    def _parse_literary_reference_options(
        self, raw: str | None
    ) -> tuple[str, str | None]:
        """Parse options for the literary-reference command.

        Returns ``(operation, value)`` where operation is one of
        ``"analyze"``, ``"clear"``, ``"remove"``, ``"list"``.
        """
        if not raw:
            return "analyze", None
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = str(raw).split()
        if not tokens:
            return "analyze", None

        label_tokens: list[str] = []
        i = 0
        while i < len(tokens):
            token = str(tokens[i] or "")
            low = token.lower()
            if low == "--clear":
                return "clear", None
            if low == "--list":
                return "list", None
            if low.startswith("--remove="):
                value = token.split("=", 1)[1].strip() or None
                return "remove", value
            if low == "--remove":
                value = str(tokens[i + 1] or "").strip() if i + 1 < len(tokens) else ""
                return "remove", value or None
            label_tokens.append(token)
            i += 1
        label = " ".join(label_tokens).strip() or None
        return "analyze", label

    def _parse_campaign_export_options(
        self,
        raw: str | None,
    ) -> tuple[str, str]:
        export_type = "full"
        raw_format = "jsonl"
        if not raw:
            return export_type, raw_format
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = str(raw).split()
        i = 0
        while i < len(tokens):
            token = str(tokens[i] or "")
            low = token.lower()
            if low.startswith("--type="):
                export_type = token.split("=", 1)[1].strip().lower() or export_type
                i += 1
                continue
            if low == "--type":
                if i + 1 < len(tokens):
                    export_type = str(tokens[i + 1] or "").strip().lower() or export_type
                    i += 2
                else:
                    i += 1
                continue
            if low.startswith("--raw-format="):
                raw_format = token.split("=", 1)[1].strip().lower() or raw_format
                i += 1
                continue
            if low == "--raw-format":
                if i + 1 < len(tokens):
                    raw_format = str(tokens[i + 1] or "").strip().lower() or raw_format
                    i += 2
                else:
                    i += 1
                continue
            i += 1
        if export_type not in {"full", "raw"}:
            export_type = "full"
        if raw_format not in {"script", "markdown", "json", "jsonl", "loglines"}:
            raw_format = "jsonl"
        return export_type, raw_format

    def _source_material_export_text(
        self,
        document_key: str,
        units: list[str],
    ) -> str:
        clean_units = [str(unit or "").strip() for unit in units if str(unit or "").strip()]
        if not clean_units:
            return ""
        sample = "\n".join(clean_units[:6])
        inferred_format = ZorkEmulator._source_material_format_heuristic(sample)
        if inferred_format == ZorkEmulator.SOURCE_MATERIAL_FORMAT_RULEBOOK:
            return "\n".join(clean_units).strip()
        if inferred_format == ZorkEmulator.SOURCE_MATERIAL_FORMAT_STORY:
            return "\n\n".join(clean_units).strip()
        if str(document_key or "").strip().lower() == "message":
            return "\n\n".join(clean_units).strip()
        return "\n\n".join(clean_units).strip()

    def _source_material_export_filename(
        self,
        document_key: str,
        document_label: str | None = None,
        *,
        used_names: set[str] | None = None,
    ) -> str:
        label = " ".join(str(document_label or "").strip().split())
        if label:
            base = label[:180]
        else:
            key = str(document_key or "").strip().lower()
            if not key:
                key = "source-material"
            base = ZorkMemory._normalize_source_document_key(key) or "source-material"
            base = base[:180]
        filename = f"{base}.txt"
        if used_names is None:
            return filename
        if filename not in used_names:
            used_names.add(filename)
            return filename

        fallback_key = (
            ZorkMemory._normalize_source_document_key(str(document_key or "").strip())
            or "source-material"
        )[:80]
        suffix = 2
        while True:
            candidate = f"{base} ({fallback_key}-{suffix}).txt"
            if candidate not in used_names:
                used_names.add(candidate)
                return candidate
            suffix += 1

    def _get_private_dm_binding(self, user_id: int) -> dict | None:
        binding = self.config.get_zork_private_dm(user_id)
        if not isinstance(binding, dict) or not binding.get("enabled"):
            return None
        raw_cid = binding.get("campaign_id")
        if not raw_cid:
            return None
        # campaign_id may be a UUID string (new TGE) or an old integer.
        # Keep it as a string in both cases — the bridge resolves it.
        campaign_id = str(raw_cid)
        if not campaign_id or campaign_id == "0":
            return None
        result = dict(binding)
        result["campaign_id"] = campaign_id
        for key in ("guild_id", "channel_id"):
            try:
                value = int(result.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            result[key] = value or None
        result["campaign_name"] = str(result.get("campaign_name") or "").strip() or None
        return result

    def _should_ignore_message(self, message) -> bool:
        if message.type != discord.MessageType.default and message.type != discord.MessageType.reply:
            return True
        if message.author.bot:
            return True
        content = message.content.strip()
        if not content:
            return True
        prefix = self._prefix()
        if content.startswith(prefix):
            return True
        if content.startswith("<@&") or content.startswith("<#"):
            return True
        if message.mentions:
            first_mention = message.mentions[0]
            mention_tokens = (f"<@{first_mention.id}>", f"<@!{first_mention.id}>")
            if content.startswith(mention_tokens):
                if first_mention.id != self.bot.user.id:
                    return True
        return False

    def _strip_bot_mention(self, message_content: str) -> str:
        if not self.bot or not self.bot.user:
            return message_content
        return (
            message_content.replace(f"<@{self.bot.user.id}>", "")
            .replace(f"<@!{self.bot.user.id}>", "")
            .strip()
        )

    _NARRATION_LINE_FILTERS = ("psychological distress",)

    # TTS emotive tags to strip from display text
    _EMOTIVE_TAG_RE = re.compile(
        r"<(?:giggle|laughter|guffaw|sigh|cry|gasp|groan"
        r"|inhale|exhale|whisper|mumble|uh|um"
        r"|singing|humming|cough|sneeze|sniff|clear_throat"
        r"|shhh|quiet)>",
        re.IGNORECASE,
    )

    @classmethod
    def _filter_narration(cls, text: str) -> str:
        lines = text.split("\n")
        filtered = [
            line for line in lines
            if not any(f in line.lower() for f in cls._NARRATION_LINE_FILTERS)
        ]
        # Strip TTS emotive tags from display
        result = "\n".join(filtered)
        return cls._EMOTIVE_TAG_RE.sub("", result).strip()

    @staticmethod
    def _format_scene_speaker_name(raw: object) -> str:
        text = str(raw or "").strip()
        if not text:
            return "narrator"
        if text.startswith("<@"):
            return text
        if text.lower() == "narrator":
            return "narrator"
        if text.lower() == text and "-" in text:
            parts = [part for part in text.split("-") if part]
            if parts:
                return " ".join(part.capitalize() for part in parts)
        return text

    @classmethod
    def _should_format_scene_output(cls, narration: str) -> bool:
        text = str(narration or "").strip()
        if not text:
            return False
        lowered = text.lower()
        if "timed event in progress" in lowered:
            return False
        if lowered.startswith("another turn is already resolving"):
            return False
        return True

    @classmethod
    def _extract_status_prefix_lines(cls, narration: str) -> list[str]:
        text = str(narration or "").strip()
        if not text:
            return []
        kept: list[str] = []
        for raw_line in text.splitlines():
            line = str(raw_line or "").strip()
            if not line:
                continue
            if line.startswith(("⏰", "⚠️", "✅")):
                kept.append(line)
        return kept

    @classmethod
    def _contains_pending_timer_line(cls, narration: str) -> bool:
        text = str(narration or "").strip()
        if not text:
            return False
        return any(
            str(raw_line or "").strip().startswith("⏰ *Timed event:*")
            for raw_line in text.splitlines()
        )

    @staticmethod
    def _is_probable_discord_actor_id(value: object) -> bool:
        text = str(value or "").strip()
        return bool(text and text.isdigit())

    @classmethod
    def _format_scene_output_for_discord(
        cls,
        narration: str,
        scene_output: object,
    ) -> str:
        if not isinstance(scene_output, dict):
            return str(narration or "").strip()
        beats = scene_output.get("beats")
        if not isinstance(beats, list) or not beats:
            return str(narration or "").strip()

        rendered_beats: list[str] = []
        for beat in beats:
            if not isinstance(beat, dict):
                continue
            text = str(beat.get("text") or "").strip()
            if not text:
                continue
            # Strip TTS emotive tags (e.g. <laughter>, <sigh>) from display
            text = cls._EMOTIVE_TAG_RE.sub("", text).strip()
            text = re.sub(r"\s{2,}", " ", text)
            if not text:
                continue
            speaker = cls._format_scene_speaker_name(beat.get("speaker"))
            rendered_beats.append(f"-# speaker: {speaker}\n{text}")

        if rendered_beats:
            rendered = "\n\n".join(rendered_beats)
            status_lines = cls._extract_status_prefix_lines(narration)
            if status_lines:
                return rendered + "\n\n" + "\n".join(status_lines)
            return rendered
        return str(narration or "").strip()

    @classmethod
    def _prepend_webui_actor_input(
        cls,
        rendered: str,
        *,
        source_name: str,
        action_text: str,
    ) -> str:
        action = str(action_text or "").strip()
        if not action:
            return str(rendered or "").strip()
        opening = f"-# speaker: {cls._format_scene_speaker_name(source_name)}\n{action}"
        body = str(rendered or "").strip()
        if not body:
            return opening
        return f"{opening}\n\n{body}"

    @classmethod
    def _format_preset_campaigns(
        cls, active_campaign_id: int | None, campaigns
    ) -> list[str]:
        if not getattr(ZorkEmulator, "PRESET_CAMPAIGNS", None):
            return []

        preset_rows: dict[str, object] = {}
        for campaign in campaigns:
            normalized = ZorkEmulator._normalize_campaign_name(campaign.name or "")
            preset_key = ZorkEmulator.PRESET_ALIASES.get(normalized)
            if preset_key and preset_key in ZorkEmulator.PRESET_CAMPAIGNS:
                preset_rows[preset_key] = campaign

        out: list[str] = []
        for preset_key in ZorkEmulator.PRESET_CAMPAIGNS:
            marker = "-"
            row = preset_rows.get(preset_key)
            if row is not None and getattr(row, "id", None) == active_campaign_id:
                marker = "*"
            out.append(f"{marker} {preset_key}")
        return out

    async def _send_action_reply(
        self, ctx_like, narration: str, campaign_id: int = None, notices: list[str] | None = None
    ):
        for notice in notices or []:
            await self._send_large_message(ctx_like, f"[Notice] {notice}", campaign_id=campaign_id)
        if campaign_id is not None and self._should_format_scene_output(narration):
            actor_id = getattr(getattr(ctx_like, "author", None), "id", None)
            scene_output = ZorkEmulator.get_latest_scene_output_for_actor(
                campaign_id,
                actor_id,
            )
            narration = self._format_scene_output_for_discord(narration, scene_output)
        narration = self._filter_narration(narration)
        mention = getattr(getattr(ctx_like, "author", None), "mention", None)
        if mention:
            msg = await self._send_large_message(
                ctx_like, f"{mention}\n{narration}", campaign_id=campaign_id
            )
        else:
            msg = await self._send_large_message(ctx_like, narration, campaign_id=campaign_id)
        timer_bound = False
        if campaign_id is not None and msg is not None and self._contains_pending_timer_line(narration):
            timer_bound = bool(ZorkEmulator.register_timer_message(campaign_id, msg.id))
        if msg is not None:
            try:
                await msg.add_reaction("ℹ️")
                if timer_bound:
                    await msg.add_reaction("⏲️")
                await msg.add_reaction("⏪")
                await msg.add_reaction("❌")
            except Exception:
                logger.debug("Failed adding Zork turn reactions", exc_info=True)
        return msg

    @staticmethod
    def _bare_shortcut_kind(raw: object) -> str | None:
        text = " ".join(str(raw or "").strip().lower().split())
        if text in {"calendar", "cal", "events"}:
            return "calendar"
        if text in {"chapters", "outline"}:
            return "chapters"
        if text in {"roster", "characters", "npcs"}:
            return "roster"
        if text in {"inventory", "inv", "i"}:
            return "inventory"
        return None

    @staticmethod
    def _render_chapter_outline_text(chapter_data: dict[str, object]) -> str:
        chapters = chapter_data.get("chapters", []) if isinstance(chapter_data, dict) else []
        if not chapters:
            return "No chapters found for this campaign."
        lines = [f"__**Chapter Outline**__ ({len(chapters)} chapters)\n"]
        for ch in chapters:
            if ch.get("is_current"):
                icon = "\u25B6"
            elif ch.get("status") in ("completed", "resolved"):
                icon = "\u2713"
            else:
                icon = "\u25CB"
            title = ch.get("title", "Untitled")
            summary = ch.get("summary", "")
            lines.append(f"{icon} **{title}**")
            if summary and ch.get("is_current"):
                lines.append(f"   {summary}")
            if ch.get("is_current") and ch.get("scenes"):
                for sc in ch["scenes"]:
                    marker = "\u25B8 " if sc.get("is_current") else "  "
                    sc_title = sc.get("title", "Untitled")
                    bold = f"**{sc_title}**" if sc.get("is_current") else sc_title
                    lines.append(f"   {marker}{bold}")
        return "\n".join(lines)

    def _resolve_active_campaign(self, ctx_like, campaign_id: str | None = None):
        if campaign_id is not None:
            return ZorkEmulator.query_campaign(campaign_id)
        if getattr(ctx_like, "guild", None) is None:
            return None
        channel = ZorkEmulator.get_or_create_channel(
            ctx_like.guild.id,
            ctx_like.channel.id,
        )
        if channel.campaign_id is None:
            return None
        return ZorkEmulator.query_campaign_for_channel(channel)

    async def _send_bare_shortcut_reply(
        self,
        ctx_like,
        *,
        shortcut_kind: str,
        campaign_id: str | None = None,
    ) -> bool:
        app = AppConfig.get_flask()
        if app is None:
            return False
        with app.app_context():
            campaign = self._resolve_active_campaign(ctx_like, campaign_id=campaign_id)
            if campaign is None:
                await self._send_large_message(
                    ctx_like,
                    "No active campaign in this channel.",
                )
                return True
            if shortcut_kind == "calendar":
                text = ZorkEmulator.get_calendar_text(
                    campaign.id,
                    getattr(getattr(ctx_like, "author", None), "id", None),
                )
            elif shortcut_kind == "chapters":
                text = self._render_chapter_outline_text(
                    ZorkEmulator.get_chapter_list(campaign)
                )
            elif shortcut_kind == "roster":
                text = ZorkEmulator.format_roster(
                    ZorkEmulator.get_campaign_characters(campaign)
                )
            elif shortcut_kind == "inventory":
                actor_id = getattr(getattr(ctx_like, "author", None), "id", None)
                if actor_id is None:
                    return False
                player = ZorkEmulator.get_or_create_player(
                    campaign.id,
                    actor_id,
                    campaign=campaign,
                )
                player_state = ZorkEmulator.get_player_state(player)
                text = ZorkEmulator._format_inventory(player_state) or "Inventory: empty"
            else:
                return False
        await self._send_large_message(ctx_like, text, campaign_id=campaign.id)
        return True

    async def _prepare_thread_source_material(
        self,
        ctx,
        campaign,
        *,
        channel,
        summary_instructions: str | None = None,
        default_label: str | None = None,
    ) -> tuple[str | None, str | None, dict]:
        attachment_infos = await ZorkEmulator._extract_attachment_texts_from_message(
            ctx.message
        )
        if not attachment_infos:
            return None, None, {}

        fallback_label = str(default_label or "source-material").strip() or "source-material"

        summary_parts: list[str] = []
        ingest_messages: list[str] = []
        all_literary_profiles: dict = {}
        for attachment, attachment_text in attachment_infos:
            if isinstance(attachment_text, str) and attachment_text.startswith("ERROR:"):
                await self._send_message(
                    channel,
                    attachment_text.replace("ERROR:", "", 1),
                    campaign_id=campaign.id,
                )
                continue
            if not attachment_text:
                continue

            chunks, _, _, _, _ = ZorkEmulator._chunk_text_by_tokens(attachment_text)
            if not chunks:
                continue

            classification_chunk = chunks[0]
            try:
                source_format = await ZorkEmulator._classify_source_material_format(
                    classification_chunk,
                    campaign=campaign,
                    channel_id=getattr(channel, "id", None),
                )
            except Exception:
                source_format = ZorkEmulator.SOURCE_MATERIAL_FORMAT_GENERIC

            attachment_label = ZorkEmulator._extract_attachment_label(
                [attachment],
                fallback=fallback_label,
            )

            if source_format == ZorkEmulator.SOURCE_MATERIAL_FORMAT_GENERIC:
                summary = await ZorkEmulator._summarise_long_text(
                    attachment_text,
                    ctx.message,
                    channel=channel,
                    campaign=campaign,
                    summary_instructions=summary_instructions,
                )
                if summary:
                    summary_parts.append(f"{attachment_label}:\n{summary}")
                continue

            ingest_ok, ingest_message, literary_profiles = await ZorkEmulator.ingest_source_material_text(
                campaign,
                attachment_text,
                label=attachment_label,
                channel=channel,
                source_format=source_format,
                message=ctx.message,
            )
            if ingest_ok:
                if ingest_message:
                    ingest_messages.append(ingest_message)
            elif "No `.txt` attachment found." not in ingest_message:
                ingest_messages.append(ingest_message)
            if literary_profiles:
                all_literary_profiles.update(literary_profiles)

        attachment_summary = "\n\n".join(summary_parts).strip() or None
        ingest_message = "; ".join(ingest_messages).strip() or None
        return attachment_summary, ingest_message, all_literary_profiles

    async def _handle_source_material_command(self, ctx, *, label: str = None):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return

        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
        operation, parsed_value = self._parse_source_material_options(label)

        if operation == "clear":
            with app.app_context():
                docs = SourceMaterialMemory.list_source_material_documents(
                    campaign.id,
                    limit=200,
                )
                removed_rows = SourceMaterialMemory.clear_source_material_documents(
                    campaign.id
                )
            if not docs:
                await ctx.send("No source-material documents are stored for this campaign.")
                return
            await ctx.send(
                f"Cleared {len(docs)} source-material document(s) "
                f"({removed_rows} stored snippet row(s)) from `{campaign.name}`."
            )
            return

        if operation == "remove":
            requested = str(parsed_value or "").strip()
            if not requested:
                prefix = self._prefix()
                await ctx.send(
                    f"Usage: `{prefix}zork source-material --remove <document-key>`"
                )
                return
            with app.app_context():
                docs = SourceMaterialMemory.list_source_material_documents(
                    campaign.id,
                    limit=200,
                )
                requested_norm = ZorkMemory._normalize_source_document_key(requested)
                match = None
                for row in docs:
                    row_key = str(row.get("document_key") or "").strip()
                    row_label = " ".join(
                        str(row.get("document_label") or "").strip().split()
                    )
                    if requested in {row_key, row_label} or requested_norm == row_key:
                        match = row
                        break
                if not match:
                    await ctx.send(
                        "Source-material document not found. "
                        f"Requested `{requested}`."
                    )
                    return
                row_key = str(match.get("document_key") or "").strip()
                row_label = str(match.get("document_label") or "").strip() or row_key
                removed_rows = SourceMaterialMemory.delete_source_material_document(
                    campaign.id,
                    row_key,
                )
            if removed_rows <= 0:
                await ctx.send(
                    f"Could not remove source-material document `{row_key}`."
                )
                return
            await ctx.send(
                f"Removed source-material document `{row_label}` "
                f"(key `{row_key}`, {removed_rows} stored snippet row(s))."
            )
            return

        reaction_added = await ZorkEmulator._add_processing_reaction(ctx)
        try:
            summary, message, literary_profiles = await self._prepare_thread_source_material(
                ctx,
                campaign,
                channel=ctx.channel,
                default_label=label,
                summary_instructions=None,
            )
            ok = True
            if summary is None and (
                message is None or "No `.txt` attachment found." in str(message)
            ):
                ok = False
            if ok and summary and not message:
                message = summary
            elif ok and message and summary:
                message = f"{summary}\n\n{message}"
            if literary_profiles:
                with app.app_context():
                    campaign = ZorkEmulator.query_campaign(campaign.id)
                    state = ZorkEmulator.get_campaign_state(campaign)
                    styles = state.get(ZorkEmulator.LITERARY_STYLES_STATE_KEY)
                    if not isinstance(styles, dict):
                        styles = {}
                    styles.update(literary_profiles)
                    state[ZorkEmulator.LITERARY_STYLES_STATE_KEY] = styles
                    campaign.state_json = ZorkEmulator._dump_json(state)
                    campaign.updated_at = ZorkEmulator.utcnow()
                    ZorkEmulator.commit_model(campaign)
        finally:
            if reaction_added:
                await ZorkEmulator._remove_processing_reaction(ctx)

        message_text = str(message or "")

        if not ok and "No `.txt` attachment found." in message_text:
            prefix = self._prefix()
            await ctx.send(
                "Attach a `.txt` file to ingest source material.\n"
                f"Usage: `{prefix}zork source-material [label]`\n"
                f"Or manage stored docs with `{prefix}zork source-material --remove <document-key>` "
                f"or `{prefix}zork source-material --clear`."
            )
            return
        await self._send_large_message(
            ctx,
            message_text or "No source-material changes were made.",
            max_chars=3900,
        )

    async def _handle_literary_reference_command(self, ctx, *, label: str = None):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return

        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return

        operation, parsed_value = self._parse_literary_reference_options(label)

        if operation == "list":
            with app.app_context():
                campaign = ZorkEmulator.query_campaign(campaign.id)
                state = ZorkEmulator.get_campaign_state(campaign)
                styles = state.get(ZorkEmulator.LITERARY_STYLES_STATE_KEY)
            if not isinstance(styles, dict) or not styles:
                await ctx.send("No literary style profiles stored for this campaign.")
                return
            lines = []
            for key in sorted(styles.keys()):
                entry = styles[key]
                if not isinstance(entry, dict):
                    continue
                profile = str(entry.get("profile") or "").strip()
                truncated = (profile[:120] + "...") if len(profile) > 120 else profile
                lines.append(f"**{key}**: {truncated}")
            await self._send_large_message(
                ctx,
                f"Literary style profiles ({len(lines)}):\n" + "\n".join(lines),
                max_chars=3900,
            )
            return

        if operation == "clear":
            with app.app_context():
                campaign = ZorkEmulator.query_campaign(campaign.id)
                state = ZorkEmulator.get_campaign_state(campaign)
                styles = state.get(ZorkEmulator.LITERARY_STYLES_STATE_KEY)
                if not isinstance(styles, dict) or not styles:
                    await ctx.send("No literary style profiles to clear.")
                    return
                count = len(styles)
                state.pop(ZorkEmulator.LITERARY_STYLES_STATE_KEY, None)
                campaign.state_json = ZorkEmulator._dump_json(state)
                campaign.updated_at = ZorkEmulator.utcnow()
                ZorkEmulator.commit_model(campaign)
            await ctx.send(f"Cleared {count} literary style profile(s).")
            return

        if operation == "remove":
            requested = str(parsed_value or "").strip()
            if not requested:
                prefix = self._prefix()
                await ctx.send(
                    f"Usage: `{prefix}zork literary-reference --remove <label>`"
                )
                return
            with app.app_context():
                campaign = ZorkEmulator.query_campaign(campaign.id)
                state = ZorkEmulator.get_campaign_state(campaign)
                styles = state.get(ZorkEmulator.LITERARY_STYLES_STATE_KEY)
                if not isinstance(styles, dict) or not styles:
                    await ctx.send("No literary style profiles stored.")
                    return
                # Remove exact key + all sub-keys (label-*)
                keys_to_remove = [
                    k for k in styles
                    if k == requested or k.startswith(f"{requested}-")
                ]
                if not keys_to_remove:
                    await ctx.send(f"No literary style profiles matching `{requested}`.")
                    return
                for k in keys_to_remove:
                    styles.pop(k, None)
                if not styles:
                    state.pop(ZorkEmulator.LITERARY_STYLES_STATE_KEY, None)
                campaign.state_json = ZorkEmulator._dump_json(state)
                campaign.updated_at = ZorkEmulator.utcnow()
                ZorkEmulator.commit_model(campaign)
            await ctx.send(
                f"Removed {len(keys_to_remove)} literary style profile(s): "
                + ", ".join(f"`{k}`" for k in sorted(keys_to_remove))
            )
            return

        # Default: analyze
        # Require .txt attachment
        raw_text = await ZorkEmulator._extract_attachment_text(ctx.message)
        if not raw_text:
            prefix = self._prefix()
            await ctx.send(
                "Attach a `.txt` file containing literary prose to analyse.\n"
                f"Usage: `{prefix}zork literary-reference [label]`\n"
                f"Or manage stored profiles with `{prefix}zork literary-reference --list`, "
                f"`{prefix}zork literary-reference --remove <label>`, "
                f"or `{prefix}zork literary-reference --clear`."
            )
            return

        # Derive label from argument or filename
        if not parsed_value:
            for attachment in ctx.message.attachments:
                if attachment.filename.lower().endswith(".txt"):
                    parsed_value = attachment.filename.rsplit(".", 1)[0]
                    break
        if not parsed_value:
            parsed_value = "unnamed"
        # Normalize label: lowercase, spaces->hyphens, max 60 chars
        normalized_label = (
            str(parsed_value).strip().lower().replace(" ", "-").replace("_", "-")[:60]
        )

        reaction_added = await ZorkEmulator._add_processing_reaction(ctx)
        try:
            profiles = await ZorkEmulator._analyze_literary_style(
                raw_text,
                normalized_label,
                campaign=campaign,
                channel_id=ctx.channel.id,
            )
        finally:
            if reaction_added:
                await ZorkEmulator._remove_processing_reaction(ctx)

        if not profiles:
            await ctx.send("Could not extract any literary style profiles from the attached text.")
            return

        # Store profiles in campaign_state
        with app.app_context():
            campaign = ZorkEmulator.query_campaign(campaign.id)
            state = ZorkEmulator.get_campaign_state(campaign)
            styles = state.get(ZorkEmulator.LITERARY_STYLES_STATE_KEY)
            if not isinstance(styles, dict):
                styles = {}
            styles.update(profiles)
            state[ZorkEmulator.LITERARY_STYLES_STATE_KEY] = styles
            campaign.state_json = ZorkEmulator._dump_json(state)
            campaign.updated_at = ZorkEmulator.utcnow()
            ZorkEmulator.commit_model(campaign)

        keys = sorted(profiles.keys())
        keys_text = ", ".join(f"`{k}`" for k in keys)
        await ctx.send(
            f"Stored {len(profiles)} literary style profile(s): {keys_text}\n"
            f"Characters can reference these via `literary_style` in character_updates."
        )

    async def _handle_campaign_rules_command(self, ctx, *, raw: str = None):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return

        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return

        operation, key, value = self._parse_campaign_rules_options(raw)

        if operation == "list":
            with app.app_context():
                rules = ZorkEmulator.list_campaign_rules(campaign.id)
            if not rules:
                await ctx.send("No campaign rules are stored in `campaign-rulebook`.")
                return
            lines = [f"`{row['key']}`" for row in rules if str(row.get("key") or "").strip()]
            await self._send_large_message(
                ctx,
                f"Campaign rules ({len(lines)}):\n" + "\n".join(lines),
                max_chars=3900,
            )
            return

        if operation == "get":
            requested = str(key or "").strip()
            if not requested:
                await ctx.send("Provide a campaign rule key or use `--add` / `--upsert`.")
                return
            with app.app_context():
                rule = ZorkEmulator.get_campaign_rule(campaign.id, requested)
            if not rule:
                await ctx.send(f"Campaign rule `{requested}` not found.")
                return
            await self._send_large_message(
                ctx,
                f"`{rule['key']}`: {rule['value']}",
                max_chars=3900,
            )
            return

        requested_key = str(key or "").strip()
        requested_value = str(value or "").strip()
        if not requested_key or not requested_value:
            prefix = self._prefix()
            await ctx.send(
                f"Usage: `{prefix}zork campaign-rules --{operation} <KEY> <rule text>`"
            )
            return

        with app.app_context():
            result = ZorkEmulator.put_campaign_rule(
                campaign.id,
                rule_key=requested_key,
                rule_text=requested_value,
                upsert=(operation == "upsert"),
            )

        if not result.get("ok"):
            if result.get("reason") == "exists":
                await self._send_large_message(
                    ctx,
                    f"Campaign rule `{result.get('key')}` already exists.\n"
                    f"Old: {result.get('old_value')}\n"
                    "Use `--upsert` to replace it.",
                    max_chars=3900,
                )
                return
            await ctx.send("Could not store campaign rule.")
            return

        key_text = str(result.get("key") or requested_key)
        new_value = str(result.get("new_value") or requested_value)
        old_value = str(result.get("old_value") or "").strip()
        if result.get("replaced"):
            await self._send_large_message(
                ctx,
                f"Updated campaign rule `{key_text}`.\n"
                f"Old: {old_value}\n"
                f"New: {new_value}",
                max_chars=3900,
            )
            return

        await self._send_large_message(
            ctx,
            f"Added campaign rule `{key_text}`.\n"
            f"New: {new_value}",
            max_chars=3900,
        )

    async def _handle_rewind(self, message, app):
        """Process a 'rewind' reply: restore state and purge messages."""
        target_msg_id = message.reference.message_id
        dm_scope = message.guild is None
        dm_binding = None

        with app.app_context():
            if dm_scope:
                dm_binding = self._get_private_dm_binding(message.author.id)
                if dm_binding is None:
                    await self._send_message(message.channel, "No active campaign in this DM.")
                    return
                campaign_id = dm_binding["campaign_id"]
            else:
                channel_rec = ZorkEmulator.get_or_create_channel(
                    message.guild.id, message.channel.id
                )
                if not channel_rec.enabled or channel_rec.campaign_id is None:
                    await self._send_message(message.channel, "No active campaign in this channel.")
                    return
                campaign_id = channel_rec.campaign_id
            campaign = ZorkEmulator.query_campaign(campaign_id)
            if campaign is None:
                await self._send_message(message.channel, "Campaign not found.", campaign_id=campaign_id)
                return
            # Normalize to the campaign's real UUID in case we resolved a legacy ID.
            campaign_id = campaign.id
            if ZorkEmulator.is_in_setup_mode(campaign):
                await self._send_message(
                    message.channel,
                    "Cannot rewind during campaign setup.",
                    campaign_id=campaign_id,
                )
                return

        lock = ZorkEmulator._get_lock(campaign_id)
        async with lock:
            with app.app_context():
                result = ZorkEmulator.execute_rewind(
                    campaign_id,
                    target_msg_id,
                    channel_id=message.channel.id,
                    rewind_user_id=message.author.id if dm_scope else None,
                    player_only=dm_scope,
                )

            if result is None:
                await self._send_message(
                    message.channel,
                    "Could not find a snapshot for that message. "
                    "Only messages created after the rewind feature was added can be rewound to.",
                    campaign_id=campaign_id,
                )
                return

            turn_id, deleted_count = result

            # Purge Discord messages after the target.
            try:
                target_msg = await message.channel.fetch_message(target_msg_id)
                await self._purge_messages_after(message.channel, target_msg, message)
            except discord.NotFound:
                pass
            except Exception:
                logger.exception("Zork rewind: failed to purge messages")

            if not dm_scope:
                # Guild/thread rewind restores whole campaign snapshot, so global
                # timers and queued SMS deliveries must be cleared.
                ZorkEmulator.cancel_pending_timer(campaign_id)
                ZorkEmulator.cancel_pending_sms_deliveries(campaign_id)

            if dm_scope:
                await self._send_message(
                    message.channel,
                    f"Rewound your DM thread to turn {turn_id}. Removed {deleted_count} of your subsequent turn(s)."
                )
            else:
                await self._send_message(
                    message.channel,
                    f"Rewound to turn {turn_id}. Removed {deleted_count} subsequent turn(s)."
                    ,
                    campaign_id=campaign_id,
                )

    async def _purge_messages_after(self, channel, target_message, rewind_message):
        """Delete messages in *channel* that come after *target_message*."""
        keep_ids = {target_message.id, rewind_message.id}

        def should_delete(m):
            return m.id not in keep_ids

        try:
            await channel.purge(after=target_message, check=should_delete, limit=200)
        except Exception:
            logger.exception("Zork rewind: purge failed")

    @commands.Cog.listener()
    async def on_message(self, message):
        if self._should_ignore_message(message):
            return
        app = AppConfig.get_flask()
        if app is None:
            return
        content = self._strip_bot_mention(message.content)
        audio_attachment = self._first_audio_attachment(message)
        has_audio_only_input = bool(audio_attachment is not None and not content)

        campaign_id = None
        if message.guild is None:
            binding = self._get_private_dm_binding(message.author.id)
            if binding is None:
                return
            with app.app_context():
                campaign = ZorkEmulator.query_campaign(binding["campaign_id"])
                if campaign is None:
                    self.config.clear_zork_private_dm(message.author.id)
                    await self._send_message(
                        message.channel,
                        "Your linked private Zork campaign no longer exists. "
                        f"Re-enable it from the campaign channel with `{self._prefix()}zork private enable`.",
                        campaign_id=binding["campaign_id"],
                    )
                    return
                if ZorkEmulator.is_in_setup_mode(campaign):
                    await self._send_message(
                        message.channel,
                        f"Campaign `{campaign.name}` is still in setup. "
                        "Finish setup in the server channel or thread before using private DMs.",
                        campaign_id=binding["campaign_id"],
                    )
                    return
            campaign_id = str(binding["campaign_id"])
        else:
            with app.app_context():
                if not ZorkEmulator.is_channel_enabled(
                    message.guild.id, message.channel.id
                ):
                    return
            inferred_campaign_id = self._infer_campaign_id(message)
            if inferred_campaign_id is None:
                return
            campaign_id = str(inferred_campaign_id)

        if has_audio_only_input:
            await self._handle_audio_transcription_message(
                message,
                campaign_id=campaign_id,
                attachment=audio_attachment,
            )
            return
        if not content:
            return

        # Rewind detection — must happen before begin_turn.
        content_stripped = message.content.strip().lower()
        if (
            message.guild is not None
            and
            content_stripped == "rewind"
            and message.reference is not None
            and message.reference.message_id is not None
        ):
            await self._handle_rewind(message, app)
            return

        queued_content = self._strip_queue_prefix(content)
        if queued_content is not None:
            await self._enqueue_turn_message(
                message,
                campaign_id=campaign_id,
                content=queued_content,
            )
            return

        await self._process_campaign_message(
            message,
            campaign_id=campaign_id,
            content=content,
        )

    @commands.group(name="zork", invoke_without_command=True)
    async def zork(self, ctx, *, action: str = None):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return

        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return

        if action is not None:
            action_stripped = str(action or "").strip()
            if action_stripped.startswith("source-material") or action_stripped.startswith("source_material"):
                rest = action_stripped.split(None, 1)[1].strip() if len(action_stripped.split(None, 1)) > 1 else None
                await self._handle_source_material_command(ctx, label=rest)
                return
            if action_stripped.startswith("campaign-rules") or action_stripped.startswith("campaign_rules"):
                rest = action_stripped.split(None, 1)[1].strip() if len(action_stripped.split(None, 1)) > 1 else None
                await self._handle_campaign_rules_command(ctx, raw=rest)
                return
            if action_stripped.startswith("literary-reference") or action_stripped.startswith("literary_reference"):
                rest = action_stripped.split(None, 1)[1].strip() if len(action_stripped.split(None, 1)) > 1 else None
                await self._handle_literary_reference_command(ctx, label=rest)
                return

        if action is None:
            with app.app_context():
                channel = ZorkEmulator.get_or_create_channel(
                    ctx.guild.id, ctx.channel.id
                )
                if not channel.enabled:
                    _, campaign = ZorkEmulator.enable_channel(
                        ctx.guild.id, ctx.channel.id, ctx.author.id
                    )
                    message = (
                        f"Adventure mode enabled for this channel. Active campaign: `{campaign.name}`.\n"
                        f"Use `{self._prefix()}zork help` to see commands."
                    )
                    await ctx.send(message)
                    if campaign.last_narration:
                        await self._send_large_message(
                            ctx, campaign.last_narration
                        )
                    return

                campaign = (
                    ZorkEmulator.query_campaign_for_channel(channel)
                    if channel.campaign_id
                    else None
                )
                if campaign is None:
                    _, campaign = ZorkEmulator.enable_channel(
                        ctx.guild.id, ctx.channel.id, ctx.author.id
                    )
                campaign_name = campaign.name
                await ctx.send(
                    f"Adventure mode is already enabled. Active campaign: `{campaign_name}`.\n"
                    f"Use `{self._prefix()}zork help` to see commands."
                )
                return

        campaign_id, error_text = await ZorkEmulator.begin_turn(
            ctx, command_prefix=self._prefix()
        )
        if error_text is not None:
            await ctx.send(error_text)
            return
        if campaign_id is None:
            return

        # Setup mode intercept
        with app.app_context():
            _setup_campaign = ZorkEmulator.query_campaign(campaign_id)
            if _setup_campaign is not None:
                self._sync_campaign_backend_state(
                    _setup_campaign,
                    channel_id=getattr(ctx.channel, "id", None),
                    sync_style_direction=True,
                )
            _in_setup = _setup_campaign and ZorkEmulator.is_in_setup_mode(
                _setup_campaign
            )
        if _in_setup:
            reaction_added = await ZorkEmulator._add_processing_reaction(ctx)
            try:
                with app.app_context():
                    _setup_campaign = ZorkEmulator.query_campaign(campaign_id)
                    response = await ZorkEmulator.handle_setup_message(
                        ctx, action, _setup_campaign, command_prefix=self._prefix()
                    )
                    if response:
                        await self._send_large_message(ctx, response, campaign_id=campaign_id)
                        await self._notify_text_game_webui_turn_refresh(
                            campaign_id=campaign_id,
                            actor_id=ctx.author.id,
                        )
            finally:
                if reaction_added:
                    await ZorkEmulator._remove_processing_reaction(ctx)
                ZorkEmulator.end_turn(campaign_id, ctx.author.id)
            return

        reaction_added = await ZorkEmulator._add_processing_reaction(ctx)
        try:
            narration = await ZorkEmulator.play_action(
                ctx,
                action,
                command_prefix=self._prefix(),
                campaign_id=campaign_id,
                manage_claim=False,
            )
            if narration is None:
                return
            notices = ZorkEmulator.pop_turn_ephemeral_notices(
                campaign_id, ctx.author.id
            )
            msg = await self._send_action_reply(
                ctx, narration, campaign_id=campaign_id, notices=notices
            )
            await self._notify_text_game_webui_turn_refresh(
                campaign_id=campaign_id,
                actor_id=ctx.author.id,
            )
            if msg is not None:
                with app.app_context():
                    ZorkEmulator.record_turn_message_ids(
                        campaign_id, ctx.message.id, msg.id
                    )
        finally:
            if reaction_added:
                await ZorkEmulator._remove_processing_reaction(ctx)
            ZorkEmulator.end_turn(campaign_id, ctx.author.id)

    @zork.command(name="help")
    async def zork_help(self, ctx):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        prefix = self._prefix()
        message = (
            f"Zork commands:\n"
            f"- `{prefix}zork` enable adventure mode in this channel\n"
            f"- `{prefix}zork <action>` take an action (ex: look, open door, take lamp)\n"
            f"- `{prefix}zork thread [name] [--empty] [--imdb|--no-imdb] [--summary-instructions \"...\"]` create a dedicated Zork thread/campaign for yourself (`--imdb` is opt-in; `--empty` skips auto setup)\n"
            f"- `{prefix}zork share [thread-id]` show this thread/channel id or bind this channel/thread to another Zork thread's active campaign, even across servers\n"
            f"- `{prefix}zork source-material [label]` ingest attached `.txt` as campaign canon memory; "
            "format is auto-detected as story/rulebook/generic.\n"
            f"- `{prefix}zork source-material --remove <document-key>` remove one stored source document from the active campaign\n"
            f"- `{prefix}zork source-material --clear` remove all stored source documents from the active campaign\n"
            f"- `{prefix}zork source-material-export` export stored source documents back into `.txt` attachments in this thread/channel\n"
            f"- `{prefix}zork campaign-rules` list all keys in `campaign-rulebook`\n"
            f"- `{prefix}zork campaign-rules <KEY>` show one campaign rule\n"
            f"- `{prefix}zork campaign-rules --add <KEY> <rule...>` add one rule without replacing\n"
            f"- `{prefix}zork campaign-rules --upsert <KEY> <rule...>` create or replace one rule\n"
            f"- `{prefix}zork literary-reference [label]` analyse attached `.txt` prose and extract literary style profiles for characters\n"
            f"- `{prefix}zork literary-reference --list` show stored literary style profiles\n"
            f"- `{prefix}zork literary-reference --remove <label>` remove a literary style profile (and sub-keys)\n"
            f"- `{prefix}zork literary-reference --clear` remove all literary style profiles\n"
            f"- `{prefix}zork campaign-export [--type full|raw] [--raw-format jsonl|json|markdown|script|loglines]` export the campaign and stored source docs\n"
            f"- `{prefix}zork backend [zai|codex|claude|gemini|opencode] [model]` view or set the text backend/model for this channel/thread (creator/admin only to change)\n"
            f"- `{prefix}zork thinking [enable|disable]` view or set backend thinking/reasoning when supported (creator/admin only to change)\n"
            f"- `{prefix}zork style [prompt|default]` view or set the style direction for this channel/thread (max 120 chars; creator/admin only to change)\n"
            f"- `{prefix}zork private [enable|disable]` bind your DMs to the current campaign so your turns stay private but shared history stays in-world\n"
            f"- `{prefix}zork link-account <uuid>` link your Discord account to the DTM web UI session currently waiting on that code\n"
            f"- `{prefix}zork campaigns` list campaigns\n"
            f"- `{prefix}zork campaign <name>` switch or create campaign\n"
            f"- `{prefix}zork identity <name>` set your character name\n"
            f"- `{prefix}zork persona <text>` set your character persona\n"
            f"- `{prefix}zork rails` show strict guardrails mode status for active campaign\n"
            f"- `{prefix}zork rails enable|disable` toggle strict on-rails action validation for active campaign\n"
            f"- `{prefix}zork on-rails` show on-rails narrative mode status\n"
            f"- `{prefix}zork on-rails enable|disable` lock/unlock story to the chapter outline\n"
            f"- `{prefix}zork timed-events` show timed events status; enable/disable toggles\n"
            f"- `{prefix}zork speed [value]` view or set in-world clock speed multiplier (0.1–10.0, creator/admin only)\n"
            f"- `{prefix}zork timed-events-speed [value]` view or set realtime timed-event speed multiplier (0.1–10.0, creator/admin only)\n"
            f"- `{prefix}zork clock [HH[:MM]|DAY HH[:MM]|WEEKDAY DAY HH[:MM]]` view or set the campaign's global clock (creator/bot owner only)\n"
            f"- `{prefix}zork clock-type [loose-calendar|consequential-calendar|individual-calendars]` view or set the campaign time/calendar mode (creator/bot owner only)\n"
            f"- `{prefix}zork difficulty [story|easy|medium|normal|hard|impossible]` view or set difficulty template (creator/admin only)\n"
            f"- `{prefix}zork roster` view the NPC character roster with portraits\n"
            f"- `{prefix}zork roster <name> portrait` regenerate portrait for a character\n"
            f"- `{prefix}zork avatar <prompt|accept|decline>` generate/accept/decline your character avatar\n"
            f"- `{prefix}zork attributes` view attributes and points\n"
            f"- `{prefix}zork attributes <name> <value>` set or create attribute\n"
            f"- `{prefix}zork stats` view player stats\n"
            f"- `{prefix}zork hint` view your currently visible imminent plot hints\n"
            f"- `{prefix}zork puzzles [none|light|moderate|full]` view or set puzzle encounter mode\n"
            f"- `{prefix}zork level` level up if you have enough XP\n"
            f"- `{prefix}zork inventory` view your current inventory\n"
            f"- `{prefix}zork map` draw an ASCII map for your location\n"
            f"- `{prefix}zork reset` reset this channel's Zork state (Image Admin only)\n"
            f"- `{prefix}zork disable` disable adventure mode in this channel\n"
            f"\n**In-game shortcuts** (type directly, no prefix):\n"
            f"- `{prefix}zork chapters` / `outline` — view chapter outline & progress\n"
            f"- `calendar` / `cal` / `events` — view game time & upcoming events\n"
            f"- `roster` / `characters` / `npcs` — view the NPC roster\n"
            f"- `inventory` / `inv` / `i` — view your inventory\n"
        )
        await self._send_large_message(ctx, message)

    @zork.command(name="calendar", aliases=["cal", "events"])
    async def zork_calendar(self, ctx):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            text = ZorkEmulator.get_calendar_text(channel.campaign_id, ctx.author.id)
        await self._send_large_message(ctx, text)

    @zork.command(name="chapters", aliases=["outline"])
    async def zork_chapters(self, ctx):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign(channel.campaign_id)
            if campaign is None:
                await ctx.send("Campaign not found.")
                return
            chapter_data = ZorkEmulator.get_chapter_list(campaign)
        await self._send_large_message(
            ctx,
            self._render_chapter_outline_text(chapter_data),
        )

    @zork.command(name="inventory", aliases=["inv", "i"])
    async def zork_inventory(self, ctx):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        handled = await self._send_bare_shortcut_reply(
            ctx,
            shortcut_kind="inventory",
        )
        if not handled:
            await ctx.send("No active campaign in this channel.")

    @zork.command(name="enable")
    async def zork_enable(self, ctx):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            _, campaign = ZorkEmulator.enable_channel(
                ctx.guild.id, ctx.channel.id, ctx.author.id
            )
            await ctx.send(
                f"Adventure mode enabled. Active campaign: `{campaign.name}`."
            )

    @zork.command(name="disable")
    async def zork_disable(self, ctx):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            channel.enabled = False
            channel.updated_at = ZorkEmulator.utcnow()
            ZorkEmulator.commit_model(channel)
            await ctx.send("Adventure mode disabled for this channel.")

    @zork.command(name="campaigns")
    async def zork_campaigns(self, ctx):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            campaigns = ZorkEmulator.list_campaigns(ctx.guild.id)
            if not campaigns and not ZorkEmulator.PRESET_CAMPAIGNS:
                await ctx.send(
                    f"No campaigns yet. Use `{self._prefix()}zork campaign <name>` to create one."
                )
                return
            active_id = channel.campaign_id
            lines = self._format_preset_campaigns(active_id, campaigns)
            if not lines:
                await ctx.send(
                    f"No campaigns yet. Use `{self._prefix()}zork campaign <name>` to create one."
                )
                return
            await self._send_large_message(ctx, "Campaigns:\n" + "\n".join(lines))

    @zork.command(name="campaign")
    async def zork_campaign(self, ctx, *, name: str = None):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if name is None:
                if channel.campaign_id is None:
                    await ctx.send("No active campaign in this channel.")
                    return
                campaign = ZorkEmulator.query_campaign_for_channel(channel)
                campaign_name = campaign.name if campaign else "unknown"
                await ctx.send(f"Active campaign: `{campaign_name}`.")
                return
            campaign, allowed, reason = ZorkEmulator.set_active_campaign(
                channel,
                ctx.guild.id,
                name,
                ctx.author.id,
                enforce_activity_window=not isinstance(ctx.channel, discord.Thread),
            )
            if not allowed:
                await ctx.send(f"Cannot switch campaigns: {reason}.")
                return
            await ctx.send(f"Active campaign set to `{campaign.name}`.")

    @zork.group(name="rails", invoke_without_command=True)
    async def zork_rails(self, ctx):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            rails_on = ZorkEmulator.is_guardrails_enabled(campaign)
            mode = "enabled" if rails_on else "disabled"
            await ctx.send(
                f"Rails mode is `{mode}` for campaign `{campaign.name}`.\n"
                f"Use `{self._prefix()}zork rails enable` or `{self._prefix()}zork rails disable`."
            )

    @zork_rails.command(name="enable")
    async def zork_rails_enable(self, ctx):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            ZorkEmulator.set_guardrails_enabled(campaign, True)
            await ctx.send(f"Rails mode enabled for campaign `{campaign.name}`.")

    @zork_rails.command(name="disable")
    async def zork_rails_disable(self, ctx):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            ZorkEmulator.set_guardrails_enabled(campaign, False)
            await ctx.send(f"Rails mode disabled for campaign `{campaign.name}`.")

    @zork.group(name="on-rails", invoke_without_command=True)
    async def zork_on_rails(self, ctx):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            on_rails = ZorkEmulator.is_on_rails(campaign)
            mode = "enabled" if on_rails else "disabled"
            await ctx.send(
                f"On-rails mode is `{mode}` for campaign `{campaign.name}`.\n"
                f"Use `{self._prefix()}zork on-rails enable` or `{self._prefix()}zork on-rails disable`."
            )

    @zork_on_rails.command(name="enable")
    async def zork_on_rails_enable(self, ctx):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            ZorkEmulator.set_on_rails(campaign, True)
            await ctx.send(f"On-rails mode enabled for campaign `{campaign.name}`.")

    @zork_on_rails.command(name="disable")
    async def zork_on_rails_disable(self, ctx):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            ZorkEmulator.set_on_rails(campaign, False)
            await ctx.send(f"On-rails mode disabled for campaign `{campaign.name}`.")

    @zork.command(name="puzzles")
    async def zork_puzzles(self, ctx, *, mode: str = None):
        """View or set puzzle mode for active campaign."""
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            state = ZorkEmulator.get_campaign_state(campaign)
            if mode is None:
                current = state.get("puzzle_mode", "none")
                prefix = self._prefix()
                await ctx.send(
                    f"Puzzle mode is `{current}` for campaign `{campaign.name}`.\n"
                    f"Modes: **none** (no mechanical challenges), **light** (environmental puzzles only), "
                    f"**moderate** (+ skill checks & riddles), **full** (+ mini-games).\n"
                    f"Set with: `{prefix}zork puzzles <mode>`"
                )
                return
            normalized = mode.strip().lower()
            valid_modes = ("none", "light", "moderate", "full")
            if normalized not in valid_modes:
                await ctx.send(f"Invalid mode. Choose one of: {', '.join(valid_modes)}")
                return
            state["puzzle_mode"] = normalized
            campaign.state_json = ZorkEmulator._dump_json(state)
            from discord_tron_master.classes.app_config import AppConfig as AC
            ZorkEmulator.commit_model(campaign)
            await ctx.send(f"Puzzle mode set to `{normalized}` for campaign `{campaign.name}`.")

    @zork.group(name="timed-events", invoke_without_command=True)
    async def zork_timed_events(self, ctx):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            enabled = ZorkEmulator.is_timed_events_enabled(campaign)
            mode = "enabled" if enabled else "disabled"
            await ctx.send(
                f"Timed events are `{mode}` for campaign `{campaign.name}`.\n"
                f"Use `{self._prefix()}zork timed-events enable` or `{self._prefix()}zork timed-events disable`."
            )

    @zork_timed_events.command(name="enable")
    async def zork_timed_events_enable(self, ctx):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            # TODO: campaign.created_by_actor_id is a string actor_id, not a Discord int user ID.
            # This comparison needs a bridge lookup to map actor_id -> Discord user ID.
            if campaign.created_by_actor_id != str(ctx.author.id) and not await self._is_image_admin(
                ctx
            ):
                await ctx.send(
                    "Only the campaign creator or an Image Admin can change this setting."
                )
                return
            ZorkEmulator.set_timed_events_enabled(campaign, True)
            await ctx.send(f"Timed events enabled for campaign `{campaign.name}`.")

    @zork_timed_events.command(name="disable")
    async def zork_timed_events_disable(self, ctx):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            # TODO: campaign.created_by_actor_id is a string actor_id, not a Discord int user ID.
            # This comparison needs a bridge lookup to map actor_id -> Discord user ID.
            if campaign.created_by_actor_id != str(ctx.author.id) and not await self._is_image_admin(
                ctx
            ):
                await ctx.send(
                    "Only the campaign creator or an Image Admin can change this setting."
                )
                return
            ZorkEmulator.set_timed_events_enabled(campaign, False)
            await ctx.send(f"Timed events disabled for campaign `{campaign.name}`.")

    @zork.command(name="identity")
    async def zork_identity(self, ctx, *, character_name: str = None):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            player = ZorkEmulator.get_or_create_player(
                campaign.id, ctx.author.id, campaign=campaign
            )
            player_state = ZorkEmulator.get_player_state(player)

            if character_name is None:
                current_name = player_state.get("character_name")
                if current_name:
                    await ctx.send(f"Current identity: `{current_name}`.")
                else:
                    await ctx.send(
                        f"No identity set. Use `{self._prefix()}zork identity <name>`."
                    )
                return

            character_name = character_name.strip()
            if not character_name:
                await ctx.send("Identity cannot be empty.")
                return
            character_name = character_name[:64]
            old_name = player_state.get("character_name")
            player_state["character_name"] = character_name
            ZorkEmulator._persist_player_state_for_campaign_actor(
                campaign.id,
                ctx.author.id,
                player_state,
            )
            if old_name and isinstance(old_name, str) and old_name != character_name:
                campaign.summary = (campaign.summary or "").replace(
                    old_name, character_name
                )
                campaign.updated_at = ZorkEmulator.utcnow()
                ZorkEmulator.commit_model(campaign)
            await ctx.send(f"Identity set to `{character_name}`.")

    @zork.command(name="persona")
    async def zork_persona(self, ctx, *, persona: str = None):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            player = ZorkEmulator.get_or_create_player(
                campaign.id, ctx.author.id, campaign=campaign
            )
            player_state = ZorkEmulator.get_player_state(player)
            campaign_state = ZorkEmulator.get_campaign_state(campaign)

            if persona is None:
                current_persona = player_state.get("persona")
                default_persona = ZorkEmulator.get_campaign_default_persona(
                    campaign,
                    campaign_state=campaign_state,
                )
                message = (
                    f"Your persona: `{current_persona}`\n"
                    f"Campaign default persona: `{default_persona}`"
                )
                await self._send_large_message(ctx, message)
                return

            persona = persona.strip()
            if not persona:
                await ctx.send("Persona cannot be empty.")
                return
            persona = persona[:400]
            player_state["persona"] = persona
            player.state_json = ZorkEmulator._dump_json(player_state)
            player.updated_at = ZorkEmulator.utcnow()
            ZorkEmulator.commit_model(player)
            await ctx.send("Persona updated for your character.")

    @zork.command(name="private")
    async def zork_private(self, ctx, *, mode: str = None):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Run this in the campaign channel or thread you want to bind.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return

        command = str(mode or "").strip().lower()
        binding = self._get_private_dm_binding(ctx.author.id)

        if command in ("disable", "off"):
            if binding is None:
                await ctx.send("Private DMs are already disabled for you.")
                return
            self.config.clear_zork_private_dm(ctx.author.id)
            bound_name = binding.get("campaign_name") or f"campaign {binding['campaign_id']}"
            await ctx.send(
                f"Private DMs disabled for `{bound_name}`. "
                "Your future turns will only come from normal server messages."
            )
            return

        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            campaign = (
                ZorkEmulator.query_campaign_for_channel(channel)
                if channel.campaign_id
                else None
            )
            player = None
            player_state = {}
            if campaign is not None:
                player = ZorkEmulator.get_or_create_player(
                    campaign.id, ctx.author.id, campaign=campaign
                )
                player_state = ZorkEmulator.get_player_state(player)

        if command in ("", "status"):
            if binding is None:
                if campaign is None:
                    await ctx.send(
                        "Private DMs are disabled. No active campaign is bound in this channel."
                    )
                else:
                    await ctx.send(
                        f"Private DMs are disabled. Use `{self._prefix()}zork private enable` "
                        f"to bind your DMs to `{campaign.name}`."
                    )
                return
            current_name = campaign.name if campaign is not None else None
            bound_name = binding.get("campaign_name") or f"campaign {binding['campaign_id']}"
            if current_name and current_name == bound_name:
                await ctx.send(
                    f"Private DMs are enabled for `{bound_name}` from this channel. "
                    "Send plain messages to the bot in DM to play privately."
                )
            else:
                await ctx.send(
                    f"Private DMs are enabled for `{bound_name}`. "
                    f"Use `{self._prefix()}zork private enable` here to rebind to `{current_name}`."
                    if current_name
                    else f"Private DMs are enabled for `{bound_name}`."
                )
            return

        if command not in ("enable", "on"):
            await ctx.send(
                f"Usage: `{self._prefix()}zork private [enable|disable]`"
            )
            return

        if campaign is None:
            await ctx.send("No active campaign in this channel.")
            return
        if ZorkEmulator.is_in_setup_mode(campaign):
            await ctx.send(
                "Finish campaign setup before enabling private DMs for this campaign."
            )
            return
        character_name = str(player_state.get("character_name") or "").strip()
        if not character_name:
            await ctx.send(
                f"Set your identity first with `{self._prefix()}zork identity <name>`, "
                "then enable private DMs."
            )
            return

        self.config.set_zork_private_dm(
            ctx.author.id,
            enabled=True,
            campaign_id=campaign.id,
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            campaign_name=campaign.name,
        )
        await ctx.send(
            f"Private DMs enabled for `{campaign.name}` as `{character_name}`.\n"
            "Send plain messages to the bot in DM and they will act in this shared campaign."
        )

    @zork.command(name="link-account")
    async def zork_link_account(self, ctx, code: str = None):
        ctx = self._wrap_send(ctx)
        if not code:
            await ctx.send(
                f"Usage: `{self._prefix()}zork link-account <uuid>`"
            )
            return
        try:
            normalized_code = str(uuid.UUID(str(code).strip()))
        except (TypeError, ValueError):
            await ctx.send("Link code must be a valid UUID.")
            return
        if not self.config.is_text_game_webui_enabled():
            await ctx.send("text-game-webui is not enabled in config.")
            return
        try:
            result = await self._confirm_text_game_webui_link(
                code=normalized_code,
                actor_id=str(ctx.author.id),
                display_name=str(getattr(ctx.author, "display_name", "") or getattr(ctx.author, "name", "") or ""),
            )
        except Exception as exc:
            await ctx.send(f"Could not link web UI account: {exc}")
            return
        linked_name = str(result.get("display_name") or getattr(ctx.author, "display_name", "") or "").strip()
        if linked_name:
            await ctx.send(
                f"Web UI linked for `{linked_name}`. Return to the browser and it should unlock automatically."
            )
        else:
            await ctx.send(
                "Web UI linked. Return to the browser and it should unlock automatically."
            )

    @staticmethod
    def _split_top_level_csv(text):
        """Split `text` on commas at bracket depth 0; preserves nested `[...]` groups."""
        parts: list[str] = []
        buf: list[str] = []
        depth = 0
        for ch in text:
            if ch == "[":
                depth += 1
                buf.append(ch)
            elif ch == "]":
                depth -= 1
                if depth < 0:
                    raise ValueError("unbalanced brackets in model list")
                buf.append(ch)
            elif ch == "," and depth == 0:
                parts.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
        if depth != 0:
            raise ValueError("unbalanced brackets in model list")
        parts.append("".join(buf))
        return parts

    @classmethod
    def _parse_zork_backend_model_arg(cls, text):
        """Parse the model portion of `!zork backend <backend> <model>`.

        Returns:
            None for "no model";
            str for a single model;
            dict {"research", "narration"} for `[research, narration]` phased pair;
            list of (str | dict) for a random pool — each element is either a plain
            model or a phased pair.

        Raises ValueError for malformed input (empty brackets, wrong arity in pairs,
        unbalanced brackets).
        """
        body = str(text or "").strip()
        if not body:
            return None
        if not (body.startswith("[") and body.endswith("]")):
            return body
        inner = body[1:-1].strip()
        if not inner:
            raise ValueError("model list `[...]` cannot be empty")
        raw_parts = cls._split_top_level_csv(inner)
        items: list = []
        for raw in raw_parts:
            part = raw.strip()
            if not part:
                continue
            if part.startswith("[") and part.endswith("]"):
                pair = [
                    item.strip()
                    for item in part[1:-1].split(",")
                    if item.strip()
                ]
                if len(pair) != 2:
                    raise ValueError(
                        "phased pair `[research, narration]` requires exactly two items"
                    )
                items.append({"research": pair[0], "narration": pair[1]})
            else:
                items.append(part)
        if not items:
            raise ValueError("model list `[...]` cannot be empty")
        if len(items) == 1:
            return items[0]
        return items

    @staticmethod
    def _format_zork_backend_model(model_value):
        if not model_value:
            return None
        if isinstance(model_value, dict):
            return (
                f"research=`{model_value.get('research')}`, "
                f"narration=`{model_value.get('narration')}`"
            )
        if isinstance(model_value, list):
            rendered = []
            for item in model_value:
                if isinstance(item, dict):
                    rendered.append(
                        f"[research=`{item.get('research')}`, narration=`{item.get('narration')}`]"
                    )
                else:
                    rendered.append(f"`{item}`")
            return "random pool: " + ", ".join(rendered)
        return f"`{model_value}`"

    @zork.command(name="backend")
    async def zork_backend(self, ctx, *, option: str = None):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return

        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return

        current = self.config.get_zork_backend_config(
            ctx.channel.id,
            default_backend="zai",
        )
        current_backend = str(current.get("backend") or "zai").strip() or "zai"
        current_model = current.get("model") or None
        allowed = ", ".join(f"`{item}`" for item in AppConfig.ZORK_BACKEND_OPTIONS)
        if option is None:
            display = self._format_zork_backend_model(current_model) or "`default`"
            await ctx.send(
                f"Current Zork backend for this channel/thread: `{current_backend}`.\n"
                f"Current model override: {display}\n"
                f"Available backends: {allowed}"
            )
            return

        body = str(option or "").strip()
        backend_token, _, raw_model = body.partition(" ")
        normalized = self.config.normalize_zork_backend(backend_token, default="")
        if normalized not in AppConfig.ZORK_BACKEND_OPTIONS:
            await ctx.send(
                f"Unknown backend `{option}`. Available backends: {allowed}"
            )
            return
        try:
            model = self._parse_zork_backend_model_arg(raw_model)
        except ValueError as exc:
            await ctx.send(f"Invalid model spec: {exc}")
            return

        # TODO: campaign.created_by_actor_id is a string actor_id, not a Discord int user ID.
        # This comparison needs a bridge lookup to map actor_id -> Discord user ID.
        if campaign.created_by_actor_id != str(ctx.author.id) and not await self._is_image_admin(ctx):
            await ctx.send(
                "Only the campaign creator or an Image Admin can change this setting."
            )
            return

        self.config.set_zork_backend(ctx.channel.id, normalized, model=model)
        with app.app_context():
            campaign = ZorkEmulator.query_campaign(campaign.id)
            if campaign is not None:
                self._sync_campaign_backend_state(
                    campaign,
                    channel_id=ctx.channel.id,
                    backend=normalized,
                    model=model,
                )
        display = self._format_zork_backend_model(model)
        model_text = f" with model {display}" if display else " with the backend default model"
        await ctx.send(
            f"Zork backend for `{campaign.name}` in this channel/thread set to `{normalized}`{model_text}."
        )

    @zork.command(name="thinking")
    async def zork_thinking(self, ctx, *, option: str = None):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return

        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return

        current = self.config.get_zork_backend_config(
            ctx.channel.id,
            default_backend="zai",
        )
        backend = str(current.get("backend") or "zai").strip() or "zai"
        enabled = bool(current.get("thinking_enabled", True))
        support_note = (
            "Supported by this backend."
            if backend in self.THINKING_SUPPORTED_BACKENDS
            else f"Currently ignored by backend `{backend}`."
        )
        if option is None:
            state_text = "enabled" if enabled else "disabled"
            await ctx.send(
                f"Current LLM thinking for this channel/thread: `{state_text}`.\n"
                f"Backend: `{backend}`. {support_note}\n"
                f"Use `{self._prefix()}zork thinking enable` or `{self._prefix()}zork thinking disable` to change it."
            )
            return

        normalized = " ".join(str(option or "").strip().lower().split())
        enable_tokens = {"enable", "enabled", "on", "true", "yes"}
        disable_tokens = {"disable", "disabled", "off", "false", "no"}
        if normalized in enable_tokens:
            new_value = True
        elif normalized in disable_tokens:
            new_value = False
        else:
            await ctx.send("Use `enable` or `disable`.")
            return

        if campaign.created_by_actor_id != str(ctx.author.id) and not await self._is_image_admin(ctx):
            await ctx.send(
                "Only the campaign creator or an Image Admin can change this setting."
            )
            return

        self.config.set_zork_backend_thinking(ctx.channel.id, new_value)
        with app.app_context():
            campaign = ZorkEmulator.query_campaign(campaign.id)
            if campaign is not None:
                self._sync_campaign_backend_state(
                    campaign,
                    channel_id=ctx.channel.id,
                    thinking_enabled=new_value,
                )
        state_text = "enabled" if new_value else "disabled"
        await ctx.send(
            f"LLM thinking set to `{state_text}` for `{campaign.name}` in this channel/thread. {support_note}"
        )

    @zork.command(name="style")
    async def zork_style(self, ctx, *, option: str = None):
        ctx = self._wrap_send(ctx)
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return

        with app.app_context():
            dm_scope = ctx.guild is None
            if dm_scope:
                binding = self.config.get_zork_private_dm(ctx.author.id)
                if not binding.get("enabled") or not binding.get("campaign_id"):
                    await ctx.send(
                        "No private DM campaign is bound. Enable it from a campaign channel first."
                    )
                    return
                campaign = ZorkEmulator.query_campaign(binding.get("campaign_id"))
                if campaign is None:
                    self.config.clear_zork_private_dm(ctx.author.id)
                    await ctx.send(
                        "Your private DM binding is stale. Re-enable it from the campaign channel."
                    )
                    return
            else:
                channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
                if channel.campaign_id is None:
                    await ctx.send("No active campaign in this channel.")
                    return
                campaign = ZorkEmulator.query_campaign_for_channel(channel)
                if campaign is None:
                    await ctx.send("No active campaign in this channel.")
                    return

        current_style = self.config.get_zork_style(
            ctx.channel.id,
            default_value=AppConfig.DEFAULT_ZORK_STYLE,
        )
        if option is None:
            scope_text = "this DM only" if ctx.guild is None else "this channel/thread"
            await ctx.send(
                f"Current Zork style for {scope_text}: `{current_style}`.\n"
                f"Use `{self._prefix()}zork style default` to clear the override, or "
                f"`{self._prefix()}zork style <prompt>` to set a custom style direction."
            )
            return

        style_text = AppConfig.normalize_zork_style(option, default=None, max_chars=120)
        lowered = str(option or "").strip().lower()
        if lowered in {"default", "clear", "reset"}:
            style_text = None
        elif not style_text:
            await ctx.send(
                f"Style prompt must be 1-120 characters, or use `{self._prefix()}zork style default`."
            )
            return

        if (
            ctx.guild is not None
            # TODO: campaign.created_by_actor_id is a string actor_id, not a Discord int user ID.
            # This comparison needs a bridge lookup to map actor_id -> Discord user ID.
            and campaign.created_by_actor_id != str(ctx.author.id)
            and not await self._is_image_admin(ctx)
        ):
            await ctx.send(
                "Only the campaign creator or an Image Admin can change this setting."
            )
            return

        if style_text is None:
            self.config.clear_zork_style(ctx.channel.id)
            if ctx.guild is not None:
                with app.app_context():
                    refreshed_campaign = ZorkEmulator.query_campaign(campaign.id)
                    if refreshed_campaign is not None:
                        self._sync_campaign_backend_state(
                            refreshed_campaign,
                            channel_id=ctx.channel.id,
                        )
            if ctx.guild is None:
                await ctx.send(
                    f"Zork style for `{campaign.name}` in this DM reset to "
                    f"`{AppConfig.DEFAULT_ZORK_STYLE}`. The shared campaign thread style was not changed."
                )
            else:
                await ctx.send(
                    f"Zork style for `{campaign.name}` in this channel/thread reset to "
                    f"`{AppConfig.DEFAULT_ZORK_STYLE}`."
                )
            return

        self.config.set_zork_style(ctx.channel.id, style_text)
        if ctx.guild is not None:
            with app.app_context():
                refreshed_campaign = ZorkEmulator.query_campaign(campaign.id)
                if refreshed_campaign is not None:
                    self._sync_campaign_backend_state(
                        refreshed_campaign,
                        channel_id=ctx.channel.id,
                        style_direction=style_text,
                    )
        if ctx.guild is None:
            await ctx.send(
                f"Zork style for `{campaign.name}` in this DM set to `{style_text}`. "
                "The shared campaign thread style was not changed."
            )
        else:
            await ctx.send(
                f"Zork style for `{campaign.name}` in this channel/thread set to `{style_text}`."
            )

    @zork.command(name="thread")
    async def zork_thread(self, ctx, *, name: str = None):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        parsed_name, use_imdb, summary_instructions, create_empty = self._parse_thread_options(name)

        if isinstance(ctx.channel, discord.Thread):
            setup_message = None
            requested_name = bool(parsed_name)
            has_txt_attachment = (not create_empty) and any(
                str(getattr(att, "filename", "")).lower().endswith(".txt")
                for att in ctx.message.attachments
            )
            with app.app_context():
                campaign_name = parsed_name or f"thread-{ctx.channel.id}"
                channel = ZorkEmulator.get_or_create_channel(
                    ctx.guild.id, ctx.channel.id
                )
                if requested_name:
                    campaign = ZorkEmulator.create_campaign(
                        ctx.guild.id, campaign_name, ctx.author.id
                    )
                    channel = ZorkEmulator.bind_channel_campaign(
                        channel,
                        campaign.id,
                        enabled=True,
                    )
                else:
                    channel, campaign = ZorkEmulator.enable_channel(
                        ctx.guild.id, ctx.channel.id, ctx.author.id
                    )
                campaign_state = ZorkEmulator.get_campaign_state(campaign)
                att_summary = None
                if has_txt_attachment:
                    att_summary, _, literary_profiles = await self._prepare_thread_source_material(
                        ctx,
                        campaign,
                        channel=ctx.channel,
                        summary_instructions=summary_instructions,
                    )
                    if literary_profiles:
                        styles = campaign_state.get(ZorkEmulator.LITERARY_STYLES_STATE_KEY)
                        if not isinstance(styles, dict):
                            styles = {}
                        styles.update(literary_profiles)
                        campaign_state[ZorkEmulator.LITERARY_STYLES_STATE_KEY] = styles
                        campaign.state_json = ZorkEmulator._dump_json(campaign_state)
                        campaign.updated_at = ZorkEmulator.utcnow()
                        ZorkEmulator.commit_model(campaign)
                    if not requested_name and (
                        campaign_state.get("setup_phase")
                        or campaign_state.get("default_persona")
                    ):
                        campaign.state_json = "{}"
                        campaign.summary = ""
                        campaign.last_narration = None
                        campaign.updated_at = ZorkEmulator.utcnow()
                        ZorkEmulator.commit_model(campaign)
                        campaign_state = ZorkEmulator.get_campaign_state(campaign)

                if not create_empty and ((has_txt_attachment and requested_name) or (
                    not campaign_state.get("setup_phase")
                    and not campaign_state.get("default_persona")
                )):
                    setup_message = await ZorkEmulator.start_campaign_setup(
                        campaign,
                        campaign_name,
                        attachment_summary=att_summary,
                        use_imdb=use_imdb,
                        attachment_summary_instructions=summary_instructions,
                    )
                resolved_campaign_name = campaign.name
            if setup_message:
                await ctx.send(
                    f"Thread mode enabled. Campaign: `{resolved_campaign_name}`.\n\n{setup_message}"
                )
            elif create_empty:
                await ctx.send(
                    f"Thread mode enabled here. Active campaign: `{resolved_campaign_name}`.\n"
                    f"Empty thread created. Run `{self._prefix()}zork thread` here when you want to start setup."
                )
            else:
                await ctx.send(
                    f"Thread mode enabled here. Active campaign: `{resolved_campaign_name}`. "
                    f"This thread is tracked independently."
                )
            return

        thread_name = (parsed_name or f"zork-{ctx.author.display_name}").strip()
        if not thread_name:
            thread_name = f"zork-{ctx.author.id}"
        thread_name = thread_name[:90]
        try:
            thread = await ctx.channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=1440,
            )
        except Exception as e:
            await ctx.send(f"Could not create thread: {e}")
            return

        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(
                ctx.guild.id, thread.id
            )
            campaign_name = (parsed_name or thread.name or f"thread-{thread.id}").strip()
            if not campaign_name:
                campaign_name = f"thread-{thread.id}"
            campaign = ZorkEmulator.create_campaign(
                ctx.guild.id,
                campaign_name,
                ctx.author.id,
            )
            channel = ZorkEmulator.bind_channel_campaign(
                channel,
                campaign.id,
                enabled=True,
            )
            att_summary = None
            if (not create_empty) and any(
                str(getattr(att, "filename", "")).lower().endswith(".txt")
                for att in ctx.message.attachments
            ):
                att_summary, _, literary_profiles = await self._prepare_thread_source_material(
                    ctx,
                    campaign,
                    channel=thread,
                    summary_instructions=summary_instructions,
                )
                if literary_profiles:
                    campaign_state = ZorkEmulator.get_campaign_state(campaign)
                    styles = campaign_state.get(ZorkEmulator.LITERARY_STYLES_STATE_KEY)
                    if not isinstance(styles, dict):
                        styles = {}
                    styles.update(literary_profiles)
                    campaign_state[ZorkEmulator.LITERARY_STYLES_STATE_KEY] = styles
                    campaign.state_json = ZorkEmulator._dump_json(campaign_state)
                    campaign.updated_at = ZorkEmulator.utcnow()
                    ZorkEmulator.commit_model(campaign)
            setup_message = None
            if not create_empty:
                setup_message = await ZorkEmulator.start_campaign_setup(
                    campaign,
                    parsed_name or thread_name,
                    attachment_summary=att_summary,
                    use_imdb=use_imdb,
                    attachment_summary_instructions=summary_instructions,
                )
            resolved_campaign_name = campaign.name

        await self._send_message(
            ctx,
            f"Created Zork thread: {thread.mention}",
            campaign_id=campaign.id,
        )
        if create_empty:
            await self._send_message(
                thread,
                f"{ctx.author.mention} Campaign: `{resolved_campaign_name}`.\n"
                f"Empty thread created. Run `{self._prefix()}zork thread` here when you want to start setup.",
                campaign_id=campaign.id,
            )
        else:
            await self._send_message(
                thread,
                f"{ctx.author.mention} Campaign: `{resolved_campaign_name}`.\n\n{setup_message}"
                ,
                campaign_id=campaign.id,
            )

    @zork.command(name="share")
    async def zork_share(self, ctx, thread_id: str = None):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return

        current_id = getattr(ctx.channel, "id", None)
        if thread_id is None:
            with app.app_context():
                channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
                campaign = (
                    ZorkEmulator.query_campaign_for_channel(channel)
                    if channel.campaign_id
                    else None
                )
                campaign_text = (
                    f"Active campaign here: `{campaign.name}` (id `{campaign.id}`)."
                    if campaign is not None
                    else "No active campaign is bound here yet."
                )
            await ctx.send(
                f"This thread/channel id is `{current_id}`.\n"
                f"{campaign_text}\n"
                f"Run `{self._prefix()}zork share {current_id}` in another thread/channel to link it here."
            )
            return

        try:
            source_thread_id = int(str(thread_id).strip())
        except (TypeError, ValueError):
            await ctx.send("Provide a numeric thread/channel id.")
            return

        with app.app_context():
            target_channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            source_channel = ZorkEmulator.query_channel_by_channel_id(source_thread_id)
            if source_channel is None or source_channel.campaign_id is None:
                await ctx.send("That thread/channel is not linked to an active Zork campaign.")
                return
            source_campaign = ZorkEmulator.query_campaign_for_channel(source_channel)
            if source_campaign is None:
                await ctx.send("That thread/channel points to a missing Zork campaign.")
                return
            if (
                target_channel.campaign_id == source_campaign.id
                and bool(target_channel.enabled)
            ):
                await ctx.send(
                    f"This thread/channel is already linked to `{source_campaign.name}`."
                )
                return
            target_channel = ZorkEmulator.bind_channel_campaign(
                target_channel,
                source_campaign.id,
                enabled=True,
            )
            source_guild_text = (
                f" from guild `{source_campaign.namespace}`"
                if str(source_campaign.namespace) != str(ctx.guild.id)
                else ""
            )
            await ctx.send(
                f"Linked this thread/channel to shared campaign `{source_campaign.name}`"
                f"{source_guild_text} via source id `{source_thread_id}`."
            )

    @zork.command(name="source-material")
    async def zork_source_material(self, ctx, *, label: str = None):
        ctx = self._wrap_send(ctx)
        await self._handle_source_material_command(ctx, label=label)

    @zork.command(name="campaign-rules")
    async def zork_campaign_rules(self, ctx, *, raw: str = None):
        ctx = self._wrap_send(ctx)
        await self._handle_campaign_rules_command(ctx, raw=raw)

    @zork.command(name="literary-reference")
    async def zork_literary_reference(self, ctx, *, label: str = None):
        ctx = self._wrap_send(ctx)
        await self._handle_literary_reference_command(ctx, label=label)

    @zork.command(name="source-material-export")
    async def zork_source_material_export(self, ctx):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return

        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            docs = SourceMaterialMemory.list_source_material_documents(
                campaign.id,
                limit=200,
            )
            export_rows: list[tuple[str, str, str]] = []
            used_names: set[str] = set()
            for row in docs:
                document_key = str(row.get("document_key") or "").strip()
                document_label = str(row.get("document_label") or "").strip()
                if not document_key:
                    continue
                units = SourceMaterialMemory.get_source_material_document_units(
                    campaign.id,
                    document_key,
                )
                export_text = self._source_material_export_text(document_key, units)
                if not export_text:
                    continue
                export_rows.append(
                    (
                        document_key,
                        self._source_material_export_filename(
                            document_key,
                            document_label,
                            used_names=used_names,
                        ),
                        export_text,
                    )
                )
            export_rows.sort(
                key=lambda row: (0 if row[0].strip().lower() == "message" else 1, row[0])
            )

        if not export_rows:
            await ctx.send("No source-material documents are stored for this campaign.")
            return

        batch_size = 10
        sent = 0
        for start in range(0, len(export_rows), batch_size):
            batch = export_rows[start : start + batch_size]
            files: list[discord.File] = []
            for _, filename, export_text in batch:
                payload = export_text.encode("utf-8")
                files.append(
                    discord.File(
                        fp=io.BytesIO(payload),
                        filename=filename,
                    )
                )
            content = None
            if start == 0:
                content = (
                    f"Source-material export for `{campaign.name}` "
                    f"({len(export_rows)} document(s))."
                )
            await ctx.send(content=content, files=files)
            sent += len(batch)

        if sent <= 0:
            await ctx.send("Source-material export produced no files.")

    @zork.command(name="campaign-export")
    async def zork_campaign_export(self, ctx, *, options: str = None):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        export_type, raw_format = self._parse_campaign_export_options(options)

        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign_id = campaign.id
            campaign_name = campaign.name

        reaction_added = await ZorkEmulator._add_processing_reaction(ctx)
        status_msg = None
        try:
            try:
                status_msg = await ctx.send(
                    f"Campaign export: starting `{export_type}` export for `{campaign_name}`..."
                )
            except Exception:
                status_msg = None
            with app.app_context():
                campaign = ZorkEmulator.query_campaign(campaign_id)
                if campaign is None:
                    await ctx.send("No active campaign in this channel.")
                    return
                if export_type == "raw":
                    export_files = await ZorkEmulator._generate_campaign_raw_export_artifacts(
                        campaign,
                        raw_format=raw_format,
                        status_message=status_msg,
                    )
                else:
                    export_files = await ZorkEmulator._generate_campaign_export_artifacts(
                        campaign,
                        ctx.message,
                        channel=ctx.channel,
                        status_message=status_msg,
                    )
                await ZorkEmulator._edit_progress_message(
                    status_msg,
                    "Campaign export: packaging stored source-material documents...",
                )
                docs = SourceMaterialMemory.list_source_material_documents(
                    campaign.id,
                    limit=200,
                )
                source_export_files: dict[str, str] = {}
                used_names = set(export_files.keys())
                for row in docs:
                    document_key = str(row.get("document_key") or "").strip()
                    document_label = str(row.get("document_label") or "").strip()
                    if not document_key:
                        continue
                    units = SourceMaterialMemory.get_source_material_document_units(
                        campaign.id,
                        document_key,
                    )
                    export_text = self._source_material_export_text(document_key, units)
                    if not export_text:
                        continue
                    filename = self._source_material_export_filename(
                        document_key,
                        document_label,
                        used_names=used_names,
                    )
                    source_export_files[filename] = export_text
                export_files.update(source_export_files)
        except Exception:
            await ZorkEmulator._delete_progress_message(status_msg)
            raise
        finally:
            if reaction_added:
                await ZorkEmulator._remove_processing_reaction(ctx)

        if not export_files:
            await ZorkEmulator._delete_progress_message(status_msg)
            await ctx.send("Campaign export produced no files.")
            return

        files: list[discord.File] = []
        for filename, export_text in export_files.items():
            payload = str(export_text or "").encode("utf-8")
            files.append(discord.File(fp=io.BytesIO(payload), filename=filename))
        await ctx.send(
            content=f"Campaign export for `{campaign_name}` ({len(files)} file(s)).",
            files=files,
        )
        await ZorkEmulator._delete_progress_message(status_msg)

    @zork.command(name="avatar")
    async def zork_avatar(self, ctx, *, avatar_input: str = None):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            player = ZorkEmulator.get_or_create_player(
                campaign.id, ctx.author.id, campaign=campaign
            )
            player_state = ZorkEmulator.get_player_state(player)

            if avatar_input is None:
                current_avatar = player_state.get("avatar_url")
                pending_avatar = player_state.get("pending_avatar_url")
                pending_prompt = player_state.get("pending_avatar_prompt")
                lines = []
                lines.append(
                    f"Current avatar: {current_avatar if current_avatar else 'none'}"
                )
                lines.append(
                    f"Pending avatar: {pending_avatar if pending_avatar else 'none'}"
                )
                if pending_prompt:
                    lines.append(f"Pending prompt: `{pending_prompt}`")
                lines.append(
                    f"Use `{self._prefix()}zork avatar <prompt>` to generate a new candidate on white background."
                )
                lines.append(
                    f"Use `{self._prefix()}zork avatar accept` or `{self._prefix()}zork avatar decline`."
                )
                await self._send_large_message(ctx, "\n".join(lines))
                return

            clean_input = avatar_input.strip()
            command = clean_input.lower()
            if command == "accept":
                ok, message = ZorkEmulator.accept_pending_avatar(
                    campaign.id, ctx.author.id
                )
                await ctx.send(message)
                return
            if command == "decline":
                ok, message = ZorkEmulator.decline_pending_avatar(
                    campaign.id, ctx.author.id
                )
                await ctx.send(message)
                return

            # Direct URL — set avatar immediately, skip generation.
            if clean_input.lower().startswith(("http://", "https://")):
                player_state["avatar_url"] = clean_input
                player_state.pop("pending_avatar_url", None)
                player_state.pop("pending_avatar_prompt", None)
                player_state.pop("pending_avatar_generated_at", None)
                player.state_json = ZorkEmulator._dump_json(player_state)
                player.updated_at = ZorkEmulator.utcnow()
                ZorkEmulator.commit_model(player)
                await ctx.send(f"Avatar set: {clean_input}")
                return

            ok, message = await ZorkEmulator.enqueue_avatar_generation(
                ctx,
                campaign=campaign,
                player=player,
                requested_prompt=clean_input,
            )
            await ctx.send(message)

    @zork.command(name="attributes")
    async def zork_attributes(self, ctx, *args):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if not channel.enabled:
                await ctx.send(
                    f"Adventure mode is disabled. Run `{self._prefix()}zork` first."
                )
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                campaign = ZorkEmulator.get_or_create_campaign(
                    ctx.guild.id, "main", ctx.author.id
                )
            player = ZorkEmulator.get_or_create_player(
                campaign.id, ctx.author.id, campaign=campaign
            )

            if not args:
                attrs = ZorkEmulator.get_player_attributes(player)
                total_points = ZorkEmulator.total_points_for_level(player.level)
                spent = ZorkEmulator.points_spent(attrs)
                remaining = total_points - spent
                if not attrs:
                    await ctx.send(
                        f"No attributes set. Points available: {remaining}/{total_points}."
                    )
                    return
                lines = [f"{k}: {v}" for k, v in sorted(attrs.items())]
                await self._send_large_message(
                    ctx,
                    "Attributes:\n"
                    + "\n".join(lines)
                    + f"\nPoints available: {remaining}/{total_points}",
                )
                return

            args = list(args)
            if args[0].lower() in ("set", "add", "update") and len(args) >= 3:
                name = args[1].lower()
                value_str = args[2]
            elif len(args) >= 2:
                name = args[0].lower()
                value_str = args[1]
            else:
                await ctx.send(
                    f"Usage: `{self._prefix()}zork attributes <name> <value>`"
                )
                return

            try:
                value = int(value_str)
            except ValueError:
                await ctx.send("Attribute value must be an integer.")
                return

            ok, message = ZorkEmulator.set_attribute(player, name, value)
            if ok:
                await ctx.send(f"{message} `{name}` is now {value}.")
            else:
                await ctx.send(message)

    @zork.command(name="stats")
    async def zork_stats(self, ctx):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                campaign = ZorkEmulator.get_or_create_campaign(
                    ctx.guild.id, "main", ctx.author.id
                )
            player = ZorkEmulator.get_or_create_player(
                campaign.id, ctx.author.id, campaign=campaign
            )
            attrs = ZorkEmulator.get_player_attributes(player)
            player_stats = ZorkEmulator.get_player_statistics(player)
            total_points = ZorkEmulator.total_points_for_level(player.level)
            spent = ZorkEmulator.points_spent(attrs)
            remaining = total_points - spent
            xp_needed = ZorkEmulator.xp_needed_for_level(player.level)
            attrs_text = (
                ", ".join([f"{k}={v}" for k, v in sorted(attrs.items())])
                if attrs
                else "none"
            )
            message = (
                f"Campaign: `{campaign.name}`\n"
                f"Level: {player.level} | XP: {player.xp}/{xp_needed}\n"
                f"Attributes: {attrs_text}\n"
                f"Points available: {remaining}/{total_points}\n"
                f"Messages sent: {player_stats.get('messages_sent', 0)}\n"
                f"Timers averted: {player_stats.get('timers_averted', 0)}\n"
                f"Timers missed: {player_stats.get('timers_missed', 0)}\n"
                f"Attention time: {player_stats.get('attention_hours', 0.0):.2f} hours"
            )
            await self._send_large_message(ctx, message)

    @zork.command(name="level")
    async def zork_level(self, ctx):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                campaign = ZorkEmulator.get_or_create_campaign(
                    ctx.guild.id, "main", ctx.author.id
                )
            player = ZorkEmulator.get_or_create_player(
                campaign.id, ctx.author.id, campaign=campaign
            )
            ok, message = ZorkEmulator.level_up(player)
            await ctx.send(message)

    @zork.command(name="where")
    async def zork_where(self, ctx):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            players = ZorkEmulator.query_players_for_campaign(campaign.id)
            if not players:
                await ctx.send("No players have joined this campaign yet.")
                return
            now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            cutoff = now - datetime.timedelta(hours=1)
            lines = []
            for player in players:
                player_state = ZorkEmulator.get_player_state(player)
                room = (
                    player_state.get("room_summary")
                    or player_state.get("room_title")
                    or player_state.get("location")
                    or "unknown"
                )
                party_status = player_state.get("party_status")
                status = (
                    "active"
                    if player.last_active_at and player.last_active_at >= cutoff
                    else "inactive"
                )
                extra = f" | party: {party_status}" if party_status else ""
                # TODO: player.actor_id is a string actor_id, not a Discord int user ID.
                # Discord mention needs the real Discord user ID; bridge lookup may be needed.
                lines.append(f"- <@{player.actor_id}>: {room} ({status}{extra})")
            await self._send_large_message(ctx, "Locations:\n" + "\n".join(lines))

    @zork.command(name="hint")
    async def zork_hint(self, ctx):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            player = ZorkEmulator.get_or_create_player(
                campaign.id, ctx.author.id, campaign=campaign
            )
            player_state = ZorkEmulator.get_player_state(player)
            campaign_state = ZorkEmulator.get_campaign_state(campaign)
            viewer_player_slug = ZorkEmulator._player_slug_key(
                player_state.get("character_name")
            )
            viewer_location_key = ZorkEmulator._room_key_from_player_state(
                player_state
            ).lower()
            active_scene_npc_slugs = ZorkEmulator._active_scene_npc_slugs(
                campaign, player_state
            )
            hints = ZorkEmulator._plot_hints_for_viewer(
                campaign,
                campaign_state,
                viewer_user_id=ctx.author.id,
                viewer_player_slug=viewer_player_slug,
                viewer_location_key=viewer_location_key,
                active_scene_npc_slugs=active_scene_npc_slugs,
                limit=5,
            )
            visible_threads = ZorkEmulator._plot_threads_for_prompt(
                campaign_state,
                campaign=campaign,
                viewer_user_id=ctx.author.id,
                viewer_player_slug=viewer_player_slug,
                viewer_location_key=viewer_location_key,
                active_scene_npc_slugs=active_scene_npc_slugs,
                limit=10,
            )
            active_visible = [
                row for row in visible_threads if str(row.get("status") or "") == "active"
            ]
            if not hints and not active_visible:
                await ctx.send("No active hints right now.")
                return

            lines = [f"Hints for `{campaign.name}`:"]
            if hints:
                for row in hints:
                    thread = str(row.get("thread") or "").strip() or "untitled-thread"
                    hint_text = str(row.get("hint") or "").strip()
                    if hint_text:
                        lines.append(f"- `{thread}`: {hint_text}")
                    else:
                        lines.append(f"- `{thread}`")
            elif active_visible:
                lines.append("- No imminent hints, but these active threads are currently visible:")
                for row in active_visible[:5]:
                    thread = str(row.get("thread") or "").strip() or "untitled-thread"
                    setup = " ".join(str(row.get("setup") or "").split()).strip()[:180]
                    if setup:
                        lines.append(f"- `{thread}`: {setup}")
                    else:
                        lines.append(f"- `{thread}`")

            if viewer_location_key:
                lines.append(f"Location: `{viewer_location_key}`")
            if active_scene_npc_slugs:
                lines.append(
                    "Scene NPCs: " + ", ".join(f"`{slug}`" for slug in sorted(active_scene_npc_slugs))
                )
            await self._send_large_message(ctx, "\n".join(lines))

    @zork.command(name="speed")
    async def zork_speed(self, ctx, *, value: str = None):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            if value is None:
                current = ZorkEmulator.get_speed_multiplier(campaign)
                await ctx.send(
                    f"Current in-world clock speed multiplier: `{current}x` for campaign `{campaign.name}`.\n"
                    f"Use `{self._prefix()}zork speed <value>` to change (0.1–10.0)."
                )
                return
            # TODO: campaign.created_by_actor_id is a string actor_id, not a Discord int user ID.
            # This comparison needs a bridge lookup to map actor_id -> Discord user ID.
            if campaign.created_by_actor_id != str(ctx.author.id) and not await self._is_image_admin(ctx):
                await ctx.send(
                    "Only the campaign creator or an Image Admin can change the in-world clock speed multiplier."
                )
                return
            try:
                multiplier = float(value.strip())
            except ValueError:
                await ctx.send("In-world clock speed multiplier must be a number (0.1–10.0).")
                return
            if multiplier < 0.1 or multiplier > 10.0:
                await ctx.send("In-world clock speed multiplier must be between 0.1 and 10.0.")
                return
            ZorkEmulator.set_speed_multiplier(campaign, multiplier)
            await ctx.send(f"In-world clock speed multiplier set to `{multiplier}x` for campaign `{campaign.name}`.")

    @zork.command(name="timed-events-speed")
    async def zork_timed_events_speed(self, ctx, *, value: str = None):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            if value is None:
                current = ZorkEmulator.get_timed_events_speed_multiplier(campaign)
                await ctx.send(
                    f"Current timed-events speed multiplier: `{current}x` for campaign `{campaign.name}`.\n"
                    f"Use `{self._prefix()}zork timed-events-speed <value>` to change (0.1–10.0)."
                )
                return
            if campaign.created_by_actor_id != str(ctx.author.id) and not await self._is_image_admin(ctx):
                await ctx.send(
                    "Only the campaign creator or an Image Admin can change the timed-events speed multiplier."
                )
                return
            try:
                multiplier = float(value.strip())
            except ValueError:
                await ctx.send("Timed-events speed multiplier must be a number (0.1–10.0).")
                return
            if multiplier < 0.1 or multiplier > 10.0:
                await ctx.send("Timed-events speed multiplier must be between 0.1 and 10.0.")
                return
            ZorkEmulator.set_timed_events_speed_multiplier(campaign, multiplier)
            await ctx.send(
                f"Timed-events speed multiplier set to `{multiplier}x` for campaign `{campaign.name}`."
            )

    @zork.command(name="clock")
    async def zork_clock(self, ctx, *, value: str = None):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            current_clock = ZorkEmulator.get_campaign_clock(campaign)
            current_label = str(current_clock.get("date_label") or "").strip() or ZorkEmulator.get_world_time_text(campaign.id)
            current_hour = int(current_clock.get("hour", 0) or 0)
            current_minute = int(current_clock.get("minute", 0) or 0)
            if value is None:
                await ctx.send(
                    f"Current campaign clock: `{current_label} ({current_hour:02d}:{current_minute:02d})`.\n"
                    f"Use `{self._prefix()}zork clock 18:30`, "
                    f"`{self._prefix()}zork clock 138 18:30`, or "
                    f"`{self._prefix()}zork clock friday 138 18:30` to change it."
                )
                return
            is_owner = await self.bot.is_owner(ctx.author)
            if campaign.created_by_actor_id != str(ctx.author.id) and not is_owner:
                await ctx.send(
                    "Only the campaign creator or the bot owner can change the campaign clock."
                )
                return
            parsed, error = self._parse_clock_value(
                value,
                current_day=int(current_clock.get("day", 1) or 1),
            )
            if parsed is None:
                await ctx.send(error or "Invalid clock value.")
                return
            updated = ZorkEmulator.set_campaign_clock(
                campaign,
                day=int(parsed.get("day", 1) or 1),
                hour=int(parsed.get("hour", 0) or 0),
                minute=int(parsed.get("minute", 0) or 0),
                day_of_week=parsed.get("day_of_week"),
            )
            if not isinstance(updated, dict) or not updated:
                await ctx.send("Failed to update the campaign clock.")
                return
            updated_label = str(updated.get("date_label") or "").strip() or ZorkEmulator.get_world_time_text(campaign.id)
            updated_hour = int(updated.get("hour", 0) or 0)
            updated_minute = int(updated.get("minute", 0) or 0)
            note = ""
            state = ZorkEmulator.get_campaign_state(campaign)
            if str(state.get("time_model") or "").strip().lower() == "individual_clocks":
                note = " Global campaign clock updated; personal player clocks were not changed."
            await ctx.send(
                f"Campaign clock set to `{updated_label} ({updated_hour:02d}:{updated_minute:02d})` for campaign `{campaign.name}`.{note}"
            )

    @zork.command(name="clock-type")
    async def zork_clock_type(self, ctx, *, value: str = None):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            current = ZorkEmulator.get_campaign_clock_type(campaign)
            if value is None:
                await ctx.send(
                    f"Current clock type: `{current}` for campaign `{campaign.name}`.\n"
                    "Available: `loose-calendar`, `consequential-calendar`, `individual-calendars`."
                )
                return
            is_owner = await self.bot.is_owner(ctx.author)
            if campaign.created_by_actor_id != str(ctx.author.id) and not is_owner:
                await ctx.send(
                    "Only the campaign creator or the bot owner can change the clock type."
                )
                return
            updated, error = ZorkEmulator.set_campaign_clock_type(campaign, value)
            if error:
                await ctx.send(error)
                return
            await ctx.send(
                f"Clock type set to `{updated}` for campaign `{campaign.name}`."
            )

    @zork.command(name="difficulty")
    async def zork_difficulty(self, ctx, *, value: str = None):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return

            levels = ", ".join(f"`{v}`" for v in ZorkEmulator.DIFFICULTY_LEVELS)
            if value is None:
                current = ZorkEmulator.get_difficulty(campaign)
                await ctx.send(
                    f"Current difficulty: `{current}` for campaign `{campaign.name}`.\n"
                    f"Available: {levels}\n"
                    f"Use `{self._prefix()}zork difficulty <level>` to change."
                )
                return

            # TODO: campaign.created_by_actor_id is a string actor_id, not a Discord int user ID.
            # This comparison needs a bridge lookup to map actor_id -> Discord user ID.
            if campaign.created_by_actor_id != str(ctx.author.id) and not await self._is_image_admin(ctx):
                await ctx.send(
                    "Only the campaign creator or an Image Admin can change the difficulty."
                )
                return

            normalized = ZorkEmulator.normalize_difficulty(value)
            raw = " ".join(str(value or "").strip().lower().split())
            if normalized == "normal" and raw not in {"normal", "default", "std", "normal mode"}:
                await ctx.send(
                    f"Unknown difficulty `{value}`. Available: {levels}"
                )
                return

            ZorkEmulator.set_difficulty(campaign, normalized)
            if normalized == "normal":
                await ctx.send(
                    f"Difficulty set to `{normalized}` for campaign `{campaign.name}`. "
                    "No extra difficulty instruction is applied."
                )
            else:
                await ctx.send(
                    f"Difficulty set to `{normalized}` for campaign `{campaign.name}`."
                )

    @zork.command(name="roster")
    async def zork_roster(self, ctx, *, args: str = None):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            characters = ZorkEmulator.get_campaign_characters(campaign)

            # Check for "roster <name> portrait" subcommand.
            if args and args.strip().lower().endswith(" portrait"):
                name_query = args.strip()[: -len(" portrait")].strip().lower()
                if not name_query:
                    await ctx.send("Usage: `!zork roster <name> portrait`")
                    return
                found_slug = None
                for slug, char in characters.items():
                    char_name = str(char.get("name") or "").strip().lower()
                    if slug.lower() == name_query or char_name == name_query:
                        found_slug = slug
                        break
                if found_slug is None:
                    # Fuzzy: check if query is contained in name or slug.
                    for slug, char in characters.items():
                        char_name = str(char.get("name") or "").strip().lower()
                        if name_query in slug.lower() or name_query in char_name:
                            found_slug = slug
                            break
                if found_slug is None:
                    await ctx.send(f"Character `{name_query}` not found in roster.")
                    return
                char = characters[found_slug]
                appearance = str(char.get("appearance") or "").strip()
                if not appearance:
                    await ctx.send(
                        f"Character `{char.get('name', found_slug)}` has no appearance description for portrait generation."
                    )
                    return
                ok = await ZorkEmulator._enqueue_character_portrait(
                    ctx, campaign, found_slug, char.get("name", found_slug), appearance,
                )
                if ok:
                    await ctx.send(
                        f"Portrait generation queued for `{char.get('name', found_slug)}`."
                    )
                else:
                    await ctx.send("Failed to queue portrait generation (no GPU workers available).")
                return

            # Display roster.
            await self._send_large_message(
                ctx, ZorkEmulator.format_roster(characters)
            )

    @zork.command(name="map")
    async def zork_map(self, ctx):
        ctx = self._wrap_send(ctx)
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        if ctx.guild is None:
            with app.app_context():
                binding = self._get_private_dm_binding(ctx.author.id)
                if binding is None:
                    await ctx.send(
                        "No private DM campaign is bound. Enable it from a campaign channel first."
                    )
                    return
                campaign = ZorkEmulator.query_campaign(binding.get("campaign_id"))
                if campaign is None:
                    self.config.clear_zork_private_dm(ctx.author.id)
                    await ctx.send(
                        "Your private DM binding is stale. Re-enable it from the campaign channel."
                    )
                    return
            ascii_map = await ZorkEmulator.generate_map(
                campaign.id,
                actor_id=ctx.author.id,
                command_prefix=self._prefix(),
            )
        else:
            ascii_map = await ZorkEmulator.generate_map(ctx, command_prefix=self._prefix())
        if ascii_map.startswith("```") and ascii_map.endswith("```"):
            await self._send_large_message(ctx, ascii_map)
            return
        await self._send_large_message(ctx, f"```\n{ascii_map}\n```")

    @zork.command(name="reset")
    async def zork_reset(self, ctx):
        ctx = self._wrap_send(ctx)
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        if not await self._is_image_admin(ctx):
            await ctx.send("You are not an Image Admin.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkEmulator.query_campaign_for_channel(channel)
            if campaign is None:
                ZorkEmulator.bind_channel_campaign(channel, None)
                await ctx.send("Channel state cleared.")
                return

            shared_refs = ZorkEmulator.count_channels_for_campaign(
                campaign.id, exclude_channel_id=ctx.channel.id, guild_id=ctx.guild.id
            )

            if shared_refs > 0:
                # Avoid wiping state for other channels still bound to this campaign.
                reset_name = f"{campaign.name}-reset-{ctx.channel.id}-{int(datetime.datetime.now(datetime.timezone.utc).timestamp())}"
                new_campaign = ZorkEmulator.get_or_create_campaign(
                    ctx.guild.id, reset_name, ctx.author.id
                )
                ZorkEmulator.delete_campaign_data(new_campaign.id)
                new_campaign.summary = ""
                new_campaign.state_json = "{}"
                new_campaign.last_narration = None
                new_campaign.updated_at = ZorkEmulator.utcnow()
                channel = ZorkEmulator.bind_channel_campaign(
                    channel,
                    new_campaign.id,
                    enabled=True,
                )
                ZorkEmulator.commit_model(new_campaign)
                ZorkMemory.delete_campaign_embeddings(
                    ZorkEmulator.memory_campaign_id(new_campaign)
                )
                await ctx.send(
                    f"Channel reset to fresh campaign `{new_campaign.name}` (shared campaign left untouched)."
                )
                return

            _mcid = ZorkEmulator.memory_campaign_id(campaign)
            ZorkEmulator.delete_campaign_data(campaign.id)
            campaign.summary = ""
            campaign.state_json = "{}"
            campaign.last_narration = None
            campaign.updated_at = ZorkEmulator.utcnow()
            channel.enabled = True
            channel.updated_at = ZorkEmulator.utcnow()
            ZorkEmulator.commit_models(campaign, channel)
            ZorkMemory.delete_campaign_embeddings(_mcid)
            ZorkEmulator.cancel_pending_timer(campaign.id)
            ZorkEmulator.cancel_pending_sms_deliveries(campaign.id)
            await ctx.send(f"Reset campaign `{campaign.name}` for this channel.")

    @zork.command(name="restart")
    async def zork_restart(self, ctx):
        ctx = self._wrap_send(ctx)
        if not await self.bot.is_owner(ctx.author):
            await ctx.send("This command is restricted to the bot owner.")
            return
        await ctx.send(
            "Restart initiated. Rejecting new requests and draining in-flight turns..."
        )
        ZorkEmulator.request_shutdown()
        drained = await ZorkEmulator.wait_for_drain(
            timeout=self.RESTART_DRAIN_TIMEOUT_SECONDS
        )
        if drained:
            await ctx.send("All turns drained. Shutting down now.")
        else:
            remaining = len(ZorkEmulator._inflight_turns)
            cleared = ZorkEmulator.clear_all_inflight_claims()
            await ctx.send(
                f"Drain timeout. {remaining} turn(s) still in-flight. "
                f"Cleared {cleared} abandoned inflight claim(s) and forcing shutdown."
            )
        await self.bot.close()
        import sys

        sys.exit(0)
