"""Drop-in bridge replacing the old ZorkEmulator classmethod interface.

Usage:
    from discord_tron_master.adapters.emulator_bridge import EmulatorBridge as ZorkEmulator
"""
from __future__ import annotations

import asyncio
import contextvars
import datetime
import inspect
import json
import logging
import os
import sqlite3
import threading
from typing import Any, Optional, Tuple

from discord_tron_master.adapters.tge_ports import (
    get_tge_completion_overrides,
    reset_tge_completion_overrides,
    set_tge_completion_overrides,
)

logger = logging.getLogger(__name__)

_ZORK_LOG_ROOT = os.path.join(os.getcwd(), "zork-logs")
_ZORK_LOG_PATH: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "zork_log_path",
    default=None,
)
_ZORK_LOG_RETENTION = 100


def _zork_log_component(value: object, label: str = "id") -> str:
    raw = str(value or label).strip()
    return raw if raw else label


def _zork_log_context_dir(
    *,
    guild_id: object = None,
    channel_id: object = None,
    user_id: object = None,
) -> str:
    if guild_id is not None and channel_id is not None:
        return os.path.join(
            _ZORK_LOG_ROOT,
            _zork_log_component(guild_id, "guild"),
            _zork_log_component(channel_id, "thread"),
        )
    if user_id is not None:
        return os.path.join(
            _ZORK_LOG_ROOT,
            _zork_log_component(user_id, "user"),
        )
    if guild_id is not None:
        return os.path.join(
            _ZORK_LOG_ROOT,
            _zork_log_component(guild_id, "guild"),
            "campaign",
        )
    return os.path.join(_ZORK_LOG_ROOT, "global")


def _zork_log_rotate(path: str) -> None:
    """Archive existing latest log before starting a new one."""
    try:
        if not os.path.exists(path):
            return
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        archive = os.path.join(os.path.dirname(path), f"turn-{ts}.log")
        counter = 0
        while os.path.exists(archive):
            counter += 1
            archive = os.path.join(os.path.dirname(path), f"turn-{ts}-{counter}.log")
        os.rename(path, archive)
        # Prune old archives
        dir_path = os.path.dirname(path)
        archives = sorted(
            (f for f in os.listdir(dir_path) if f.startswith("turn-")),
            reverse=True,
        )
        for old in archives[_ZORK_LOG_RETENTION:]:
            try:
                os.remove(os.path.join(dir_path, old))
            except Exception:
                pass
    except Exception:
        pass


def _zork_log_begin(
    *,
    guild_id: object = None,
    channel_id: object = None,
    user_id: object = None,
    is_dm: bool = False,
) -> contextvars.Token:
    """Push a contextual log scope; returns a contextvar token for _zork_log_end."""
    dir_path = _zork_log_context_dir(
        guild_id=None if is_dm else guild_id,
        channel_id=None if is_dm else channel_id,
        user_id=user_id if is_dm else None,
    )
    os.makedirs(dir_path, exist_ok=True)
    if is_dm:
        latest_name = "latest.log"
    else:
        latest_name = f"latest-{_zork_log_component(user_id, 'user')}.log"
    log_path = os.path.join(dir_path, latest_name)
    _zork_log_rotate(log_path)
    return _ZORK_LOG_PATH.set(log_path)


def _zork_log_end(token: contextvars.Token) -> None:
    """Pop the contextual log scope."""
    _ZORK_LOG_PATH.reset(token)


def _zork_log(section: str, body: str = "") -> None:
    """Append a timestamped section to the active log file."""
    try:
        log_path = _ZORK_LOG_PATH.get()
        if log_path is None:
            log_dir = os.path.join(_ZORK_LOG_ROOT, "global")
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, "event.log")
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a") as fh:
            fh.write(f"\n{'=' * 72}\n[{ts}] {section}\n{'=' * 72}\n")
            if body:
                fh.write(body)
                if not body.endswith("\n"):
                    fh.write("\n")
    except Exception:
        pass


class _EmulatorBridgeMeta(type):
    """Delegate missing class attributes and helpers to TGE's emulator."""

    def __getattr__(cls, name):
        try:
            from text_game_engine import ZorkEmulator as TGEZorkEmulator
        except Exception:
            TGEZorkEmulator = None

        if TGEZorkEmulator is not None and hasattr(TGEZorkEmulator, name):
            class_attr = getattr(TGEZorkEmulator, name)
            if not callable(class_attr):
                return class_attr

            def _class_proxy(*args, **kwargs):
                cls._ensure_init()
                target = getattr(cls._emu, name)
                return target(*args, **kwargs)

            _class_proxy.__name__ = name
            return _class_proxy

        cls._ensure_init()
        target = getattr(cls._emu, name, None)
        if target is not None:
            return target
        raise AttributeError(f"EmulatorBridge has no attribute {name!r}")


class EmulatorBridge(metaclass=_EmulatorBridgeMeta):
    """Singleton classmethod facade over TGE's instance-based ZorkEmulator.

    Every public/private method the cog calls is forwarded to the TGE instance.
    Lazy-initialized on first use.
    """

    _emu = None  # TGE ZorkEmulator instance
    _session_factory = None
    _init_lock = threading.Lock()
    _initialized = False
    _inflight_turns: dict[str, object] = {}
    _shutdown_requested = False

    @classmethod
    def _ensure_init(cls):
        if cls._initialized:
            return
        with cls._init_lock:
            if cls._initialized:
                return
            cls._do_init()
            cls._initialized = True

    @classmethod
    def _do_init(cls):
        from discord_tron_master.classes.app_config import AppConfig
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from text_game_engine import GameEngine, ZorkEmulator as TGEZorkEmulator
        from text_game_engine.persistence.sqlalchemy.db import build_session_factory
        from text_game_engine.persistence.sqlalchemy.base import Base

        from discord_tron_master.adapters.tge_ports import (
            TextCompletionAdapter,
            TimerEffectsAdapter,
            NotificationAdapter,
            MediaGenerationAdapter,
            ZorkMemoryAdapter,
            IMDBLookupAdapter,
        )

        config = AppConfig()
        url = (
            f"mysql+mysqlconnector://"
            f"{config.get_mysql_user()}:{config.get_mysql_password()}"
            f"@{config.get_mysql_hostname()}/{config.get_mysql_dbname()}"
        )
        engine = create_engine(url, echo=False, pool_pre_ping=True, pool_recycle=3600)

        # Don't call create_schema -- tables already exist from Alembic migration
        cls._session_factory = build_session_factory(engine)

        def _gpt_factory():
            from discord_tron_master.classes.openai.text import GPT
            gpt = GPT()
            overrides = get_tge_completion_overrides()
            backend_config = config.get_zork_backend_config(default_backend="zai")
            backend = (
                str(overrides.get("backend") or backend_config.get("backend") or "zai").strip()
                or "zai"
            )
            model = str(
                overrides.get("model") or backend_config.get("model") or ""
            ).strip()
            gpt.backend = backend
            if model:
                gpt.engine = model
            return gpt

        completion_port = TextCompletionAdapter(gpt_factory=_gpt_factory)
        timer_effects_port = TimerEffectsAdapter()
        notification_port = NotificationAdapter()
        media_port = MediaGenerationAdapter()
        memory_port = ZorkMemoryAdapter()
        imdb_port = IMDBLookupAdapter()

        from text_game_engine.tool_aware_llm import ToolAwareZorkLLM
        from text_game_engine.persistence.sqlalchemy.uow import SQLAlchemyUnitOfWork

        llm = ToolAwareZorkLLM(
            session_factory=cls._session_factory,
            completion_port=completion_port,
            temperature=0.85,
            max_tokens=16384,
        )

        def _uow_factory():
            return SQLAlchemyUnitOfWork(cls._session_factory)

        game_engine = GameEngine(
            uow_factory=_uow_factory,
            llm=llm,
            lease_ttl_seconds=GameEngine.DEFAULT_LEASE_TTL_SECONDS,
            max_conflict_retries=2,
        )

        # Point TGE's SourceMaterialMemory at DTM's shared SQLite database.
        from text_game_engine.core.source_material_memory import SourceMaterialMemory
        from discord_tron_master.classes import zork_memory as _zm_mod

        SourceMaterialMemory.configure(
            db_path=_zm_mod._DB_PATH,
            campaign_id_translator=ZorkMemoryAdapter._int_campaign_id,
        )
        cls._migrate_legacy_memory_ids()

        cls._emu = TGEZorkEmulator(
            game_engine=game_engine,
            session_factory=cls._session_factory,
            completion_port=completion_port,
            timer_effects_port=timer_effects_port,
            memory_port=memory_port,
            imdb_port=imdb_port,
            media_port=media_port,
            notification_port=notification_port,
        )
        # Critical: bind the emulator to the LLM so complete_turn() can
        # build prompts via ZorkEmulator instead of falling back to DeterministicLLM.
        llm.bind_emulator(cls._emu)
        llm.set_log_callback(_zork_log)
        # DTM shows inventory only on reaction/command, not in every narration.
        cls._emu.append_inventory_to_narration = False
        logger.info("EmulatorBridge: TGE ZorkEmulator initialized")

    @staticmethod
    def _filter_supported_kwargs(fn, kwargs: dict[str, Any]) -> dict[str, Any]:
        if not kwargs:
            return {}
        try:
            sig = inspect.signature(fn)
        except Exception:
            return dict(kwargs)
        if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in sig.parameters.values()):
            return dict(kwargs)
        allowed = set(sig.parameters.keys())
        return {key: value for key, value in kwargs.items() if key in allowed}

    @staticmethod
    def _trim_supported_args(fn, args: tuple[Any, ...]) -> tuple[Any, ...]:
        if not args:
            return ()
        try:
            sig = inspect.signature(fn)
        except Exception:
            return args
        positional_params = [
            param
            for param in sig.parameters.values()
            if param.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        if any(param.kind is inspect.Parameter.VAR_POSITIONAL for param in sig.parameters.values()):
            return args
        return args[: len(positional_params)]

    # -- Persistence helpers ---------------------------------------------------

    @classmethod
    def save_campaign(cls, campaign):
        """Persist changes to a Campaign object."""
        cls._ensure_init()
        with cls._session_factory() as session:
            session.merge(campaign)
            session.commit()

    @classmethod
    def save_player(cls, player):
        """Persist changes to a Player object."""
        cls._ensure_init()
        with cls._session_factory() as session:
            session.merge(player)
            session.commit()

    @classmethod
    def get_active_campaign(cls, session_obj):
        """Given a TGE Session, return the active Campaign object."""
        cls._ensure_init()
        from text_game_engine.persistence.sqlalchemy.models import Campaign
        # The Session's campaign_id is the active campaign.
        # In TGE, session.campaign_id IS the active campaign.
        with cls._session_factory() as db_session:
            return db_session.get(Campaign, session_obj.campaign_id)

    # -- Channel / Session Management ------------------------------------------

    @classmethod
    def get_or_create_channel(cls, guild_id, channel_id):
        cls._ensure_init()
        return cls._emu.get_or_create_channel(guild_id, channel_id)

    @classmethod
    def enable_channel(cls, guild_id, channel_id, actor_id=None):
        cls._ensure_init()
        return cls._emu.enable_channel(guild_id, channel_id, str(actor_id) if actor_id else "0")

    @classmethod
    def bind_channel_campaign(cls, channel_session, campaign_id, *, enabled=None):
        cls._ensure_init()
        if channel_session is None:
            return None
        from text_game_engine.persistence.sqlalchemy.models import Session as GameSession

        channel_id = getattr(channel_session, "id", channel_session)
        with cls._session_factory() as session:
            row = session.get(GameSession, channel_id)
            if row is None:
                return None
            meta = cls._emu._load_session_metadata(row)
            resolved_campaign_id = str(campaign_id) if campaign_id is not None else None
            meta["active_campaign_id"] = resolved_campaign_id
            row.campaign_id = resolved_campaign_id
            if enabled is not None:
                row.enabled = bool(enabled)
            row.updated_at = cls.utcnow()
            cls._emu._store_session_metadata(row, meta)
            session.commit()
            return row

    @classmethod
    def set_channel_label(cls, channel_session, label):
        cls._ensure_init()
        if channel_session is None:
            return None
        from text_game_engine.persistence.sqlalchemy.models import Session as GameSession

        channel_id = getattr(channel_session, "id", channel_session)
        label_text = " ".join(str(label or "").split()).strip()
        if not label_text:
            return None
        with cls._session_factory() as session:
            row = session.get(GameSession, channel_id)
            if row is None:
                return None
            meta = cls._emu._load_session_metadata(row)
            if str(meta.get("label") or "").strip() == label_text:
                return row
            meta["label"] = label_text
            row.updated_at = cls.utcnow()
            cls._emu._store_session_metadata(row, meta)
            session.commit()
            return row

    @classmethod
    def is_channel_enabled(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu.is_channel_enabled(*args, **kwargs)

    # -- Campaign Management ---------------------------------------------------

    @classmethod
    def create_campaign(cls, *args, **kwargs):
        cls._ensure_init()
        if args:
            namespace = args[0]
            name = args[1] if len(args) > 1 else kwargs.get("name")
            created_by_actor_id = (
                args[2] if len(args) > 2 else kwargs.get("created_by_actor_id")
            )
            campaign_id = args[3] if len(args) > 3 else kwargs.get("campaign_id")
        else:
            namespace = kwargs.get("namespace")
            name = kwargs.get("name")
            created_by_actor_id = kwargs.get("created_by_actor_id")
            campaign_id = kwargs.get("campaign_id")
        return cls._emu.get_or_create_campaign(
            namespace,
            name,
            str(created_by_actor_id) if created_by_actor_id is not None else "system",
            campaign_id=campaign_id,
        )

    @classmethod
    def get_or_create_campaign(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu.get_or_create_campaign(*args, **kwargs)

    @classmethod
    def list_campaigns(cls, guild_id):
        cls._ensure_init()
        return cls._emu.list_campaigns(str(guild_id))

    @classmethod
    def set_active_campaign(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu.set_active_campaign(*args, **kwargs)

    @classmethod
    def get_campaign_state(cls, campaign):
        cls._ensure_init()
        return cls._emu.get_campaign_state(campaign)

    @classmethod
    def get_campaign_characters(cls, campaign):
        cls._ensure_init()
        return cls._emu.get_campaign_characters(campaign)

    @classmethod
    def get_campaign_default_persona(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu.get_campaign_default_persona(*args, **kwargs)

    @classmethod
    def can_switch_campaign(cls, campaign):
        cls._ensure_init()
        return cls._emu.can_switch_campaign(campaign)

    # -- Turn Flow -------------------------------------------------------------

    @classmethod
    async def begin_turn(cls, *args, **kwargs):
        cls._ensure_init()
        return await cls._emu.begin_turn(*args, **kwargs)

    @classmethod
    async def begin_turn_for_campaign(cls, message, campaign_id, **kwargs):
        """Begin a turn for a DM-bound campaign.

        TGE's begin_turn(campaign_id, actor_id) expects UUID campaign IDs.
        This method resolves legacy integer campaign IDs before forwarding.
        """
        cls._ensure_init()
        # Resolve legacy campaign IDs from DM bindings
        campaign = cls.query_campaign(campaign_id)
        if campaign is None:
            return None, "Campaign not found."
        actor_id = str(getattr(getattr(message, "author", None), "id", ""))
        return await cls._emu.begin_turn(campaign.id, actor_id, **kwargs)

    @classmethod
    async def play_action(cls, *args, **kwargs):
        cls._ensure_init()
        if cls._shutdown_requested:
            return "Restart in progress. New turns are temporarily disabled."
        # Push contextual log scope from the Discord ctx (first arg).
        ctx = args[0] if args else None
        guild = getattr(ctx, "guild", None)
        channel = getattr(ctx, "channel", None)
        author = getattr(ctx, "author", None)
        log_token = _zork_log_begin(
            guild_id=getattr(guild, "id", None) if guild else None,
            channel_id=getattr(channel, "id", None) if channel else None,
            user_id=getattr(author, "id", None) if author else None,
            is_dm=guild is None,
        )
        campaign_id = kwargs.get("campaign_id")
        completion_token = set_tge_completion_overrides(
            cls._campaign_completion_overrides(campaign_id)
        )
        try:
            return await cls._emu.play_action(*args, **kwargs)
        finally:
            reset_tge_completion_overrides(completion_token)
            _zork_log_end(log_token)

    @classmethod
    async def handle_setup_message(cls, *args, **kwargs):
        cls._ensure_init()
        ctx = args[0] if args else None
        guild = getattr(ctx, "guild", None)
        channel = getattr(ctx, "channel", None)
        author = getattr(ctx, "author", None)
        log_token = _zork_log_begin(
            guild_id=getattr(guild, "id", None) if guild else None,
            channel_id=getattr(channel, "id", None) if channel else None,
            user_id=getattr(author, "id", None) if author else None,
            is_dm=guild is None,
        )
        campaign_like = args[2] if len(args) > 2 else kwargs.get("campaign")
        campaign_id = getattr(campaign_like, "id", campaign_like)
        completion_token = set_tge_completion_overrides(
            cls._campaign_completion_overrides(campaign_id)
        )
        try:
            return await cls._emu.handle_setup_message(*args, **kwargs)
        finally:
            reset_tge_completion_overrides(completion_token)
            _zork_log_end(log_token)

    @classmethod
    def _campaign_completion_overrides(cls, campaign_id) -> dict[str, Any]:
        from discord_tron_master.classes.app_config import AppConfig

        config = AppConfig()
        resolved = config.get_zork_backend_config(default_backend="zai")
        campaign = cls.query_campaign(campaign_id) if campaign_id is not None else None
        if campaign is not None:
            state = cls._emu.get_campaign_state(campaign)
            raw = state.get("zork_backend_config") if isinstance(state, dict) else None
            if isinstance(raw, dict):
                backend = config.normalize_zork_backend(
                    raw.get("backend"),
                    default=str(resolved.get("backend") or "zai").strip() or "zai",
                )
                model = str(raw.get("model") or "").strip() or None
                thinking_value = raw.get("thinking_enabled")
                resolved = {
                    "backend": backend,
                    "model": model,
                    "thinking_enabled": (
                        thinking_value if isinstance(thinking_value, bool) else True
                    ),
                }
        return {
            "backend": str(resolved.get("backend") or "zai").strip() or "zai",
            "model": str(resolved.get("model") or "").strip() or None,
            "thinking_enabled": bool(resolved.get("thinking_enabled", True)),
        }

    @classmethod
    def end_turn(cls, campaign_id, user_id):
        cls._ensure_init()
        cls._emu.end_turn(str(campaign_id), str(user_id))

    @classmethod
    def pop_turn_ephemeral_notices(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu.pop_turn_ephemeral_notices(*args, **kwargs)

    @classmethod
    def record_turn_message_ids(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu.record_turn_message_ids(*args, **kwargs)

    @classmethod
    def bind_latest_narrator_message(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu.bind_latest_narrator_message(*args, **kwargs)

    @classmethod
    def get_latest_scene_output_for_actor(cls, campaign_id, actor_id):
        cls._ensure_init()
        campaign = cls.query_campaign(campaign_id)
        if campaign is None or actor_id is None:
            return None
        from text_game_engine.persistence.sqlalchemy.models import Turn

        with cls._session_factory() as session:
            turn = (
                session.query(Turn)
                .filter(
                    Turn.campaign_id == str(campaign.id),
                    Turn.actor_id == str(actor_id),
                    Turn.kind == "narrator",
                )
                .order_by(Turn.id.desc())
                .first()
            )
            if turn is None:
                return None
            meta = cls._emu._safe_turn_meta(turn) if hasattr(cls._emu, "_safe_turn_meta") else {}
        scene_output = meta.get("scene_output") if isinstance(meta, dict) else None
        return scene_output if isinstance(scene_output, dict) else None

    @classmethod
    def get_calendar_text(cls, campaign_id, actor_id=None):
        cls._ensure_init()
        campaign = cls.query_campaign(campaign_id)
        if campaign is None:
            return "No active campaign in this channel."
        campaign_state = cls._emu.get_campaign_state(campaign)
        player_calendar_lines = []
        player_state = {}
        if actor_id is not None:
            player = cls._emu.get_or_create_player(str(campaign.id), str(actor_id))
            player_state = cls._emu.get_player_state(player)
        game_time, _global_game_time, _time_model, _calendar_policy = cls._emu._current_game_time_for_prompt(
            campaign_state,
            player_state=player_state,
        )
        calendar_entries = cls._emu._calendar_for_prompt(
            campaign_state,
            player_state=player_state,
            viewer_actor_id=str(actor_id) if actor_id is not None else None,
        )
        if actor_id is not None:
            player_calendar_lines = cls._emu._player_calendar_events_for_display(
                player_state
            )
        date_label = game_time.get("date_label")
        if not date_label:
            day = game_time.get("day", "?")
            period = str(game_time.get("period", "?")).title()
            date_label = f"Day {day}, {period}"
        lines = [f"**Game Time:** {date_label}"]
        if calendar_entries:
            lines.append("**Upcoming Events:**")
            for event in calendar_entries:
                hours_remaining = int(
                    event.get("hours_remaining", int(event.get("days_remaining", 0)) * 24)
                )
                fire_day = int(event.get("fire_day", 1))
                fire_hour = max(0, min(23, int(event.get("fire_hour", 23))))
                desc = str(event.get("description", "") or "")
                if hours_remaining < 0:
                    eta = f"overdue by {abs(hours_remaining)} hour(s)"
                elif hours_remaining == 0:
                    eta = "fires now"
                elif hours_remaining < 48:
                    eta = f"fires in {hours_remaining} hour(s)"
                else:
                    eta_days = (hours_remaining + 23) // 24
                    eta = f"fires in {eta_days} day(s)"
                line = (
                    f"- **{event.get('name', 'Unknown')}** - "
                    f"Day {fire_day}, {fire_hour:02d}:00 ({eta})"
                )
                if desc:
                    line += f" ({desc})"
                lines.append(line)
        else:
            lines.append("No upcoming events.")
        if player_calendar_lines:
            lines.append("**Personal Events:**")
            lines.extend(player_calendar_lines)
        return "\n".join(lines)

    @classmethod
    def get_world_time_text(cls, campaign_id, actor_id=None):
        cls._ensure_init()
        campaign = cls.query_campaign(campaign_id)
        if campaign is None:
            return None
        campaign_state = cls._emu.get_campaign_state(campaign)
        player_state = {}
        if actor_id is not None:
            player = cls._emu.get_or_create_player(str(campaign.id), str(actor_id))
            player_state = cls._emu.get_player_state(player)
        game_time, _global_game_time, _time_model, _calendar_policy = cls._emu._current_game_time_for_prompt(
            campaign_state,
            player_state=player_state,
        )
        if not isinstance(game_time, dict):
            game_time = {}
        date_label = str(game_time.get("date_label") or "").strip()
        hour = game_time.get("hour")
        minute = game_time.get("minute")
        if not date_label:
            day = game_time.get("day", "?")
            period = str(game_time.get("period", "?")).title()
            date_label = f"Day {day}, {period}"
        try:
            hour_int = max(0, min(23, int(hour)))
            minute_int = max(0, min(59, int(minute)))
            return f"{date_label} ({hour_int:02d}:{minute_int:02d})"
        except (TypeError, ValueError):
            return date_label or None

    @classmethod
    def prepend_world_time_header(cls, text, campaign_id, actor_id=None):
        body = str(text or "").strip()
        if not body:
            return body
        if body.startswith("-# world time:"):
            return body
        label = cls.get_world_time_text(campaign_id, actor_id=actor_id)
        if not label:
            return body
        return f"-# world time: {label}\n{body}"

    @classmethod
    def get_latest_unread_sms_thread_for_actor(cls, campaign_id, actor_id, *, limit=20):
        cls._ensure_init()
        campaign = cls.query_campaign(campaign_id)
        if campaign is None:
            return None, None, []
        player = cls._emu.get_or_create_player(str(campaign.id), str(actor_id))
        player_state = cls._emu.get_player_state(player)
        campaign_state = cls._emu.get_campaign_state(campaign)
        contact_roster = cls._emu._sms_contact_roster(campaign)
        aliases = cls._emu._sms_player_aliases(actor_id=actor_id, player_state=player_state)
        actor_key = cls._emu._sms_actor_key(actor_id)
        read_state = cls._emu._sms_read_state_from_campaign_state(campaign_state)
        actor_row = read_state.get(actor_key) or {}
        read_threads = actor_row.get("threads")
        if not isinstance(read_threads, dict):
            read_threads = {}
        threads = cls._emu._sms_threads_from_state(campaign_state)

        best_thread = None
        best_label = None
        best_marker = -1
        for thread_key, row in threads.items():
            if not isinstance(row, dict):
                continue
            visible_messages = cls._emu._sms_visible_messages_for_viewer(
                row,
                viewer_actor_id=actor_id,
                player_state=player_state,
            )
            if not visible_messages:
                continue
            seen_marker = cls._emu._coerce_non_negative_int(
                read_threads.get(thread_key, 0), default=0
            )
            latest_unread_marker = 0
            for msg in visible_messages:
                if not isinstance(msg, dict):
                    continue
                to_norm = cls._emu._sms_normalize_thread_key(msg.get("to"))
                if not to_norm or to_norm not in aliases:
                    continue
                seq = cls._emu._coerce_non_negative_int(msg.get("seq", 0), default=0)
                turn_id = cls._emu._coerce_non_negative_int(msg.get("turn_id", 0), default=0)
                marker = seq if seq > 0 else turn_id
                if marker > seen_marker:
                    latest_unread_marker = max(latest_unread_marker, marker)
            if latest_unread_marker <= 0:
                continue
            resolved = cls._emu._sms_resolved_contact(
                thread_key,
                row,
                viewer_actor_id=actor_id,
                player_state=player_state,
                contact_roster=contact_roster,
                visible_messages=visible_messages,
            )
            resolved_thread = str(resolved.get("thread") or thread_key).strip() or thread_key
            resolved_label = str(resolved.get("label") or row.get("label") or thread_key).strip() or resolved_thread
            if latest_unread_marker > best_marker:
                best_marker = latest_unread_marker
                best_thread = resolved_thread
                best_label = resolved_label
        if not best_thread:
            return None, None, []
        return cls._emu.read_sms_thread(
            campaign.id,
            best_thread,
            limit=limit,
            viewer_actor_id=actor_id,
        )

    @classmethod
    def mark_unread_sms_read_for_actor(cls, campaign_id, actor_id):
        cls._ensure_init()
        from text_game_engine.persistence.sqlalchemy.models import Campaign
        with cls._session_factory() as session:
            campaign = session.get(Campaign, str(campaign_id))
            if campaign is None:
                return 0
            player = cls._emu.get_or_create_player(str(campaign.id), str(actor_id))
            player_state = cls._emu.get_player_state(player)
            campaign_state = cls._emu.get_campaign_state(campaign)
            aliases = cls._emu._sms_player_aliases(actor_id=actor_id, player_state=player_state)
            actor_key = cls._emu._sms_actor_key(actor_id)
            read_state = cls._emu._sms_read_state_from_campaign_state(campaign_state)
            actor_row = read_state.get(actor_key) or {}
            read_threads = actor_row.get("threads")
            if not isinstance(read_threads, dict):
                read_threads = {}
            threads = cls._emu._sms_threads_from_state(campaign_state)
            thread_markers = {}
            for thread_key, row in threads.items():
                if not isinstance(row, dict):
                    continue
                messages = cls._emu._sms_visible_messages_for_viewer(
                    row,
                    viewer_actor_id=actor_id,
                    player_state=player_state,
                )
                if not messages:
                    continue
                seen_marker = cls._emu._coerce_non_negative_int(
                    read_threads.get(thread_key, 0), default=0
                )
                latest_unread_marker = 0
                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    to_norm = cls._emu._sms_normalize_thread_key(msg.get("to"))
                    if not to_norm or to_norm not in aliases:
                        continue
                    seq = cls._emu._coerce_non_negative_int(msg.get("seq", 0), default=0)
                    turn_id = cls._emu._coerce_non_negative_int(msg.get("turn_id", 0), default=0)
                    marker = seq if seq > 0 else turn_id
                    if marker > seen_marker:
                        latest_unread_marker = max(latest_unread_marker, marker)
                if latest_unread_marker > 0:
                    thread_markers[thread_key] = latest_unread_marker
            if not thread_markers:
                return 0
            changed = cls._emu._sms_mark_threads_read(
                campaign_state,
                actor_id=actor_id,
                player_state=player_state,
                thread_markers=thread_markers,
            )
            if not changed:
                return 0
            campaign.state_json = cls._emu._dump_json(campaign_state)
            session.commit()
            return len(thread_markers)

    # -- Player Management -----------------------------------------------------

    @classmethod
    def get_or_create_player(cls, campaign_id, user_id, *, campaign=None):
        cls._ensure_init()
        return cls._emu.get_or_create_player(str(campaign_id), str(user_id))

    @classmethod
    def get_player_state(cls, player):
        cls._ensure_init()
        return cls._emu.get_player_state(player)

    @classmethod
    def get_player_attributes(cls, player):
        cls._ensure_init()
        return cls._emu.get_player_attributes(player)

    @classmethod
    def set_attribute(cls, player, name, value):
        cls._ensure_init()
        return cls._emu.set_attribute(player, name, value)

    @classmethod
    def level_up(cls, player):
        cls._ensure_init()
        return cls._emu.level_up(player)

    @classmethod
    def get_player_statistics(cls, player):
        cls._ensure_init()
        return cls._emu.get_player_statistics(player)

    @classmethod
    def total_points_for_level(cls, level):
        cls._ensure_init()
        return cls._emu.total_points_for_level(level)

    @classmethod
    def points_spent(cls, attrs):
        cls._ensure_init()
        return cls._emu.points_spent(attrs)

    @classmethod
    def xp_needed_for_level(cls, level):
        cls._ensure_init()
        return cls._emu.xp_needed_for_level(level)

    # -- Campaign Configuration ------------------------------------------------

    @classmethod
    def is_in_setup_mode(cls, campaign):
        cls._ensure_init()
        return cls._emu.is_in_setup_mode(campaign)

    @classmethod
    def is_guardrails_enabled(cls, campaign):
        cls._ensure_init()
        return cls._emu.is_guardrails_enabled(campaign)

    @classmethod
    def set_guardrails_enabled(cls, campaign, value):
        cls._ensure_init()
        return cls._emu.set_guardrails_enabled(campaign, value)

    @classmethod
    def is_on_rails(cls, campaign):
        cls._ensure_init()
        return cls._emu.is_on_rails(campaign)

    @classmethod
    def set_on_rails(cls, campaign, value):
        cls._ensure_init()
        return cls._emu.set_on_rails(campaign, value)

    @classmethod
    def is_timed_events_enabled(cls, campaign):
        cls._ensure_init()
        return cls._emu.is_timed_events_enabled(campaign)

    @classmethod
    def set_timed_events_enabled(cls, campaign, value):
        cls._ensure_init()
        return cls._emu.set_timed_events_enabled(campaign, value)

    @classmethod
    def get_difficulty(cls, campaign):
        cls._ensure_init()
        return cls._emu.get_difficulty(campaign)

    @classmethod
    def set_difficulty(cls, campaign, value):
        cls._ensure_init()
        return cls._emu.set_difficulty(campaign, value)

    @classmethod
    def normalize_difficulty(cls, value):
        cls._ensure_init()
        return cls._emu.normalize_difficulty(value)

    @classmethod
    def get_speed_multiplier(cls, campaign):
        cls._ensure_init()
        return cls._emu.get_speed_multiplier(campaign)

    @classmethod
    def set_speed_multiplier(cls, campaign, value):
        cls._ensure_init()
        return cls._emu.set_speed_multiplier(campaign, value)

    @classmethod
    def get_timed_events_speed_multiplier(cls, campaign):
        cls._ensure_init()
        return cls._emu.get_timed_events_speed_multiplier(campaign)

    @classmethod
    def set_timed_events_speed_multiplier(cls, campaign, value):
        cls._ensure_init()
        return cls._emu.set_timed_events_speed_multiplier(campaign, value)

    @classmethod
    def get_campaign_clock(cls, campaign):
        cls._ensure_init()
        return cls._emu.get_campaign_clock(campaign)

    @classmethod
    def set_campaign_clock(cls, campaign, *, day, hour, minute=0, day_of_week=None):
        cls._ensure_init()
        return cls._emu.set_campaign_clock(
            campaign,
            day=day,
            hour=hour,
            minute=minute,
            day_of_week=day_of_week,
        )

    @classmethod
    def get_campaign_clock_type(cls, campaign):
        cls._ensure_init()
        return cls._emu.get_campaign_clock_type(campaign)

    @classmethod
    def set_campaign_clock_type(cls, campaign, value):
        cls._ensure_init()
        return cls._emu.set_campaign_clock_type(campaign, value)

    # -- Campaign Rules --------------------------------------------------------

    @classmethod
    def list_campaign_rules(cls, campaign_id):
        cls._ensure_init()
        return cls._emu.list_campaign_rules(str(campaign_id))

    @classmethod
    def get_campaign_rule(cls, campaign_id, rule_name):
        cls._ensure_init()
        return cls._emu.get_campaign_rule(str(campaign_id), rule_name)

    @classmethod
    def put_campaign_rule(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu.put_campaign_rule(*args, **kwargs)

    # -- Rewind / History ------------------------------------------------------

    @classmethod
    def execute_rewind(cls, *args, **kwargs):
        cls._ensure_init()
        fn = cls._emu.execute_rewind
        return fn(*args, **cls._filter_supported_kwargs(fn, kwargs))

    @classmethod
    def execute_delete_turn(cls, *args, **kwargs):
        cls._ensure_init()
        mapped_kwargs = dict(kwargs)
        channel_id = mapped_kwargs.pop("channel_id", None)
        delete_user_id = mapped_kwargs.pop("delete_user_id", None)
        if delete_user_id is not None and "delete_actor_id" not in mapped_kwargs:
            mapped_kwargs["delete_actor_id"] = str(delete_user_id)
        if channel_id is not None and "session_id" not in mapped_kwargs:
            session_obj = cls.query_channel_by_channel_id(channel_id)
            if session_obj is not None and getattr(session_obj, "id", None):
                mapped_kwargs["session_id"] = str(session_obj.id)
        fn = cls._emu.execute_delete_turn
        return fn(*args, **cls._filter_supported_kwargs(fn, mapped_kwargs))

    @classmethod
    def get_turn_for_message(cls, message_id):
        """Query TGE Turn by external_message_id (was discord_message_id)."""
        cls._ensure_init()
        try:
            mid = str(int(message_id))
        except (TypeError, ValueError):
            return None
        from text_game_engine.persistence.sqlalchemy.models import Turn
        with cls._session_factory() as session:
            return (
                session.query(Turn)
                .filter(Turn.external_message_id == mid, Turn.kind == "narrator")
                .order_by(Turn.id.desc())
                .first()
            )

    @classmethod
    def list_recent_turn_message_refs(cls, *, limit_per_campaign: int = 5):
        """Return recent narrator-turn Discord message refs for enabled sessions.

        Each row contains enough data for Discord startup/bootstrap code to
        refetch the message and restore control reactions after reconnect.
        """
        cls._ensure_init()
        try:
            limit = max(1, int(limit_per_campaign))
        except (TypeError, ValueError):
            limit = 5
        from text_game_engine.persistence.sqlalchemy.models import (
            Session as GameSession,
            Turn,
        )

        refs = []
        seen: set[tuple[str, str]] = set()
        with cls._session_factory() as session:
            sessions = (
                session.query(GameSession)
                .filter(
                    GameSession.enabled == True,  # noqa: E712
                    GameSession.campaign_id.isnot(None),
                )
                .all()
            )
            for sess in sessions:
                surface_channel_id = (
                    str(
                        getattr(sess, "surface_thread_id", None)
                        or getattr(sess, "surface_channel_id", None)
                        or ""
                    ).strip()
                )
                if not surface_channel_id:
                    continue
                turns = (
                    session.query(Turn)
                    .filter(
                        Turn.campaign_id == str(sess.campaign_id),
                        Turn.session_id == str(sess.id),
                        Turn.kind == "narrator",
                        Turn.external_message_id.isnot(None),
                    )
                    .order_by(Turn.id.desc())
                    .limit(limit)
                    .all()
                )
                for turn in turns:
                    message_id = str(getattr(turn, "external_message_id", "") or "").strip()
                    if not message_id:
                        continue
                    dedupe_key = (surface_channel_id, message_id)
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    refs.append(
                        {
                            "campaign_id": str(sess.campaign_id),
                            "session_id": str(sess.id),
                            "channel_id": surface_channel_id,
                            "message_id": message_id,
                            "turn_id": int(getattr(turn, "id", 0) or 0),
                        }
                    )
        refs.sort(key=lambda row: int(row.get("turn_id") or 0), reverse=True)
        return refs

    @classmethod
    def get_turn_info_text_for_message(cls, message_id):
        """Build turn info text from TGE Turn + snapshot data."""
        cls._ensure_init()
        try:
            mid = str(int(message_id))
        except (TypeError, ValueError):
            return None
        from text_game_engine.persistence.sqlalchemy.models import (
            Campaign, Player, Snapshot, Timer, Turn,
        )
        with cls._session_factory() as session:
            turn = (
                session.query(Turn)
                .filter(Turn.external_message_id == mid, Turn.kind == "narrator")
                .order_by(Turn.id.desc())
                .first()
            )
            if turn is None:
                timer = (
                    session.query(Timer)
                    .filter(Timer.external_message_id == mid)
                    .order_by(Timer.updated_at.desc(), Timer.created_at.desc())
                    .first()
                )
                if timer is None:
                    return None
                campaign = (
                    session.get(Campaign, timer.campaign_id)
                    if getattr(timer, "campaign_id", None)
                    else None
                )
                campaign_state = cls._emu.get_campaign_state(campaign) if campaign else {}
                lines = ["Timed Event"]
                game_time = campaign_state.get("game_time") or campaign_state.get("_game_time") or {}
                if isinstance(game_time, dict) and game_time:
                    day = game_time.get("day", "?")
                    hour = game_time.get("hour", "?")
                    minute = game_time.get("minute", "?")
                    period = game_time.get("period", "")
                    lines.append(f"World Time: Day {day}, {hour}:{str(minute).zfill(2)} {period}".strip())
                status = str(getattr(timer, "status", "") or "").strip() or "unknown"
                lines.append(f"Status: {status}")
                event_text = str(getattr(timer, "event_text", "") or "").strip()
                if event_text:
                    lines.append(f"Event: {event_text}")
                due_at = getattr(timer, "due_at", None)
                if due_at is not None:
                    try:
                        import calendar as _calendar

                        due_ts = int(_calendar.timegm(due_at.utctimetuple()))
                        lines.append(f"Due: <t:{due_ts}:F> (<t:{due_ts}:R>)")
                    except Exception:
                        lines.append(f"Due: {due_at}")
                interruptible = getattr(timer, "interruptible", None)
                if interruptible is not None:
                    lines.append(
                        "Interruptible: yes" if bool(interruptible) else "Interruptible: no"
                    )
                interrupt_action = str(getattr(timer, "interrupt_action", "") or "").strip()
                if interrupt_action:
                    lines.append(f"Interrupt: {interrupt_action}")
                return "\n".join(lines)
            campaign = session.get(Campaign, turn.campaign_id) if turn.campaign_id else None
            player = None
            if turn.campaign_id and turn.actor_id:
                player = session.query(Player).filter_by(
                    campaign_id=turn.campaign_id,
                    actor_id=turn.actor_id,
                ).first()

            # Load meta from turn
            meta = cls._emu._safe_turn_meta(turn) if hasattr(cls._emu, "_safe_turn_meta") else {}
            campaign_state = cls._emu.get_campaign_state(campaign) if campaign else {}
            player_state = {}
            inventory_line = "Inventory: empty"

            snapshot = session.query(Snapshot).filter_by(turn_id=turn.id).first()
            if snapshot is not None:
                snapshot_cs = cls._emu._load_json(
                    getattr(snapshot, "campaign_state_json", "{}") or "{}", {}
                )
                if isinstance(snapshot_cs, dict) and snapshot_cs:
                    campaign_state = snapshot_cs
                try:
                    players_data = json.loads(
                        getattr(snapshot, "players_json", "[]") or "[]"
                    )
                except Exception:
                    players_data = []
                for row in players_data:
                    if not isinstance(row, dict):
                        continue
                    p_actor = str(row.get("player_id") or row.get("actor_id") or "")
                    if p_actor == str(turn.actor_id or ""):
                        raw_state = row.get("state_json")
                        if isinstance(raw_state, dict):
                            player_state = raw_state
                        else:
                            player_state = cls._emu._load_json(raw_state or "{}", {})
                        inventory_line = (
                            cls._emu._format_inventory(player_state) or "Inventory: empty"
                        )
                        break

            if not player_state and player is not None:
                player_state = cls._emu.get_player_state(player)
                inventory_line = (
                    cls._emu._format_inventory(player_state) or "Inventory: empty"
                )

            # Build info lines
            lines = []
            # Location
            location = player_state.get("current_location") or player_state.get("look") or ""
            if location:
                lines.append(f"Location: {location}")
            # Calendar/time
            game_time = campaign_state.get("game_time") or campaign_state.get("_game_time") or {}
            if isinstance(game_time, dict) and game_time:
                day = game_time.get("day", "?")
                hour = game_time.get("hour", "?")
                minute = game_time.get("minute", "?")
                period = game_time.get("period", "")
                lines.append(f"Time: Day {day}, {hour}:{str(minute).zfill(2)} {period}".strip())
            # Character
            char_name = player_state.get("character_name") or ""
            if char_name:
                lines.append(f"Character: {char_name}")
            if player is not None:
                lines.append(f"Level: {getattr(player, 'level', 1)} | XP: {getattr(player, 'xp', 0)}")
            # Inventory
            lines.append(inventory_line)
            # Reasoning (spoiler)
            reasoning = meta.get("reasoning") if isinstance(meta, dict) else None
            if reasoning:
                lines.append(f"||Reasoning: {reasoning}||")
            else:
                lines.append("Reasoning: unavailable.")
            return "\n".join(lines)

    # -- Source Material & Export -----------------------------------------------

    @classmethod
    async def ingest_source_material_text(cls, *args, **kwargs):
        cls._ensure_init()
        return await cls._emu.ingest_source_material_text(*args, **kwargs)

    @classmethod
    async def _analyze_literary_style(cls, *args, **kwargs):
        cls._ensure_init()
        return await cls._emu._analyze_literary_style(*args, **kwargs)

    @classmethod
    async def _extract_attachment_text(cls, *args, **kwargs):
        cls._ensure_init()
        return await cls._emu._extract_attachment_text(*args, **kwargs)

    @classmethod
    async def _extract_attachment_texts_from_message(cls, *args, **kwargs):
        cls._ensure_init()
        return await cls._emu._extract_attachment_texts_from_message(*args, **kwargs)

    @classmethod
    async def _classify_source_material_format(cls, *args, **kwargs):
        cls._ensure_init()
        return await cls._emu._classify_source_material_format(*args, **kwargs)

    @classmethod
    async def _summarise_long_text(cls, *args, **kwargs):
        cls._ensure_init()
        return await cls._emu._summarise_long_text(*args, **kwargs)

    @classmethod
    async def _generate_campaign_export_artifacts(cls, *args, **kwargs):
        cls._ensure_init()
        fn = cls._emu._generate_campaign_export_artifacts
        return await fn(
            *cls._trim_supported_args(fn, args),
            **cls._filter_supported_kwargs(fn, kwargs),
        )

    @classmethod
    async def _generate_campaign_raw_export_artifacts(cls, *args, **kwargs):
        cls._ensure_init()
        fn = cls._emu._generate_campaign_raw_export_artifacts
        return await fn(
            *cls._trim_supported_args(fn, args),
            **cls._filter_supported_kwargs(fn, kwargs),
        )

    @classmethod
    async def start_campaign_setup(cls, *args, **kwargs):
        cls._ensure_init()
        return await cls._emu.start_campaign_setup(*args, **kwargs)

    # -- Avatar & Media --------------------------------------------------------

    @classmethod
    async def enqueue_avatar_generation(cls, *args, **kwargs):
        cls._ensure_init()
        return await cls._emu.enqueue_avatar_generation(*args, **kwargs)

    @classmethod
    def accept_pending_avatar(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu.accept_pending_avatar(*args, **kwargs)

    @classmethod
    def decline_pending_avatar(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu.decline_pending_avatar(*args, **kwargs)

    @classmethod
    async def _enqueue_character_portrait(cls, *args, **kwargs):
        cls._ensure_init()
        return await cls._emu._enqueue_character_portrait(*args, **kwargs)

    @classmethod
    def record_room_scene_image_url_for_channel(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu.record_room_scene_image_url_for_channel(*args, **kwargs)

    @classmethod
    async def enqueue_scene_composite_from_seed(cls, *args, **kwargs):
        cls._ensure_init()
        return await cls._emu.enqueue_scene_composite_from_seed(*args, **kwargs)

    @classmethod
    def record_pending_avatar_image_for_campaign(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu.record_pending_avatar_image_for_campaign(*args, **kwargs)

    @classmethod
    def record_character_portrait_url(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu.record_character_portrait_url(*args, **kwargs)

    # -- Timer & Lifecycle -----------------------------------------------------

    @classmethod
    def register_timer_message(cls, campaign_id, message_id):
        cls._ensure_init()
        return cls._emu.register_timer_message(str(campaign_id), str(message_id))

    @classmethod
    def has_active_timer_for_message(cls, message_id):
        cls._ensure_init()
        return cls._emu.has_active_timer_for_message(str(message_id))

    @classmethod
    def extend_pending_timer_for_message(cls, message_id, *, extra_seconds=60):
        cls._ensure_init()
        return cls._emu.extend_pending_timer_for_message(
            str(message_id),
            extra_seconds=extra_seconds,
        )

    @classmethod
    def get_pending_timer_notice(cls, campaign_id):
        cls._ensure_init()
        return cls._emu.get_pending_timer_notice(str(campaign_id))

    @classmethod
    def get_timed_event_in_progress_notice(cls, campaign_id, actor_id):
        cls._ensure_init()
        return cls._emu.get_timed_event_in_progress_notice(
            str(campaign_id), str(actor_id)
        )

    @classmethod
    def cancel_pending_timer(cls, campaign_id):
        cls._ensure_init()
        return cls._emu.cancel_pending_timer(str(campaign_id))

    @classmethod
    def cancel_pending_sms_deliveries(cls, campaign_id):
        cls._ensure_init()
        return cls._emu.cancel_pending_sms_deliveries(str(campaign_id))

    @classmethod
    def request_shutdown(cls):
        cls._ensure_init()
        fn = getattr(cls._emu, "request_shutdown", None)
        if callable(fn):
            return fn()
        cls._shutdown_requested = True
        backend_inflight = getattr(cls._emu, "_inflight_turns", None)
        if backend_inflight is not None:
            cls._inflight_turns = backend_inflight
        logger.info("EmulatorBridge: using bridge-local shutdown/drain fallback")
        return None

    @classmethod
    def clear_all_inflight_claims(cls) -> int:
        cls._ensure_init()
        deleted = 0
        try:
            session_factory = cls._session_factory
            if session_factory is not None:
                from text_game_engine.persistence.sqlalchemy.models import InflightTurn

                with session_factory() as session:
                    deleted = int(session.query(InflightTurn).delete() or 0)
                    session.commit()
        except Exception:
            logger.warning("Failed to clear inflight turn claims", exc_info=True)
            return 0
        try:
            backend_inflight = getattr(cls._emu, "_inflight_turns", None)
            backend_lock = getattr(cls._emu, "_inflight_turns_lock", None)
            if backend_inflight is not None:
                if backend_lock is not None:
                    with backend_lock:
                        backend_inflight.clear()
                else:
                    backend_inflight.clear()
            claims = getattr(cls._emu, "_claims", None)
            if isinstance(claims, dict):
                claims.clear()
        except Exception:
            logger.debug("Failed to clear emulator-local inflight state", exc_info=True)
        cls._inflight_turns = set()
        if deleted:
            logger.warning("Cleared %s inflight turn claim(s)", deleted)
        return deleted

    @classmethod
    async def wait_for_drain(cls, timeout=600):
        cls._ensure_init()
        fn = getattr(cls._emu, "wait_for_drain", None)
        if callable(fn):
            drained = await fn(timeout=timeout)
            if drained:
                return True
        backend_inflight = getattr(cls._emu, "_inflight_turns", None)
        backend_lock = getattr(cls._emu, "_inflight_turns_lock", None)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0, float(timeout))
        while loop.time() < deadline:
            try:
                if backend_inflight is None:
                    active = set()
                elif backend_lock is not None:
                    with backend_lock:
                        active = set(backend_inflight)
                else:
                    active = set(backend_inflight)
            except Exception:
                active = set()
            db_active = 0
            try:
                session_factory = cls._session_factory
                if session_factory is not None:
                    from datetime import datetime, timezone
                    from text_game_engine.persistence.sqlalchemy.models import InflightTurn

                    with session_factory() as session:
                        now = datetime.now(timezone.utc).replace(tzinfo=None)
                        db_active = int(
                            session.query(InflightTurn)
                            .filter(InflightTurn.expires_at > now)
                            .count()
                        )
            except Exception:
                db_active = 0
            cls._inflight_turns = active
            if not active and db_active <= 0:
                return True
            await asyncio.sleep(0.25)
        try:
            if backend_inflight is None:
                cls._inflight_turns = set()
            elif backend_lock is not None:
                with backend_lock:
                    cls._inflight_turns = set(backend_inflight)
            else:
                cls._inflight_turns = set(backend_inflight)
        except Exception:
            pass
        return False

    # -- Utility / Processing --------------------------------------------------

    @classmethod
    async def _add_processing_reaction(cls, ctx_or_message):
        cls._ensure_init()
        return await cls._emu._add_processing_reaction(ctx_or_message)

    @classmethod
    async def _remove_processing_reaction(cls, ctx_or_message):
        cls._ensure_init()
        return await cls._emu._remove_processing_reaction(ctx_or_message)

    @classmethod
    def _dump_json(cls, data):
        cls._ensure_init()
        return cls._emu._dump_json(data)

    @classmethod
    def _normalize_campaign_name(cls, name):
        cls._ensure_init()
        return cls._emu._normalize_campaign_name(name)

    @classmethod
    def _source_material_format_heuristic(cls, text):
        cls._ensure_init()
        return cls._emu._source_material_format_heuristic(text)

    @classmethod
    def _extract_attachment_label(cls, attachments, fallback="source-material"):
        cls._ensure_init()
        return cls._emu._extract_attachment_label(attachments, fallback=fallback)

    @classmethod
    def _chunk_text_by_tokens(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu._chunk_text_by_tokens(*args, **kwargs)

    @classmethod
    def _player_slug_key(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu._player_slug_key(*args, **kwargs)

    @classmethod
    def _room_key_from_player_state(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu._room_key_from_player_state(*args, **kwargs)

    @classmethod
    def _active_scene_npc_slugs(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu._active_scene_npc_slugs(*args, **kwargs)

    @classmethod
    def _plot_hints_for_viewer(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu._plot_hints_for_viewer(*args, **kwargs)

    @classmethod
    def _plot_threads_for_prompt(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu._plot_threads_for_prompt(*args, **kwargs)

    @classmethod
    def format_roster(cls, characters):
        cls._ensure_init()
        return cls._emu.format_roster(characters)

    @classmethod
    async def generate_map(cls, *args, **kwargs):
        cls._ensure_init()
        return await cls._emu.generate_map(*args, **kwargs)

    @classmethod
    async def _edit_progress_message(cls, *args, **kwargs):
        cls._ensure_init()
        fn = getattr(cls._emu, "_edit_progress_message", None)
        if callable(fn):
            return await fn(*args, **cls._filter_supported_kwargs(fn, kwargs))
        status_message = args[0] if args else kwargs.get("status_message")
        content = args[1] if len(args) > 1 else kwargs.get("content")
        if status_message is None:
            return None
        try:
            await status_message.edit(content=str(content or "").strip()[:3900] or "Working...")
        except Exception:
            return None
        return None

    @classmethod
    async def _delete_progress_message(cls, *args, **kwargs):
        cls._ensure_init()
        fn = getattr(cls._emu, "_delete_progress_message", None)
        if callable(fn):
            return await fn(*args, **cls._filter_supported_kwargs(fn, kwargs))
        status_message = args[0] if args else kwargs.get("status_message")
        if status_message is None:
            return None
        try:
            await status_message.delete()
        except Exception:
            return None
        return None

    @classmethod
    def _get_lock(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu._get_lock(*args, **kwargs)

    # ── ORM Query Helpers (replaces ZorkCampaign.query.get etc.) ─────
    # These let the cog query TGE model objects the same way it queried
    # the old Flask-SQLAlchemy models.

    @classmethod
    def query_campaign(cls, campaign_id):
        """Equivalent of ZorkCampaign.query.get(campaign_id).

        Returns a TGE Campaign detached from session (expire_on_commit=False),
        so callers can read/write attributes freely.
        """
        if campaign_id is None:
            return None
        cls._ensure_init()
        from text_game_engine.persistence.sqlalchemy.models import Campaign
        cid = str(campaign_id)
        with cls._session_factory() as session:
            return session.get(Campaign, cid)

    @classmethod
    def query_campaign_for_channel(cls, channel_session):
        """Given a TGE Session (channel), return its active Campaign."""
        if channel_session is None:
            return None
        cls._ensure_init()
        from text_game_engine.persistence.sqlalchemy.models import Campaign
        # TGE Session.campaign_id IS the active campaign
        cid = getattr(channel_session, "campaign_id", None)
        if not cid:
            return None
        with cls._session_factory() as session:
            return session.get(Campaign, cid)

    @classmethod
    def query_channel_by_channel_id(cls, channel_id):
        """Equivalent of ZorkChannel.query.filter_by(channel_id=X).first()."""
        cls._ensure_init()
        from text_game_engine.persistence.sqlalchemy.models import Session as GameSession
        with cls._session_factory() as session:
            return (
                session.query(GameSession)
                .filter(GameSession.surface_channel_id == str(channel_id))
                .first()
            )

    @classmethod
    def query_players_for_campaign(cls, campaign_id):
        """Equivalent of ZorkPlayer.query.filter_by(campaign_id=X).all()."""
        cls._ensure_init()
        from text_game_engine.persistence.sqlalchemy.models import Player
        with cls._session_factory() as session:
            return session.query(Player).filter(
                Player.campaign_id == str(campaign_id)
            ).all()

    @classmethod
    def count_channels_for_campaign(cls, campaign_id, exclude_channel_id=None, guild_id=None):
        """Count Sessions referencing a campaign (for shared-campaign checks)."""
        cls._ensure_init()
        from text_game_engine.persistence.sqlalchemy.models import Session as GameSession
        with cls._session_factory() as session:
            q = session.query(GameSession).filter(
                GameSession.campaign_id == str(campaign_id),
                GameSession.enabled == True,
            )
            if guild_id is not None:
                q = q.filter(GameSession.surface_guild_id == str(guild_id))
            if exclude_channel_id is not None:
                q = q.filter(GameSession.surface_channel_id != str(exclude_channel_id))
            return q.count()

    @classmethod
    def delete_campaign_data(cls, campaign_id):
        """Delete all turns, snapshots, and players for a campaign.

        Used by the reset command. Equivalent of the old
        ZorkTurn/ZorkSnapshot/ZorkPlayer .query.filter_by().delete() calls.
        """
        cls._ensure_init()
        from text_game_engine.persistence.sqlalchemy.models import (
            Player, Snapshot, Turn,
        )
        with cls._session_factory() as session:
            cid = str(campaign_id)
            session.query(Snapshot).filter(Snapshot.campaign_id == cid).delete(
                synchronize_session=False
            )
            session.query(Turn).filter(Turn.campaign_id == cid).delete(
                synchronize_session=False
            )
            session.query(Player).filter(Player.campaign_id == cid).delete(
                synchronize_session=False
            )
            session.commit()

    @classmethod
    def commit_model(cls, obj):
        """Merge a detached model object back to the DB and commit.

        Replaces the old pattern of: obj.field = X; db.session.commit()
        Since TGE models are detached (expire_on_commit=False), we
        need to merge them back into a session to persist changes.
        """
        cls._ensure_init()
        with cls._session_factory() as session:
            session.merge(obj)
            session.commit()

    @classmethod
    def commit_models(cls, *objs):
        """Merge multiple detached model objects and commit in one transaction."""
        cls._ensure_init()
        with cls._session_factory() as session:
            for obj in objs:
                session.merge(obj)
            session.commit()

    @classmethod
    def utcnow(cls):
        """Return UTC now() as a naive datetime, for setting updated_at fields."""
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).replace(tzinfo=None)

    @classmethod
    def memory_campaign_id(cls, campaign_id_or_obj) -> int:
        from discord_tron_master.adapters.tge_ports import ZorkMemoryAdapter

        raw = getattr(campaign_id_or_obj, "id", campaign_id_or_obj)
        return ZorkMemoryAdapter._int_campaign_id(str(raw))

    @classmethod
    def _migrate_legacy_memory_ids(cls) -> None:
        from text_game_engine.persistence.sqlalchemy.models import Campaign
        from discord_tron_master.classes import zork_memory as _zm_mod

        db_path = getattr(_zm_mod, "_DB_PATH", "")
        if not db_path or not os.path.exists(db_path):
            return

        mappings: list[tuple[int, int, str]] = []
        with cls._session_factory() as session:
            for campaign in session.query(Campaign).all():
                try:
                    state = json.loads(getattr(campaign, "state_json", "{}") or "{}")
                except Exception:
                    continue
                if not isinstance(state, dict):
                    continue
                legacy = state.get("_legacy_campaign_id")
                try:
                    old_id = int(legacy)
                except (TypeError, ValueError):
                    continue
                mappings.append((old_id, cls.memory_campaign_id(campaign.id), str(campaign.id)))

        if not mappings:
            return

        migrated_campaign_ids: set[str] = set()
        conn = sqlite3.connect(db_path)
        try:
            for old_id, new_id, campaign_id in mappings:
                if old_id != new_id:
                    for table in (
                        "turn_embeddings",
                        "manual_memories",
                        "source_material_chunks",
                        "source_material_digests",
                        "turn_embedding_visible_players",
                        "turn_embedding_aware_npcs",
                    ):
                        conn.execute(
                            f"UPDATE {table} SET campaign_id = ? WHERE campaign_id = ?",
                            (new_id, old_id),
                        )
                migrated_campaign_ids.add(campaign_id)
            conn.commit()
        finally:
            conn.close()

        with cls._session_factory() as session:
            changed = False
            for campaign in session.query(Campaign).filter(
                Campaign.id.in_(sorted(migrated_campaign_ids))
            ).all():
                try:
                    state = json.loads(getattr(campaign, "state_json", "{}") or "{}")
                except Exception:
                    continue
                if not isinstance(state, dict) or "_legacy_campaign_id" not in state:
                    continue
                state.pop("_legacy_campaign_id", None)
                campaign.state_json = json.dumps(state, separators=(",", ":"))
                changed = True
            if changed:
                session.commit()

    # -- Fallback for any method not explicitly bridged ------------------------

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __class_getitem__(cls, name):
        """Not used, but prevents TypeError on subscript access."""
        raise TypeError(f"EmulatorBridge is not subscriptable")
