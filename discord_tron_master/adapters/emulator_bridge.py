"""Drop-in bridge replacing the old ZorkEmulator classmethod interface.

Usage:
    from discord_tron_master.adapters.emulator_bridge import EmulatorBridge as ZorkEmulator
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


class EmulatorBridge:
    """Singleton classmethod facade over TGE's instance-based ZorkEmulator.

    Every public/private method the cog calls is forwarded to the TGE instance.
    Lazy-initialized on first use.
    """

    _emu = None  # TGE ZorkEmulator instance
    _session_factory = None
    _init_lock = threading.Lock()
    _initialized = False

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
            return GPT()

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

        game_engine = GameEngine(uow_factory=_uow_factory, llm=llm)

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
        logger.info("EmulatorBridge: TGE ZorkEmulator initialized")

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
    def is_channel_enabled(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu.is_channel_enabled(*args, **kwargs)

    # -- Campaign Management ---------------------------------------------------

    @classmethod
    def create_campaign(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu.create_campaign(*args, **kwargs)

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
    async def begin_turn_for_campaign(cls, *args, **kwargs):
        cls._ensure_init()
        return await cls._emu.begin_turn_for_campaign(*args, **kwargs)

    @classmethod
    async def play_action(cls, *args, **kwargs):
        cls._ensure_init()
        return await cls._emu.play_action(*args, **kwargs)

    @classmethod
    async def handle_setup_message(cls, *args, **kwargs):
        cls._ensure_init()
        return await cls._emu.handle_setup_message(*args, **kwargs)

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
        return cls._emu.execute_rewind(*args, **kwargs)

    @classmethod
    def execute_delete_turn(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu.execute_delete_turn(*args, **kwargs)

    @classmethod
    def get_turn_for_message(cls, message_id):
        cls._ensure_init()
        return cls._emu.get_turn_for_message(str(message_id))

    @classmethod
    def get_turn_info_text_for_message(cls, *args, **kwargs):
        cls._ensure_init()
        return cls._emu.get_turn_info_text_for_message(*args, **kwargs)

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
        return await cls._emu._generate_campaign_export_artifacts(*args, **kwargs)

    @classmethod
    async def _generate_campaign_raw_export_artifacts(cls, *args, **kwargs):
        cls._ensure_init()
        return await cls._emu._generate_campaign_raw_export_artifacts(*args, **kwargs)

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
        return cls._emu.request_shutdown()

    @classmethod
    async def wait_for_drain(cls, timeout=120):
        cls._ensure_init()
        return await cls._emu.wait_for_drain(timeout=timeout)

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
        return await cls._emu._edit_progress_message(*args, **kwargs)

    @classmethod
    async def _delete_progress_message(cls, *args, **kwargs):
        cls._ensure_init()
        return await cls._emu._delete_progress_message(*args, **kwargs)

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
        with cls._session_factory() as session:
            return session.get(Campaign, str(campaign_id))

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

    # -- Fallback for any method not explicitly bridged ------------------------

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __class_getitem__(cls, name):
        """Not used, but prevents TypeError on subscript access."""
        raise TypeError(f"EmulatorBridge is not subscriptable")

    @classmethod
    def __getattr__(cls, name):
        """Catch-all for any method not explicitly defined above."""
        cls._ensure_init()
        attr = getattr(cls._emu, name, None)
        if attr is not None:
            return attr
        raise AttributeError(f"EmulatorBridge has no attribute {name!r}")
