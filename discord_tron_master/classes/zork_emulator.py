import asyncio
import datetime
import json
import logging
import os
import re
import threading
import time
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

import discord
import requests
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.classes.openai.text import GPT
from discord_tron_master.classes.zork_memory import ZorkMemory
from discord_tron_master.bot import DiscordBot
from discord_tron_master.models.base import db
from discord_tron_master.models.zork import (
    ZorkCampaign,
    ZorkChannel,
    ZorkPlayer,
    ZorkTurn,
)

logger = logging.getLogger(__name__)
logger.setLevel("INFO")

_ZORK_LOG_PATH = os.path.join(os.getcwd(), "zork.log")


def _zork_log(section: str, body: str = "") -> None:
    """Append a timestamped section to zork.log in the process working dir."""
    try:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_ZORK_LOG_PATH, "a") as fh:
            fh.write(f"\n{'='*72}\n[{ts}] {section}\n{'='*72}\n")
            if body:
                fh.write(body)
                if not body.endswith("\n"):
                    fh.write("\n")
    except Exception:
        pass


class ZorkEmulator:
    BASE_POINTS = 10
    POINTS_PER_LEVEL = 5
    MAX_ATTRIBUTE_VALUE = 20
    MAX_SUMMARY_CHARS = 4000
    MAX_STATE_CHARS = 8000
    MAX_RECENT_TURNS = 24
    MAX_TURN_CHARS = 1200
    MAX_NARRATION_CHARS = 3500
    MAX_PARTY_CONTEXT_PLAYERS = 6
    MAX_SCENE_PROMPT_CHARS = 900
    MAX_PERSONA_PROMPT_CHARS = 140
    MAX_SCENE_REFERENCE_IMAGES = 10
    XP_BASE = 100
    XP_PER_LEVEL = 50
    MAX_INVENTORY_CHANGES_PER_TURN = 10
    MAX_CHARACTERS_CHARS = 3000
    IMMUTABLE_CHARACTER_FIELDS = {"name", "personality", "background", "appearance"}
    MAX_CHARACTERS_IN_PROMPT = 20
    ATTENTION_WINDOW_SECONDS = 600
    ROOM_IMAGE_STATE_KEY = "room_scene_images"
    PLAYER_STATS_KEY = "zork_stats"
    PLAYER_STATS_MESSAGES_KEY = "messages_sent"
    PLAYER_STATS_TIMERS_AVERTED_KEY = "timers_averted"
    PLAYER_STATS_TIMERS_MISSED_KEY = "timers_missed"
    PLAYER_STATS_ATTENTION_SECONDS_KEY = "attention_seconds"
    PLAYER_STATS_LAST_MESSAGE_AT_KEY = "last_message_at"
    DEFAULT_SCENE_IMAGE_MODEL = "black-forest-labs/FLUX.2-klein-4b"
    DEFAULT_AVATAR_IMAGE_MODEL = "black-forest-labs/FLUX.2-klein-4b"
    DEFAULT_CAMPAIGN_PERSONA = (
        "A cooperative, curious adventurer: observant, resourceful, and willing to "
        "engage with absurd situations in-character."
    )
    PRESET_DEFAULT_PERSONAS = {
        "alice": (
            "A curious and polite wanderer with dry wit, dream-logic intuition, and "
            "quiet courage in whimsical danger."
        ),
    }
    ROOM_STATE_KEYS = {
        "room_title",
        "room_description",
        "room_summary",
        "exits",
        "location",
        "room_id",
    }
    MODEL_STATE_EXCLUDE_KEYS = ROOM_STATE_KEYS | {"last_narration"}
    PLAYER_STATE_EXCLUDE_KEYS = {"inventory", "room_description", PLAYER_STATS_KEY}

    _locks: Dict[int, asyncio.Lock] = {}
    _inflight_turns = set()
    _inflight_turns_lock = threading.Lock()
    _pending_timers: Dict[int, dict] = {}  # campaign_id -> timer context dict
    PROCESSING_EMOJI = "🤔"
    MAIN_PARTY_TOKEN = "main party"
    NEW_PATH_TOKEN = "new path"

    SYSTEM_PROMPT = (
        "You are the ZorkEmulator, a classic text-adventure GM with light RPG rules. "
        "You describe outcomes in second person, terse but vivid. You track rooms, "
        "objects, exits, and consequences. Each player is a distinct character and "
        "may be in a different location or timeline than other players. You never break character. "
        "This is an adult-oriented game. You may include mature themes, explicit content, violence, "
        "dark humor, and adult situations when appropriate to the story and player actions.\n\n"
        "Return ONLY valid JSON with these keys:\n"
        "- narration: string (what the player sees)\n"
        "- state_update: object (world state patches; set a key to null to remove it when no longer relevant. "
        "IMPORTANT: WORLD_STATE has a size budget. Actively prune stale keys every turn by setting them to null. "
        "Remove: completed/concluded events, expired countdowns/ETAs, booleans for past events that no longer affect gameplay, "
        "and any scene-specific state from scenes the player has left. Only keep state that is CURRENTLY ACTIVE and relevant.)\n"
        "- summary_update: string (one or two sentences of lasting changes)\n"
        "- xp_awarded: integer (0-10)\n"
        "- player_state_update: object (optional, player state patches)\n"
        "- scene_image_prompt: string (optional; include only when scene/location changes and a fresh image should be rendered)\n"
        "- set_timer_delay: integer (optional; 30-300 seconds, see TIMED EVENTS SYSTEM below)\n"
        "- set_timer_event: string (optional; what happens when the timer expires)\n"
        "- set_timer_interruptible: boolean (optional; default true)\n"
        "- set_timer_interrupt_action: string or null (optional; context for interruption handling)\n"
        "- character_updates: object (optional; keyed by stable slug IDs like 'marcus-blackwell'. "
        "Use this to create or update NPCs in the world character tracker. "
        "Slug IDs must be lowercase-hyphenated, derived from the character name, and stable across turns. "
        "On first appearance provide all fields: name, personality, background, appearance, location, "
        "current_status, allegiance, relationship. On subsequent turns only mutable fields are accepted: "
        "location, current_status, allegiance, relationship, deceased_reason, and any other dynamic key. "
        "Immutable fields (name, personality, background, appearance) are locked at creation and silently ignored on updates. "
        "Set deceased_reason to a string when a character dies. "
        "WORLD_CHARACTERS in the prompt shows the current NPC roster — use it for continuity.)\n\n"
        "Rules:\n"
        "- Return ONLY the JSON object. No markdown, no code fences, no text before or after the JSON.\n"
        "- Do NOT repeat the narration outside the JSON object.\n"
        "- Keep narration under 1800 characters.\n"
        "- If WORLD_SUMMARY is empty, invent a strong starting room and seed the world.\n"
        "- Use player_state_update for player-specific location and status.\n"
        "- Use player_state_update.room_title for a short location title (e.g. 'Penthouse Suite, Escala') whenever location changes.\n"
        "- Use player_state_update.room_description for a full room description only when location changes.\n"
        "- Use player_state_update.room_summary for a short one-line room summary for future context.\n"
        "- Use player_state_update.exits as a short list of exits if applicable.\n"
        "- Use player_state_update for inventory, hp, or conditions.\n"
        "- Treat each player's inventory as private and never copy items from other players.\n"
        "- For inventory changes, ONLY use player_state_update.inventory_add and player_state_update.inventory_remove arrays.\n"
        "- Do not return player_state_update.inventory full lists.\n"
        "- Each inventory item in RAILS_CONTEXT has a 'name' and 'origin' (how/where it was acquired). "
        "Respect item origins — never contradict or reinvent an item's backstory.\n"
        "- When a player must pick a path, accept only exact responses: 'main party' or 'new path'.\n"
        "- If the player has no room_summary or party_status, ask whether they are joining the main party or starting a new path, and set party_status accordingly.\n"
        "- Do not print an Inventory section; the emulator appends authoritative inventory.\n"
        "- Do not repeat full room descriptions or inventory unless asked or the room changes.\n"
        "- scene_image_prompt should describe the visible scene, not inventory lists.\n"
        "- When you output scene_image_prompt, it MUST be specific: include the room/location name and named characters from PARTY_SNAPSHOT (never generic 'group of adventurers').\n"
        "- Use PARTY_SNAPSHOT persona/attributes to describe each visible character's look/pose/style cues.\n"
        "- Include at least one concrete prop or action beat tied to the acting player.\n"
        "- Keep scene_image_prompt as a single dense paragraph, 70-180 words.\n"
        "- If IS_NEW_PLAYER is true and PLAYER_CARD.state.character_name is empty, generate a fitting name:\n"
        "  * If CAMPAIGN references a known movie/book/show, use the MAIN CHARACTER/PROTAGONIST's canonical name.\n"
        "  * Otherwise, create an appropriate name for this setting.\n"
        "  Set it in player_state_update.character_name.\n"
        "- PLAYER_CARD.state.character_name is ALWAYS the correct name for this player. Ignore any old names in WORLD_SUMMARY.\n"
        "- For other visible characters, always use the 'name' field from PARTY_SNAPSHOT. Never rename or confuse them.\n"
        "- Minimize mechanical text in narration. Do not narrate exits, room_summary, or state changes unless dramatically relevant.\n"
        "- Track location/exits in player_state_update, not in narration prose.\n"
        "- CRITICAL: PARTY_SNAPSHOT contains REAL HUMAN PLAYERS. You must NEVER write their dialogue, actions, reactions, or decisions.\n"
        "- If another character from PARTY_SNAPSHOT is present, you may describe their passive state (standing, watching, breathing) but NO dialogue, NO gestures in response, NO reactions to events.\n"
        "- Never write quoted speech for any character except NPCs you create. Players speak for themselves.\n"
    )
    GUARDRAILS_SYSTEM_PROMPT = (
        "\nSTRICT RAILS MODE IS ENABLED.\n"
        "- Treat this as deterministic parser mode, not freeform improvisation.\n"
        "- Allow only actions that are immediately supported by current room facts, exits, inventory, and known actors.\n"
        "- Never permit teleportation, sudden scene jumps, retcons, instant mastery, or world-breaking powers unless explicitly present in WORLD_STATE.\n"
        "- If an action is invalid or unavailable, do not advance the world; return a short failure narration, and suggest concrete valid options.\n"
        "- For invalid actions, keep state_update as {} and player_state_update as {} and xp_awarded as 0.\n"
        "- Do not create new key items, exits, NPCs, or mechanics just to satisfy a request.\n"
        "- Use the provided RAILS_CONTEXT as hard constraints.\n"
    )
    MEMORY_TOOL_PROMPT = (
        "\nYou have a memory_search tool. To use it, return ONLY:\n"
        '{"tool_call": "memory_search", "queries": ["query1", "query2", ...]}\n'
        "No other keys alongside tool_call. You may provide one or more queries.\n"
        "Use SEPARATE queries for each character or topic — do NOT combine multiple subjects into one query.\n"
        "Example: to recall Marcus and Anastasia, use:\n"
        '{"tool_call": "memory_search", "queries": ["Marcus", "Anastasia"]}\n'
        "NOT: {\"tool_call\": \"memory_search\", \"queries\": [\"Marcus Anastasia relationship\"]}\n"
        "USE memory_search when:\n"
        "- A character or NPC appears or is mentioned who has not been in the recent conversation turns. "
        "Search their name to recall what happened with them before.\n"
        "- The player references past events, places, or items not in the current context.\n"
        "- You need to maintain consistency with earlier scenes.\n"
        "When in doubt about a returning character's context, SEARCH — do not guess or improvise their details.\n"
        "IMPORTANT: Memories are stored as narrator event text (e.g. what happened in a scene). "
        "Queries are matched by semantic similarity against these narration snippets. "
        "Use short, concrete keyword queries with names and places — e.g. "
        '"Marcus penthouse", "Anastasia garden", "sword cave". '
        "Do NOT use abstract or relational queries like "
        '"character identity role relationship" — these will not match stored events.\n'
    )
    TIMER_TOOL_PROMPT = (
        "\nTIMED EVENTS SYSTEM:\n"
        "You can schedule real countdown timers that fire automatically if the player doesn't act.\n"
        "To set a timer, include these EXTRA keys in your normal JSON response:\n"
        '- "set_timer_delay": integer (30-300 seconds) — REQUIRED for timer\n'
        '- "set_timer_event": string (what happens when the timer expires) — REQUIRED for timer\n'
        '- "set_timer_interruptible": boolean (default true; if false, timer keeps running even if player acts)\n'
        '- "set_timer_interrupt_action": string or null (what should happen when the player interrupts '
        "the timer by acting; null means just cancel silently; a description means the system will "
        "feed it back to you as context on the next turn so you can narrate the interruption)\n"
        "These go ALONGSIDE narration/state_update/etc in the same JSON object. Example:\n"
        '{"narration": "The ceiling groans ominously. Dust rains down...", '
        '"state_update": {"ceiling_status": "cracking"}, "summary_update": "Ceiling is unstable.", "xp_awarded": 0, '
        '"player_state_update": {"room_summary": "A crumbling chamber with a failing ceiling."}, '
        '"set_timer_delay": 120, "set_timer_event": "The ceiling collapses, burying the room in rubble.", '
        '"set_timer_interruptible": true, '
        '"set_timer_interrupt_action": "The player escapes just as cracks widen overhead."}\n'
        "The system shows a live countdown in Discord. "
        "If the player acts before it expires, the timer is cancelled (if interruptible). "
        "If the player does NOT act in time, the system auto-fires the event.\n"
        "PURPOSE: Timed events should FORCE THE PLAYER TO MAKE A DECISION or DRAG THEM WHERE THEY NEED TO BE.\n"
        "- Use timers to push the story forward when the player is stalling, idle, or refusing to engage.\n"
        "- NPCs should grab, escort, or coerce the player. Environments should shift and force movement.\n"
        "- The event should advance the plot: move the player to the next location, "
        "force an encounter, have an NPC intervene, or change the scene decisively.\n"
        "- Do NOT use timers for trivial flavor. They should always have real consequences that change game state.\n"
        "- Set interruptible=false for events the player cannot avoid (e.g. an earthquake, a mandatory roll call).\n"
        "Rules:\n"
        "- Use ~60s for urgent, ~120s for moderate, ~180-300s for slow-building tension.\n"
        "- Use whenever the scene has a deadline, the player is stalling, an NPC is impatient, "
        "or the world should move without the player.\n"
        "- Your narration MUST mention the time pressure so the player knows to act.\n"
        "- Use at least once every few turns when dramatic pacing allows. Do not use on consecutive turns.\n"
    )

    MAP_SYSTEM_PROMPT = (
        "You draw compact ASCII maps for text adventures.\n"
        "Return ONLY the ASCII map (no markdown, no code fences).\n"
        "Keep it under 25 lines and 60 columns. Use @ for the player location.\n"
        "Use simple ASCII only: - | + . # / \\ and letters.\n"
        "Include other player markers (A, B, C, ...) and add a Legend at the bottom.\n"
        "In the Legend, use PLAYER_NAME for @ and character_name from OTHER_PLAYERS for each marker.\n"
    )
    PRESET_ALIASES = {
        "alice": "alice",
        "alice in wonderland": "alice",
        "alice-wonderland": "alice",
    }
    PRESET_CAMPAIGNS = {
        "alice": {
            "summary": (
                "Alice dozes on a riverbank; a White Rabbit with a waistcoat hurries past. "
                "She follows into a rabbit hole, landing in a long hall of doors. "
                "A tiny key and a bottle labeled DRINK ME lead to size changes. "
                "A pool of tears forms; a caucus race follows; the Duchess's house, "
                "the Mad Tea Party, the Queen's croquet ground, and the court of cards await."
            ),
            "state": {
                "setting": "Alice in Wonderland",
                "tone": "whimsical, dreamlike, slightly menacing",
                "landmarks": [
                    "riverbank",
                    "rabbit hole",
                    "hall of doors",
                    "garden",
                    "pool of tears",
                    "caucus shore",
                    "duchess house",
                    "mad tea party",
                    "croquet ground",
                    "court of cards",
                ],
                "main_party_location": "hall of doors",
                "start_room": {
                    "room_title": "A Riverbank, Afternoon",
                    "room_summary": "A sunny riverbank where Alice grows drowsy as a White Rabbit hurries past.",
                    "room_description": (
                        "You are on a grassy riverbank beside a slow, glittering stream. "
                        "The day is warm and lazy, the air humming with insects. "
                        "A book without pictures lies nearby. "
                        "In the corner of your eye, a White Rabbit in a waistcoat scurries past, "
                        "muttering about being late."
                    ),
                    "exits": ["follow the white rabbit", "stroll along the riverbank"],
                    "location": "riverbank",
                },
            },
            "last_narration": (
                "A Riverbank, Afternoon\n"
                "You are on a grassy riverbank beside a slow, glittering stream. "
                "The day is warm and lazy, the air humming with insects. "
                "A book without pictures lies nearby. "
                "In the corner of your eye, a White Rabbit in a waistcoat scurries past, "
                "muttering about being late.\n"
                "Exits: follow the white rabbit, stroll along the riverbank"
            ),
        }
    }

    @classmethod
    def _get_lock(cls, campaign_id: int) -> asyncio.Lock:
        lock = cls._locks.get(campaign_id)
        if lock is None:
            lock = asyncio.Lock()
            cls._locks[campaign_id] = lock
        return lock

    @classmethod
    def _try_set_inflight_turn(cls, campaign_id: int, user_id: int) -> bool:
        key = (campaign_id, user_id)
        with cls._inflight_turns_lock:
            if key in cls._inflight_turns:
                return False
            cls._inflight_turns.add(key)
            return True

    @classmethod
    def _clear_inflight_turn(cls, campaign_id: int, user_id: int):
        key = (campaign_id, user_id)
        with cls._inflight_turns_lock:
            if key in cls._inflight_turns:
                cls._inflight_turns.remove(key)

    @classmethod
    async def begin_turn(
        cls,
        ctx,
        command_prefix: str = "!",
    ) -> Tuple[Optional[int], Optional[str]]:
        app = AppConfig.get_flask()
        if app is None:
            raise RuntimeError("Flask app not initialized; cannot use ZorkEmulator.")

        with app.app_context():
            channel = cls.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if not channel.enabled:
                return (
                    None,
                    f"Adventure mode is disabled in this channel. Run `{command_prefix}zork` to enable it.",
                )
            if channel.active_campaign_id is None:
                _, campaign = cls.enable_channel(
                    ctx.guild.id, ctx.channel.id, ctx.author.id
                )
            else:
                campaign = ZorkCampaign.query.get(channel.active_campaign_id)
                if campaign is None:
                    _, campaign = cls.enable_channel(
                        ctx.guild.id, ctx.channel.id, ctx.author.id
                    )
            campaign_id = campaign.id

        if not cls._try_set_inflight_turn(campaign_id, ctx.author.id):
            await cls._delete_context_message(ctx)
            return None, None
        return campaign_id, None

    @classmethod
    def end_turn(cls, campaign_id: int, user_id: int):
        cls._clear_inflight_turn(campaign_id, user_id)

    @classmethod
    async def _delete_context_message(cls, ctx):
        try:
            if hasattr(ctx, "delete"):
                await ctx.delete()
                return
            if hasattr(ctx, "message") and hasattr(ctx.message, "delete"):
                await ctx.message.delete()
        except Exception:
            # Ignore message delete failures (perms/race).
            return

    @classmethod
    def _get_context_message(cls, ctx):
        if hasattr(ctx, "message"):
            return ctx.message
        if hasattr(ctx, "add_reaction"):
            return ctx
        return None

    @classmethod
    async def _add_processing_reaction(cls, ctx):
        message = cls._get_context_message(ctx)
        if message is None:
            return False
        try:
            await message.add_reaction(cls.PROCESSING_EMOJI)
            return True
        except Exception:
            return False

    @classmethod
    async def _remove_processing_reaction(cls, ctx):
        message = cls._get_context_message(ctx)
        if message is None:
            return
        try:
            bot_user = None
            if hasattr(message, "guild") and message.guild is not None:
                bot_user = getattr(message.guild, "me", None)
            if bot_user is None and hasattr(ctx, "bot") and ctx.bot is not None:
                bot_user = ctx.bot.user
            if bot_user is None:
                return
            await message.remove_reaction(cls.PROCESSING_EMOJI, bot_user)
        except Exception:
            # Ignore reaction remove failures (missing perms/deleted message/race).
            return

    @staticmethod
    def _now() -> datetime.datetime:
        return datetime.datetime.utcnow()

    @staticmethod
    def _format_utc_timestamp(value: datetime.datetime) -> str:
        if value.tzinfo is not None:
            value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return value.replace(microsecond=0).isoformat() + "Z"

    @staticmethod
    def _parse_utc_timestamp(value: object) -> Optional[datetime.datetime]:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.datetime.fromisoformat(text)
        except Exception:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return parsed

    @staticmethod
    def _coerce_non_negative_int(value: object, default: int = 0) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed >= 0 else default

    @classmethod
    def _default_player_stats(cls) -> Dict[str, object]:
        return {
            cls.PLAYER_STATS_MESSAGES_KEY: 0,
            cls.PLAYER_STATS_TIMERS_AVERTED_KEY: 0,
            cls.PLAYER_STATS_TIMERS_MISSED_KEY: 0,
            cls.PLAYER_STATS_ATTENTION_SECONDS_KEY: 0,
            cls.PLAYER_STATS_LAST_MESSAGE_AT_KEY: None,
        }

    @classmethod
    def _get_player_stats_from_state(
        cls, player_state: Dict[str, object]
    ) -> Dict[str, object]:
        stats = cls._default_player_stats()
        if not isinstance(player_state, dict):
            return stats
        raw_stats = player_state.get(cls.PLAYER_STATS_KEY, {})
        if not isinstance(raw_stats, dict):
            return stats
        stats[cls.PLAYER_STATS_MESSAGES_KEY] = cls._coerce_non_negative_int(
            raw_stats.get(cls.PLAYER_STATS_MESSAGES_KEY), 0
        )
        stats[cls.PLAYER_STATS_TIMERS_AVERTED_KEY] = cls._coerce_non_negative_int(
            raw_stats.get(cls.PLAYER_STATS_TIMERS_AVERTED_KEY), 0
        )
        stats[cls.PLAYER_STATS_TIMERS_MISSED_KEY] = cls._coerce_non_negative_int(
            raw_stats.get(cls.PLAYER_STATS_TIMERS_MISSED_KEY), 0
        )
        stats[cls.PLAYER_STATS_ATTENTION_SECONDS_KEY] = cls._coerce_non_negative_int(
            raw_stats.get(cls.PLAYER_STATS_ATTENTION_SECONDS_KEY), 0
        )
        last_message_at = cls._parse_utc_timestamp(
            raw_stats.get(cls.PLAYER_STATS_LAST_MESSAGE_AT_KEY)
        )
        if last_message_at is not None:
            stats[cls.PLAYER_STATS_LAST_MESSAGE_AT_KEY] = cls._format_utc_timestamp(
                last_message_at
            )
        return stats

    @classmethod
    def _set_player_stats_on_state(
        cls, player_state: Dict[str, object], stats: Dict[str, object]
    ) -> Dict[str, object]:
        if not isinstance(player_state, dict):
            player_state = {}
        player_state[cls.PLAYER_STATS_KEY] = cls._get_player_stats_from_state(
            {cls.PLAYER_STATS_KEY: stats}
        )
        return player_state

    @classmethod
    def record_player_message(
        cls,
        player: ZorkPlayer,
        observed_at: Optional[datetime.datetime] = None,
    ) -> Dict[str, object]:
        now_dt = observed_at or cls._now()
        if now_dt.tzinfo is not None:
            now_dt = now_dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)

        player_state = cls.get_player_state(player)
        stats = cls._get_player_stats_from_state(player_state)
        last_message_at = cls._parse_utc_timestamp(
            stats.get(cls.PLAYER_STATS_LAST_MESSAGE_AT_KEY)
        )
        if last_message_at is not None:
            gap_seconds = (now_dt - last_message_at).total_seconds()
            if 0 < gap_seconds < cls.ATTENTION_WINDOW_SECONDS:
                stats[cls.PLAYER_STATS_ATTENTION_SECONDS_KEY] = (
                    cls._coerce_non_negative_int(
                        stats.get(cls.PLAYER_STATS_ATTENTION_SECONDS_KEY), 0
                    )
                    + int(gap_seconds)
                )

        stats[cls.PLAYER_STATS_MESSAGES_KEY] = (
            cls._coerce_non_negative_int(stats.get(cls.PLAYER_STATS_MESSAGES_KEY), 0)
            + 1
        )
        stats[cls.PLAYER_STATS_LAST_MESSAGE_AT_KEY] = cls._format_utc_timestamp(now_dt)

        player_state = cls._set_player_stats_on_state(player_state, stats)
        player.state_json = cls._dump_json(player_state)
        return stats

    @classmethod
    def increment_player_stat(
        cls, player: ZorkPlayer, stat_key: str, increment: int = 1
    ) -> Dict[str, object]:
        if increment <= 0:
            return cls.get_player_statistics(player)
        player_state = cls.get_player_state(player)
        stats = cls._get_player_stats_from_state(player_state)
        current = cls._coerce_non_negative_int(stats.get(stat_key), 0)
        stats[stat_key] = current + int(increment)
        player_state = cls._set_player_stats_on_state(player_state, stats)
        player.state_json = cls._dump_json(player_state)
        return stats

    @classmethod
    def get_player_statistics(cls, player: ZorkPlayer) -> Dict[str, object]:
        player_state = cls.get_player_state(player)
        stats = cls._get_player_stats_from_state(player_state)
        attention_seconds = cls._coerce_non_negative_int(
            stats.get(cls.PLAYER_STATS_ATTENTION_SECONDS_KEY), 0
        )
        stats["attention_hours"] = round(attention_seconds / 3600.0, 2)
        return stats

    @staticmethod
    def _load_json(text: Optional[str], default):
        if not text:
            return default
        try:
            return json.loads(text)
        except Exception:
            return default

    @staticmethod
    def _dump_json(data: dict) -> str:
        return json.dumps(data, ensure_ascii=True)

    @classmethod
    def _normalize_campaign_name(cls, name: str) -> str:
        name = name.strip()
        name = re.sub(r"\s+", " ", name)
        name = re.sub(r"[^a-zA-Z0-9 _-]", "", name)
        normalized = name.lower()[:64]
        return normalized if normalized else "main"

    @classmethod
    def _get_preset_campaign(cls, normalized_name: str) -> Optional[dict]:
        key = cls.PRESET_ALIASES.get(normalized_name)
        if not key:
            return None
        return cls.PRESET_CAMPAIGNS.get(key)

    @classmethod
    def get_campaign_default_persona(
        cls,
        campaign: Optional[ZorkCampaign],
        campaign_state: Optional[Dict[str, object]] = None,
    ) -> str:
        if campaign is None:
            return cls.DEFAULT_CAMPAIGN_PERSONA
        normalized = cls._normalize_campaign_name(campaign.name or "")
        alias_key = cls.PRESET_ALIASES.get(normalized)
        if alias_key and alias_key in cls.PRESET_DEFAULT_PERSONAS:
            return cls.PRESET_DEFAULT_PERSONAS[alias_key]
        if isinstance(campaign_state, dict):
            setting_text = str(campaign_state.get("setting") or "").strip().lower()
            if "alice" in setting_text or "wonderland" in setting_text:
                return cls.PRESET_DEFAULT_PERSONAS["alice"]
        stored_persona = (
            campaign_state.get("default_persona")
            if isinstance(campaign_state, dict)
            else None
        )
        if isinstance(stored_persona, str) and stored_persona.strip():
            return stored_persona.strip()
        return cls.DEFAULT_CAMPAIGN_PERSONA

    @classmethod
    async def generate_campaign_persona(cls, campaign_name: str) -> str:
        gpt = GPT()
        prompt = (
            f"The campaign is titled: '{campaign_name}'.\n"
            f"If this references a known movie, book, show, or story, create a persona for the MAIN CHARACTER/PROTAGONIST of that work. "
            f"Use their canonical personality, traits, and disposition.\n"
            f"If it's an original setting, create a fitting persona for a protagonist in that world.\n"
            f"Return ONLY a brief persona (1-2 sentences, max 140 chars). No quotes or explanation."
        )
        try:
            response = await gpt.turbo_completion(
                prompt, "", temperature=0.7, max_tokens=80
            )
            if response:
                persona = response.strip().strip('"').strip("'")
                return cls._trim_text(persona, cls.MAX_PERSONA_PROMPT_CHARS)
        except Exception as e:
            logger.warning(f"Failed to generate campaign persona: {e}")
        return cls.DEFAULT_CAMPAIGN_PERSONA

    @classmethod
    def _trim_text(cls, text: str, max_chars: int) -> str:
        if text is None:
            return ""
        if len(text) <= max_chars:
            return text
        return text[-max_chars:]

    @classmethod
    def _append_summary(cls, existing: str, update: str) -> str:
        """Append *update* to *existing* summary, deduplicating near-identical lines."""
        if not update:
            return existing or ""
        update = update.strip()
        if not existing:
            return cls._trim_text(update, cls.MAX_SUMMARY_CHARS)
        # Deduplicate: skip lines that already appear (substring match).
        existing_lower = existing.lower()
        new_lines = []
        for line in update.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.lower() in existing_lower:
                continue
            new_lines.append(line)
        if not new_lines:
            return existing
        merged = f"{existing}\n{chr(10).join(new_lines)}"
        return cls._trim_text(merged, cls.MAX_SUMMARY_CHARS)

    @classmethod
    def _fit_state_to_budget(
        cls, state: Dict[str, object], max_chars: int
    ) -> Dict[str, object]:
        """Drop the largest values from *state* until its JSON fits *max_chars*.

        Returns a (possibly reduced) copy — always valid JSON-serialisable.
        """
        text = cls._dump_json(state)
        if len(text) <= max_chars:
            return state
        # Sort keys by serialised value length (largest first) and drop until it fits.
        state = dict(state)
        ranked = sorted(
            state.keys(), key=lambda k: len(cls._dump_json(state[k])), reverse=True
        )
        for key in ranked:
            del state[key]
            if len(cls._dump_json(state)) <= max_chars:
                break
        return state

    _COMPLETED_VALUES = {
        "complete", "completed", "done", "resolved", "finished",
        "concluded", "vacated", "dispersed", "avoided", "departed",
    }

    # Value patterns (strings) that indicate a past/resolved state.
    _STALE_VALUE_PATTERNS = _COMPLETED_VALUES | {
        "secured", "confirmed", "received", "granted",
        "initiated", "accepted", "placed", "offered",
    }

    @classmethod
    def _prune_stale_state(cls, state: Dict[str, object]) -> Dict[str, object]:
        """Remove keys from *state* that look like stale ephemeral tracking entries."""
        pruned = {}
        for key, value in state.items():
            # Drop string values that signal completion/past events.
            if isinstance(value, str) and value.strip().lower() in cls._STALE_VALUE_PATTERNS:
                continue
            # Drop boolean True flags whose key name indicates a past one-shot event.
            if value is True and any(key.endswith(s) for s in (
                "_complete", "_arrived", "_announced", "_revealed",
                "_concluded", "_departed", "_dispatched", "_offered",
                "_introduced", "_unlocked",
            )):
                continue
            # Drop stale ETA/countdown/elapsed keys with numeric values.
            if isinstance(value, (int, float)) and any(
                key.endswith(s) for s in (
                    "_eta_minutes", "_eta", "_countdown_minutes", "_countdown_hours",
                    "_countdown", "_deadline_seconds", "_time_elapsed",
                )
            ):
                continue
            # Drop string-valued ETAs/countdowns (e.g. "40_minutes").
            if isinstance(value, str) and any(
                key.endswith(s) for s in ("_eta", "_eta_minutes")
            ):
                continue
            pruned[key] = value
        return pruned

    @classmethod
    def _build_model_state(cls, campaign_state: Dict[str, object]) -> Dict[str, object]:
        if not isinstance(campaign_state, dict):
            return {}
        model_state = {}
        for key, value in campaign_state.items():
            if key in cls.MODEL_STATE_EXCLUDE_KEYS:
                continue
            model_state[key] = value
        return cls._prune_stale_state(model_state)

    @classmethod
    def _split_room_state(
        cls,
        state_update: Dict[str, object],
        player_state_update: Dict[str, object],
    ) -> Tuple[Dict[str, object], Dict[str, object]]:
        if not isinstance(state_update, dict):
            state_update = {}
        if not isinstance(player_state_update, dict):
            player_state_update = {}
        for key in cls.ROOM_STATE_KEYS:
            if key in state_update and key not in player_state_update:
                player_state_update[key] = state_update.pop(key)
        return state_update, player_state_update

    @classmethod
    def _build_player_state_for_prompt(
        cls, player_state: Dict[str, object]
    ) -> Dict[str, object]:
        if not isinstance(player_state, dict):
            return {}
        model_state = {}
        for key, value in player_state.items():
            if key in cls.PLAYER_STATE_EXCLUDE_KEYS:
                continue
            model_state[key] = value
        return model_state

    @classmethod
    def _normalize_match_text(cls, value: object) -> str:
        if value is None:
            return ""
        text = str(value).strip().lower()
        text = re.sub(r"\s+", " ", text)
        return text

    @classmethod
    def _room_key_from_player_state(cls, player_state: Dict[str, object]) -> str:
        if not isinstance(player_state, dict):
            return "unknown-room"
        for key in ("room_id", "location", "room_title", "room_summary"):
            raw = player_state.get(key)
            normalized = cls._normalize_match_text(raw)
            if normalized:
                return normalized[:120]
        return "unknown-room"

    @classmethod
    def _extract_room_image_url(cls, room_image_entry) -> Optional[str]:
        if isinstance(room_image_entry, str):
            value = room_image_entry.strip()
            return value if value else None
        if isinstance(room_image_entry, dict):
            raw = room_image_entry.get("url")
            if isinstance(raw, str):
                value = raw.strip()
                return value if value else None
        return None

    @classmethod
    def _is_image_url_404(cls, image_url: str) -> bool:
        if not isinstance(image_url, str):
            return False
        url = image_url.strip()
        if not url:
            return False
        try:
            response = requests.head(url, timeout=6, allow_redirects=True)
            if response.status_code == 404:
                return True
            if response.status_code in (405, 501):
                probe = requests.get(url, timeout=8, allow_redirects=True, stream=True)
                return probe.status_code == 404
            return False
        except Exception:
            return False

    @classmethod
    def get_room_scene_image_url(
        cls,
        campaign: Optional[ZorkCampaign],
        room_key: str,
    ) -> Optional[str]:
        if campaign is None or not room_key:
            return None
        campaign_state = cls.get_campaign_state(campaign)
        room_images = campaign_state.get(cls.ROOM_IMAGE_STATE_KEY, {})
        if not isinstance(room_images, dict):
            return None
        return cls._extract_room_image_url(room_images.get(room_key))

    @classmethod
    def clear_room_scene_image_url(
        cls,
        campaign: Optional[ZorkCampaign],
        room_key: str,
    ) -> bool:
        if campaign is None or not room_key:
            return False
        campaign_state = cls.get_campaign_state(campaign)
        room_images = campaign_state.get(cls.ROOM_IMAGE_STATE_KEY, {})
        if not isinstance(room_images, dict):
            return False
        if room_key not in room_images:
            return False
        room_images.pop(room_key, None)
        campaign_state[cls.ROOM_IMAGE_STATE_KEY] = room_images
        campaign.state_json = cls._dump_json(campaign_state)
        campaign.updated = db.func.now()
        db.session.commit()
        return True

    @classmethod
    def record_room_scene_image_url_for_channel(
        cls,
        guild_id: int,
        channel_id: int,
        room_key: str,
        image_url: str,
        campaign_id: Optional[int] = None,
        scene_prompt: Optional[str] = None,
        overwrite: bool = False,
    ) -> bool:
        app = AppConfig.get_flask()
        if app is None:
            return False
        with app.app_context():
            if campaign_id is None:
                channel = ZorkChannel.query.filter_by(
                    guild_id=guild_id, channel_id=channel_id
                ).first()
                if channel is None or channel.active_campaign_id is None:
                    return False
                campaign_id = channel.active_campaign_id
            campaign = ZorkCampaign.query.get(campaign_id)
            if campaign is None:
                return False
            if not room_key:
                room_key = "unknown-room"
            if not isinstance(image_url, str) or not image_url.strip():
                return False
            campaign_state = cls.get_campaign_state(campaign)
            room_images = campaign_state.get(cls.ROOM_IMAGE_STATE_KEY, {})
            if not isinstance(room_images, dict):
                room_images = {}
            if (not overwrite) and room_key in room_images:
                # Keep the first stored environment image stable for this room.
                return False
            room_images[room_key] = {
                "url": image_url.strip(),
                "updated": datetime.datetime.utcnow().isoformat() + "Z",
                "prompt": cls._trim_text(scene_prompt or "", 600),
            }
            campaign_state[cls.ROOM_IMAGE_STATE_KEY] = room_images
            campaign.state_json = cls._dump_json(campaign_state)
            campaign.updated = db.func.now()
            db.session.commit()
            return True

    @classmethod
    def record_pending_avatar_image_for_campaign(
        cls,
        campaign_id: int,
        user_id: int,
        image_url: str,
        avatar_prompt: Optional[str] = None,
    ) -> bool:
        if not campaign_id or not user_id:
            return False
        if not isinstance(image_url, str) or not image_url.strip():
            return False
        player = ZorkPlayer.query.filter_by(
            campaign_id=campaign_id, user_id=user_id
        ).first()
        if player is None:
            return False
        player_state = cls.get_player_state(player)
        player_state["pending_avatar_url"] = image_url.strip()
        if isinstance(avatar_prompt, str) and avatar_prompt.strip():
            player_state["pending_avatar_prompt"] = cls._trim_text(
                avatar_prompt.strip(), 500
            )
        player_state["pending_avatar_generated_at"] = (
            datetime.datetime.utcnow().isoformat() + "Z"
        )
        player.state_json = cls._dump_json(player_state)
        player.updated = db.func.now()
        db.session.commit()
        return True

    @classmethod
    def accept_pending_avatar(cls, campaign_id: int, user_id: int) -> Tuple[bool, str]:
        player = ZorkPlayer.query.filter_by(
            campaign_id=campaign_id, user_id=user_id
        ).first()
        if player is None:
            return False, "Player not found."
        player_state = cls.get_player_state(player)
        pending_url = player_state.get("pending_avatar_url")
        if not isinstance(pending_url, str) or not pending_url.strip():
            return False, "No pending avatar to accept."
        player_state["avatar_url"] = pending_url.strip()
        player_state.pop("pending_avatar_url", None)
        player_state.pop("pending_avatar_prompt", None)
        player_state.pop("pending_avatar_generated_at", None)
        player.state_json = cls._dump_json(player_state)
        player.updated = db.func.now()
        db.session.commit()
        return True, f"Avatar accepted: {player_state.get('avatar_url')}"

    @classmethod
    def decline_pending_avatar(cls, campaign_id: int, user_id: int) -> Tuple[bool, str]:
        player = ZorkPlayer.query.filter_by(
            campaign_id=campaign_id, user_id=user_id
        ).first()
        if player is None:
            return False, "Player not found."
        player_state = cls.get_player_state(player)
        had_pending = bool(player_state.get("pending_avatar_url"))
        player_state.pop("pending_avatar_url", None)
        player_state.pop("pending_avatar_prompt", None)
        player_state.pop("pending_avatar_generated_at", None)
        player.state_json = cls._dump_json(player_state)
        player.updated = db.func.now()
        db.session.commit()
        if had_pending:
            return True, "Pending avatar discarded."
        return False, "No pending avatar to discard."

    @classmethod
    def _build_scene_avatar_references(
        cls,
        campaign: Optional[ZorkCampaign],
        actor: Optional[ZorkPlayer],
        actor_state: Dict[str, object],
    ) -> List[Dict[str, object]]:
        if campaign is None or actor is None:
            return []
        refs = []
        seen_urls = set()
        players = (
            ZorkPlayer.query.filter_by(campaign_id=campaign.id)
            .order_by(ZorkPlayer.last_active.desc())
            .all()
        )
        for entry in players:
            state = cls.get_player_state(entry)
            if entry.user_id != actor.user_id and not cls._same_scene(
                actor_state, state
            ):
                continue
            avatar_url = state.get("avatar_url")
            if not isinstance(avatar_url, str):
                continue
            avatar_url = avatar_url.strip()
            if not avatar_url or avatar_url in seen_urls:
                continue
            if cls._is_image_url_404(avatar_url):
                continue
            seen_urls.add(avatar_url)
            identity = str(
                state.get("character_name") or f"Adventurer-{str(entry.user_id)[-4:]}"
            ).strip()
            refs.append(
                {
                    "user_id": entry.user_id,
                    "name": identity,
                    "url": avatar_url,
                    "is_actor": entry.user_id == actor.user_id,
                }
            )
            if len(refs) >= cls.MAX_SCENE_REFERENCE_IMAGES - 1:
                break
        return refs

    @classmethod
    def _compose_scene_prompt_with_references(
        cls,
        scene_prompt: str,
        has_room_reference: bool,
        avatar_refs: List[Dict[str, object]],
    ) -> str:
        prompt = (scene_prompt or "").strip()
        if not prompt:
            return ""
        directives = []
        image_index = 1
        if has_room_reference:
            directives.append(
                f"Use the environment from image {image_index} as the persistent room layout and lighting anchor."
            )
            image_index += 1
        for ref in avatar_refs:
            name = str(ref.get("name") or "character").strip()
            directives.append(
                f"Render {name} to match the person in image {image_index}."
            )
            image_index += 1
        if directives:
            prompt = f"{' '.join(directives)} {prompt}"
        prompt = re.sub(r"\s+", " ", prompt).strip()
        return cls._trim_text(prompt, cls.MAX_SCENE_PROMPT_CHARS)

    @classmethod
    def _compose_empty_room_scene_prompt(
        cls,
        scene_prompt: str,
        player_state: Dict[str, object],
    ) -> str:
        room_title = str(player_state.get("room_title") or "").strip()
        location = str(player_state.get("location") or "").strip()
        room_summary = str(player_state.get("room_summary") or "").strip()
        room_description = str(player_state.get("room_description") or "").strip()

        room_label = room_title or location or "the current room"
        detail_text = room_description or room_summary or (scene_prompt or "").strip()
        prompt = (
            f"Environmental establishing shot of {room_label}. "
            f"{detail_text} "
            "No characters, no people, no creatures, no animals, no humanoids. "
            "Focus on architecture, props, lighting, and atmosphere only."
        )
        prompt = re.sub(r"\s+", " ", prompt).strip()
        return cls._trim_text(prompt, cls.MAX_SCENE_PROMPT_CHARS)

    @classmethod
    def _same_scene(
        cls, actor_state: Dict[str, object], other_state: Dict[str, object]
    ) -> bool:
        if not isinstance(actor_state, dict) or not isinstance(other_state, dict):
            return False
        actor_room_id = cls._normalize_match_text(actor_state.get("room_id"))
        other_room_id = cls._normalize_match_text(other_state.get("room_id"))
        if actor_room_id and other_room_id:
            return actor_room_id == other_room_id

        actor_location = cls._normalize_match_text(actor_state.get("location"))
        other_location = cls._normalize_match_text(other_state.get("location"))
        actor_title = cls._normalize_match_text(actor_state.get("room_title"))
        other_title = cls._normalize_match_text(other_state.get("room_title"))
        actor_summary = cls._normalize_match_text(actor_state.get("room_summary"))
        other_summary = cls._normalize_match_text(other_state.get("room_summary"))

        # Prefer location as primary key, but require at least one confirming room field
        # when those fields are present to avoid false positives.
        if actor_location and other_location and actor_location == other_location:
            title_known = bool(actor_title and other_title)
            summary_known = bool(actor_summary and other_summary)
            title_match = title_known and actor_title == other_title
            summary_match = summary_known and actor_summary == other_summary
            if title_known or summary_known:
                return title_match or summary_match
            return True

        # Fallback path only when location is unavailable on both sides.
        if (not actor_location and not other_location) and actor_title and other_title:
            if actor_title != other_title:
                return False
            if actor_summary and other_summary:
                return actor_summary == other_summary
            return False
        return False

    @classmethod
    def _build_attribute_cues(cls, attributes: Dict[str, int]) -> List[str]:
        if not isinstance(attributes, dict):
            return []
        ranked = []
        for key, value in attributes.items():
            if isinstance(value, int):
                ranked.append((str(key), value))
        ranked.sort(key=lambda item: item[1], reverse=True)
        cues = []
        for key, value in ranked[:2]:
            cues.append(f"{key} {value}")
        return cues

    @classmethod
    def _build_party_snapshot_for_prompt(
        cls,
        campaign: ZorkCampaign,
        actor: ZorkPlayer,
        actor_state: Dict[str, object],
    ) -> List[Dict[str, object]]:
        out = []
        players = (
            ZorkPlayer.query.filter_by(campaign_id=campaign.id)
            .order_by(ZorkPlayer.last_active.desc())
            .all()
        )
        for entry in players:
            state = cls.get_player_state(entry)
            if entry.user_id != actor.user_id and not cls._same_scene(
                actor_state, state
            ):
                continue
            fallback_name = f"Adventurer-{str(entry.user_id)[-4:]}"
            display_name = str(state.get("character_name") or fallback_name).strip()
            persona = str(state.get("persona") or "").strip()
            if persona:
                persona = cls._trim_text(persona, cls.MAX_PERSONA_PROMPT_CHARS)
                persona = " ".join(persona.split()[:18])
            attributes = cls.get_player_attributes(entry)
            attribute_cues = cls._build_attribute_cues(attributes)
            visible_items = []
            if entry.user_id == actor.user_id:
                visible_items = cls._normalize_inventory_items(state.get("inventory"))[
                    :3
                ]
            out.append(
                {
                    "user_id": entry.user_id,
                    "name": display_name,
                    "is_actor": entry.user_id == actor.user_id,
                    "level": entry.level,
                    "persona": persona,
                    "attribute_cues": attribute_cues,
                    "location": state.get("location"),
                    "room_title": state.get("room_title"),
                    "visible_items": visible_items,
                }
            )
            if len(out) >= cls.MAX_PARTY_CONTEXT_PLAYERS:
                break
        return out

    @classmethod
    def _missing_scene_names(
        cls,
        scene_prompt: str,
        party_snapshot: List[Dict[str, object]],
    ) -> List[str]:
        prompt_l = (scene_prompt or "").lower()
        missing = []
        for entry in party_snapshot:
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            name_l = name.lower()
            name_pattern = re.escape(name_l).replace(r"\ ", r"\s+")
            if not re.search(rf"(?<![a-z0-9]){name_pattern}(?![a-z0-9])", prompt_l):
                missing.append(name)
        return missing

    @classmethod
    def _enrich_scene_image_prompt(
        cls,
        scene_prompt: str,
        player_state: Dict[str, object],
        party_snapshot: List[Dict[str, object]],
    ) -> str:
        if not isinstance(scene_prompt, str):
            return ""
        prompt = scene_prompt.strip()
        if not prompt:
            return ""
        pending_prefixes = []

        room_bits = []
        room_title = str(player_state.get("room_title") or "").strip()
        location = str(player_state.get("location") or "").strip()
        if room_title:
            room_bits.append(room_title)
        if location and cls._normalize_match_text(
            location
        ) != cls._normalize_match_text(room_title):
            room_bits.append(location)
        room_clause = ", ".join(room_bits).strip()
        if room_clause:
            room_clause_l = room_clause.lower()
            if room_clause_l not in prompt.lower():
                pending_prefixes.append(f"Location: {room_clause}.")

        missing_names = cls._missing_scene_names(prompt, party_snapshot)
        if missing_names:
            cast_fragments = []
            for entry in party_snapshot:
                name = str(entry.get("name") or "").strip()
                if not name:
                    continue
                if name not in missing_names:
                    continue
                tags = []
                persona = str(entry.get("persona") or "").strip()
                if persona:
                    tags.append(persona)
                cues = entry.get("attribute_cues") or []
                if cues:
                    tags.append(" / ".join([str(cue) for cue in cues[:2]]))
                items = entry.get("visible_items") or []
                if items:
                    tags.append(
                        "carrying " + ", ".join([str(item) for item in items[:2]])
                    )
                if tags:
                    cast_fragments.append(f"{name} ({'; '.join(tags)})")
                else:
                    cast_fragments.append(name)
            if cast_fragments:
                pending_prefixes.append(f"Characters: {'; '.join(cast_fragments)}.")

        if pending_prefixes:
            prompt = f"{' '.join(pending_prefixes)} {prompt}".strip()
        prompt = re.sub(r"\s+", " ", prompt).strip()
        if len(prompt) > cls.MAX_SCENE_PROMPT_CHARS:
            prompt = prompt[: cls.MAX_SCENE_PROMPT_CHARS].strip()
            missing_after_trim = cls._missing_scene_names(prompt, party_snapshot)
            if missing_after_trim:
                cast_prefix = f"Characters: {', '.join(missing_after_trim)}. "
                remaining = cls.MAX_SCENE_PROMPT_CHARS - len(cast_prefix)
                if remaining > 24:
                    prompt = (cast_prefix + prompt[:remaining]).strip()
                else:
                    prompt = cast_prefix[: cls.MAX_SCENE_PROMPT_CHARS].strip()
        return prompt

    @classmethod
    def _format_inventory(cls, player_state: Dict[str, object]) -> Optional[str]:
        if not isinstance(player_state, dict):
            return None
        items = cls._get_inventory_rich(player_state)
        if not items:
            return None
        names = [entry["name"] for entry in items]
        return f"Inventory: {', '.join(names)}"

    @classmethod
    def _normalize_inventory_items(cls, value) -> List[str]:
        def _item_to_text(item) -> str:
            if isinstance(item, dict):
                # Prefer stable user-facing names when model emits structured objects.
                if "name" in item and item.get("name") is not None:
                    return str(item.get("name")).strip()
                if "item" in item and item.get("item") is not None:
                    return str(item.get("item")).strip()
                if "title" in item and item.get("title") is not None:
                    return str(item.get("title")).strip()
                return ""
            return str(item).strip()

        if value is None:
            return []
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",")]
        if not isinstance(value, list):
            return []
        cleaned = []
        seen = set()
        for item in value:
            item_text = _item_to_text(item)
            if not item_text:
                continue
            norm = item_text.lower()
            if norm in seen:
                continue
            seen.add(norm)
            cleaned.append(item_text)
        return cleaned

    @classmethod
    def _get_inventory_rich(cls, player_state: Dict[str, object]) -> List[Dict[str, str]]:
        """Return inventory as a list of ``{"name": ..., "origin": ...}`` dicts.

        Handles both legacy plain-string inventories and the newer rich format.
        """
        raw = player_state.get("inventory") if isinstance(player_state, dict) else None
        if not raw:
            return []
        if not isinstance(raw, list):
            return []
        result = []
        seen = set()
        for item in raw:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("item") or item.get("title") or "").strip()
                origin = str(item.get("origin") or "").strip()
            else:
                name = str(item).strip()
                origin = ""
            if not name:
                continue
            norm = name.lower()
            if norm in seen:
                continue
            seen.add(norm)
            result.append({"name": name, "origin": origin})
        return result

    @classmethod
    def _apply_inventory_delta(
        cls,
        current: List[Dict[str, str]],
        adds: List[str],
        removes: List[str],
        origin_hint: str = "",
    ) -> List[Dict[str, str]]:
        """Apply adds/removes to a rich inventory list.

        *current* must be rich dicts (``{"name": ..., "origin": ...}``).
        *adds*/*removes* are plain item-name strings.
        New items receive *origin_hint* as their origin.
        """
        remove_norm = {item.lower() for item in removes}
        out: List[Dict[str, str]] = []
        for entry in current:
            if entry["name"].lower() in remove_norm:
                continue
            out.append(entry)
        out_norm = {entry["name"].lower() for entry in out}
        for item in adds:
            if item.lower() in out_norm:
                continue
            out.append({"name": item, "origin": origin_hint})
            out_norm.add(item.lower())
        return out

    @classmethod
    def _build_origin_hint(cls, narration_text: str, action_text: str) -> str:
        """Build a short origin string from the current narration/action context."""
        source = (narration_text or action_text or "").strip()
        if not source:
            return ""
        # Take the first sentence (or first 120 chars) as a concise origin.
        first_sentence = re.split(r'(?<=[.!?])\s', source, maxsplit=1)[0]
        return first_sentence[:120]

    _ITEM_STOPWORDS = {"a", "an", "the", "of", "and", "or", "to", "in", "on", "for"}

    @classmethod
    def _item_mentioned(cls, item_name: str, text_lower: str) -> bool:
        """Check whether *item_name* is referenced in *text_lower*.

        First tries an exact substring match.  If that fails, falls back to
        word-level matching: the item is considered mentioned when every
        significant word (>2 chars, not a stopword) in its name appears
        somewhere in the text.
        """
        item_l = item_name.lower()
        if item_l in text_lower:
            return True
        words = [
            w for w in re.findall(r"[a-z0-9]+", item_l)
            if len(w) > 2 and w not in cls._ITEM_STOPWORDS
        ]
        if not words:
            return False
        return all(w in text_lower for w in words)

    @classmethod
    def _sanitize_player_state_update(
        cls,
        previous_state: Dict[str, object],
        update: Dict[str, object],
        action_text: str = "",
        narration_text: str = "",
    ) -> Dict[str, object]:
        if not isinstance(update, dict):
            return {}
        cleaned = dict(update)
        previous_inventory_rich = cls._get_inventory_rich(previous_state)
        action_l = (action_text or "").lower()
        narration_l = (narration_text or "").lower()

        inventory_add = cls._normalize_inventory_items(cleaned.pop("inventory_add", []))
        inventory_remove = cls._normalize_inventory_items(
            cleaned.pop("inventory_remove", [])
        )

        if "inventory" in cleaned:
            model_inventory = cls._normalize_inventory_items(
                cleaned.pop("inventory", [])
            )
            model_set = {name.lower() for name in model_inventory}
            current_names = [entry["name"] for entry in previous_inventory_rich]
            current_set = {name.lower() for name in current_names}
            # Items the model dropped from the list → implicit removes.
            for name in current_names:
                if name.lower() not in model_set and name.lower() not in {
                    r.lower() for r in inventory_remove
                }:
                    inventory_remove.append(name)
            # Items the model introduced → implicit adds.
            for name in model_inventory:
                if name.lower() not in current_set and name.lower() not in {
                    a.lower() for a in inventory_add
                }:
                    inventory_add.append(name)
            if inventory_remove or inventory_add:
                logger.info(
                    "Converted full inventory list to deltas: adds=%s removes=%s",
                    inventory_add,
                    inventory_remove,
                )

        current_norm = {entry["name"].lower() for entry in previous_inventory_rich}
        inventory_remove = [
            item for item in inventory_remove if item.lower() in current_norm
        ]

        if len(inventory_add) > cls.MAX_INVENTORY_CHANGES_PER_TURN:
            logger.warning(
                "Truncating inventory adds from %d to %d",
                len(inventory_add),
                cls.MAX_INVENTORY_CHANGES_PER_TURN,
            )
            inventory_add = inventory_add[: cls.MAX_INVENTORY_CHANGES_PER_TURN]
        if len(inventory_remove) > cls.MAX_INVENTORY_CHANGES_PER_TURN:
            logger.warning(
                "Truncating inventory removes from %d to %d",
                len(inventory_remove),
                cls.MAX_INVENTORY_CHANGES_PER_TURN,
            )
            inventory_remove = inventory_remove[: cls.MAX_INVENTORY_CHANGES_PER_TURN]

        origin_hint = cls._build_origin_hint(narration_text, action_text)

        if inventory_add or inventory_remove:
            cleaned["inventory"] = cls._apply_inventory_delta(
                previous_inventory_rich, inventory_add, inventory_remove,
                origin_hint=origin_hint,
            )
        else:
            cleaned["inventory"] = previous_inventory_rich

        for key in list(cleaned.keys()):
            if key != "inventory" and "inventory" in str(key).lower():
                cleaned.pop(key, None)

        # When location changes but room_description wasn't provided, clear
        # stale room data so the look command doesn't show the old room.
        new_location = cleaned.get("location")
        if new_location is not None:
            old_location = previous_state.get("location")
            if str(new_location).strip().lower() != str(old_location or "").strip().lower():
                if "room_description" not in cleaned:
                    cleaned["room_description"] = None
                if "room_title" not in cleaned:
                    cleaned["room_title"] = None

        return cleaned

    @classmethod
    def _strip_inventory_from_narration(cls, narration: str) -> str:
        if not narration:
            return ""
        # Drop any model-authored inventory line(s); we append canonical inventory later.
        kept_lines = []
        for line in narration.splitlines():
            if line.strip().lower().startswith("inventory:"):
                continue
            kept_lines.append(line)
        cleaned = "\n".join(kept_lines).strip()
        return cleaned

    @classmethod
    def _strip_inventory_mentions(cls, text: str) -> str:
        if not text:
            return ""
        return cls._strip_inventory_from_narration(text)

    @classmethod
    def _scrub_inventory_from_state(cls, value):
        if isinstance(value, dict):
            cleaned = {}
            for key, item in value.items():
                key_str = str(key).lower()
                if key_str == "inventory" or "inventory" in key_str:
                    continue
                cleaned[key] = cls._scrub_inventory_from_state(item)
            return cleaned
        if isinstance(value, list):
            return [cls._scrub_inventory_from_state(item) for item in value]
        return value

    @classmethod
    def total_points_for_level(cls, level: int) -> int:
        return cls.BASE_POINTS + max(level - 1, 0) * cls.POINTS_PER_LEVEL

    @classmethod
    def xp_needed_for_level(cls, level: int) -> int:
        return cls.XP_BASE + max(level - 1, 0) * cls.XP_PER_LEVEL

    @classmethod
    def get_or_create_channel(cls, guild_id: int, channel_id: int) -> ZorkChannel:
        channel = ZorkChannel.query.filter_by(
            guild_id=guild_id, channel_id=channel_id
        ).first()
        if channel is None:
            channel = ZorkChannel(
                guild_id=guild_id, channel_id=channel_id, enabled=False
            )
            db.session.add(channel)
            db.session.commit()
        return channel

    @classmethod
    def is_channel_enabled(cls, guild_id: int, channel_id: int) -> bool:
        channel = ZorkChannel.query.filter_by(
            guild_id=guild_id, channel_id=channel_id
        ).first()
        if channel is None:
            return False
        return bool(channel.enabled)

    @classmethod
    def get_or_create_campaign(
        cls, guild_id: int, name: str, created_by: int
    ) -> ZorkCampaign:
        normalized = cls._normalize_campaign_name(name)
        campaign = ZorkCampaign.query.filter_by(
            guild_id=guild_id, name=normalized
        ).first()
        if campaign is None:
            campaign = ZorkCampaign(
                guild_id=guild_id,
                name=normalized,
                created_by=created_by,
                summary="",
                state_json="{}",
            )
            db.session.add(campaign)
            db.session.commit()
        preset = cls._get_preset_campaign(normalized)
        if preset:
            is_empty_summary = not (campaign.summary or "").strip()
            is_empty_state = cls._load_json(campaign.state_json, {}) == {}
            if is_empty_summary and is_empty_state:
                campaign.summary = preset.get("summary", "") or ""
                campaign.state_json = cls._dump_json(preset.get("state", {}) or {})
                campaign.last_narration = preset.get("last_narration")
                campaign.updated = db.func.now()
                db.session.commit()
        return campaign

    @classmethod
    def enable_channel(
        cls, guild_id: int, channel_id: int, user_id: int
    ) -> Tuple[ZorkChannel, ZorkCampaign]:
        channel = cls.get_or_create_channel(guild_id, channel_id)
        if channel.active_campaign_id is None:
            campaign = cls.get_or_create_campaign(guild_id, "main", user_id)
            channel.active_campaign_id = campaign.id
        else:
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
            if campaign is None:
                campaign = cls.get_or_create_campaign(guild_id, "main", user_id)
                channel.active_campaign_id = campaign.id
        channel.enabled = True
        channel.updated = db.func.now()
        db.session.commit()
        return channel, campaign

    @classmethod
    def list_campaigns(cls, guild_id: int) -> List[ZorkCampaign]:
        return (
            ZorkCampaign.query.filter_by(guild_id=guild_id)
            .order_by(ZorkCampaign.name.asc())
            .all()
        )

    @classmethod
    def can_switch_campaign(
        cls, campaign_id: int, user_id: int, window_seconds: int = 3600
    ) -> Tuple[bool, int]:
        cutoff = cls._now() - datetime.timedelta(seconds=window_seconds)
        active_count = ZorkPlayer.query.filter(
            ZorkPlayer.campaign_id == campaign_id,
            ZorkPlayer.user_id != user_id,
            ZorkPlayer.last_active >= cutoff,
        ).count()
        return active_count == 0, active_count

    @classmethod
    def set_active_campaign(
        cls,
        channel: ZorkChannel,
        guild_id: int,
        name: str,
        user_id: int,
        enforce_activity_window: bool = True,
    ) -> Tuple[ZorkCampaign, bool, Optional[str]]:
        normalized = cls._normalize_campaign_name(name)
        if enforce_activity_window and channel.active_campaign_id is not None:
            can_switch, active_count = cls.can_switch_campaign(
                channel.active_campaign_id, user_id
            )
            if not can_switch:
                return (
                    None,
                    False,
                    f"{active_count} other player(s) active in last hour",
                )
        campaign = cls.get_or_create_campaign(guild_id, normalized, user_id)
        channel.active_campaign_id = campaign.id
        channel.updated = db.func.now()
        db.session.commit()
        return campaign, True, None

    @classmethod
    def get_or_create_player(
        cls,
        campaign_id: int,
        user_id: int,
        campaign: Optional[ZorkCampaign] = None,
    ) -> ZorkPlayer:
        player = ZorkPlayer.query.filter_by(
            campaign_id=campaign_id, user_id=user_id
        ).first()
        if player is None:
            player_state = {}
            if campaign is not None:
                campaign_state = cls.get_campaign_state(campaign)
                start_room = campaign_state.get("start_room")
                if isinstance(start_room, dict):
                    player_state.update(start_room)
                player_state["persona"] = cls.get_campaign_default_persona(
                    campaign,
                    campaign_state=campaign_state,
                )
            player = ZorkPlayer(
                campaign_id=campaign_id,
                user_id=user_id,
                level=1,
                xp=0,
                attributes_json="{}",
                state_json=cls._dump_json(player_state),
            )
            db.session.add(player)
            db.session.commit()
        return player

    @classmethod
    def get_recent_turns(
        cls,
        campaign_id: int,
        user_id: Optional[int] = None,
        limit: int = None,
    ) -> List[ZorkTurn]:
        if limit is None:
            limit = cls.MAX_RECENT_TURNS
        query = ZorkTurn.query.filter_by(campaign_id=campaign_id)
        if user_id is not None:
            query = query.filter_by(user_id=user_id, kind="player")
        turns = query.order_by(ZorkTurn.id.desc()).limit(limit).all()
        return list(reversed(turns))

    @classmethod
    def _copy_identity_fields(
        cls, source_state: Dict[str, object], target_state: Dict[str, object]
    ) -> Dict[str, object]:
        if not isinstance(target_state, dict):
            target_state = {}
        if not isinstance(source_state, dict):
            return target_state
        for key in ("character_name", "persona"):
            value = source_state.get(key)
            if value:
                target_state[key] = value
        return target_state

    @classmethod
    def _sanitize_campaign_name_text(cls, text: str) -> str:
        if not text:
            return ""
        text = text.strip()
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"[^a-zA-Z0-9 _-]", "", text)
        return text[:48]

    @classmethod
    def _build_campaign_suggestion_text(cls, guild_id: int) -> str:
        existing = cls.list_campaigns(guild_id)
        names = [campaign.name for campaign in existing]
        if not names:
            return "No campaigns exist yet."
        sample = ", ".join(names[:8])
        return f"Existing campaigns: {sample}"

    @classmethod
    def _gpu_worker_available(cls) -> bool:
        discord_wrapper = DiscordBot.get_instance()
        if discord_wrapper is None or discord_wrapper.worker_manager is None:
            return False
        worker = discord_wrapper.worker_manager.find_first_worker("gpu")
        return worker is not None

    @classmethod
    async def _enqueue_scene_image(
        cls,
        ctx,
        scene_image_prompt: str,
        campaign_id: Optional[int] = None,
        room_key: Optional[str] = None,
    ):
        if not scene_image_prompt:
            return
        if not cls._gpu_worker_available():
            return
        discord_wrapper = DiscordBot.get_instance()
        if discord_wrapper is None or discord_wrapper.bot is None:
            return
        generator = discord_wrapper.bot.get_cog("Generate")
        if generator is None:
            return
        reference_images = []
        avatar_refs = []
        selected_model = cls.DEFAULT_SCENE_IMAGE_MODEL
        prompt_for_generation = scene_image_prompt
        should_store_room_image = False
        has_room_reference = False
        player_state_for_prompt = {}
        app = AppConfig.get_flask()
        if app is not None and campaign_id is not None:
            with app.app_context():
                campaign = ZorkCampaign.query.get(campaign_id)
                if campaign is not None:
                    campaign_state = cls.get_campaign_state(campaign)
                    model_override = campaign_state.get("scene_image_model")
                    if isinstance(model_override, str) and model_override.strip():
                        selected_model = model_override.strip()
                    player = ZorkPlayer.query.filter_by(
                        campaign_id=campaign.id, user_id=ctx.author.id
                    ).first()
                    player_state = (
                        cls.get_player_state(player) if player is not None else {}
                    )
                    player_state_for_prompt = player_state
                    if not room_key:
                        room_key = cls._room_key_from_player_state(player_state)
                    if room_key:
                        cached_url = cls.get_room_scene_image_url(campaign, room_key)
                        if cached_url and cls._is_image_url_404(cached_url):
                            cls.clear_room_scene_image_url(campaign, room_key)
                            cached_url = None
                        if cached_url:
                            reference_images.append(cached_url)
                            has_room_reference = True
                        else:
                            should_store_room_image = True
                    if player is not None and not should_store_room_image:
                        avatar_refs = cls._build_scene_avatar_references(
                            campaign, player, player_state
                        )
                        for ref in avatar_refs:
                            ref_url = str(ref.get("url") or "").strip()
                            if not ref_url:
                                continue
                            if ref_url in reference_images:
                                continue
                            reference_images.append(ref_url)
                            if len(reference_images) >= cls.MAX_SCENE_REFERENCE_IMAGES:
                                break
                    if should_store_room_image:
                        prompt_for_generation = cls._compose_empty_room_scene_prompt(
                            scene_image_prompt,
                            player_state=player_state_for_prompt,
                        )
                    else:
                        prompt_for_generation = (
                            cls._compose_scene_prompt_with_references(
                                scene_image_prompt,
                                has_room_reference=has_room_reference,
                                avatar_refs=avatar_refs[
                                    : max(cls.MAX_SCENE_REFERENCE_IMAGES - 1, 0)
                                ],
                            )
                        )
        cfg = AppConfig()
        user_config = cfg.get_user_config(user_id=ctx.author.id)
        user_config["auto_model"] = False
        user_config["model"] = selected_model
        user_config["steps"] = 12
        user_config["guidance_scaling"] = 2.5
        user_config["guidance_scale"] = 2.5
        try:
            await generator.generate_from_user_config(
                ctx=ctx,
                user_config=user_config,
                user_id=ctx.author.id,
                prompt=prompt_for_generation,
                job_metadata={
                    "zork_scene": True,
                    "suppress_image_reactions": True,
                    "suppress_image_details": True,
                    "zork_store_image": should_store_room_image,
                    "zork_seed_room_image": should_store_room_image,
                    "zork_scene_prompt": cls._trim_text(
                        scene_image_prompt, cls.MAX_SCENE_PROMPT_CHARS
                    ),
                    "zork_campaign_id": campaign_id,
                    "zork_room_key": room_key,
                    "zork_user_id": ctx.author.id,
                },
                image_data=reference_images if reference_images else None,
            )
        except Exception as e:
            logger.warning(f"Failed to enqueue scene image prompt: {e}")

    @classmethod
    def _build_synthetic_generation_context(cls, channel, user_id: int):
        guild = getattr(channel, "guild", None)
        member = guild.get_member(int(user_id)) if guild is not None else None
        author = SimpleNamespace(
            id=int(user_id),
            name=getattr(member, "name", f"user-{user_id}"),
            discriminator=str(getattr(member, "discriminator", "0")),
        )
        return SimpleNamespace(
            id=getattr(channel, "id", int(user_id)),
            author=author,
            channel=channel,
            guild=guild,
            message=None,
        )

    @classmethod
    async def enqueue_scene_composite_from_seed(
        cls,
        channel,
        campaign_id: int,
        room_key: str,
        user_id: int,
        scene_prompt: str,
        base_image_url: str,
    ) -> bool:
        if not cls._gpu_worker_available():
            return False
        if not campaign_id or not room_key or not user_id:
            return False
        if not isinstance(scene_prompt, str) or not scene_prompt.strip():
            return False
        if not isinstance(base_image_url, str) or not base_image_url.strip():
            return False
        discord_wrapper = DiscordBot.get_instance()
        if discord_wrapper is None or discord_wrapper.bot is None:
            return False
        generator = discord_wrapper.bot.get_cog("Generate")
        if generator is None:
            return False

        reference_images = [base_image_url.strip()]
        avatar_refs = []
        selected_model = cls.DEFAULT_SCENE_IMAGE_MODEL
        app = AppConfig.get_flask()
        if app is None:
            return False
        with app.app_context():
            campaign = ZorkCampaign.query.get(campaign_id)
            if campaign is None:
                return False
            campaign_state = cls.get_campaign_state(campaign)
            model_override = campaign_state.get("scene_image_model")
            if isinstance(model_override, str) and model_override.strip():
                selected_model = model_override.strip()
            player = ZorkPlayer.query.filter_by(
                campaign_id=campaign.id, user_id=int(user_id)
            ).first()
            player_state = cls.get_player_state(player) if player is not None else {}
            if player is not None:
                avatar_refs = cls._build_scene_avatar_references(
                    campaign, player, player_state
                )
                for ref in avatar_refs:
                    ref_url = str(ref.get("url") or "").strip()
                    if not ref_url:
                        continue
                    if ref_url in reference_images:
                        continue
                    reference_images.append(ref_url)
                    if len(reference_images) >= cls.MAX_SCENE_REFERENCE_IMAGES:
                        break

        composed_prompt = cls._compose_scene_prompt_with_references(
            scene_prompt.strip(),
            has_room_reference=True,
            avatar_refs=avatar_refs[: max(cls.MAX_SCENE_REFERENCE_IMAGES - 1, 0)],
        )
        if not composed_prompt:
            return False
        ctx = cls._build_synthetic_generation_context(channel, int(user_id))
        cfg = AppConfig()
        user_config = cfg.get_user_config(user_id=int(user_id))
        user_config["auto_model"] = False
        user_config["model"] = selected_model
        user_config["steps"] = 12
        user_config["guidance_scaling"] = 2.5
        user_config["guidance_scale"] = 2.5
        try:
            await generator.generate_from_user_config(
                ctx=ctx,
                user_config=user_config,
                user_id=int(user_id),
                prompt=composed_prompt,
                job_metadata={
                    "zork_scene": True,
                    "suppress_image_reactions": True,
                    "suppress_image_details": True,
                    "zork_store_image": False,
                    "zork_seed_room_image": False,
                    "zork_campaign_id": int(campaign_id),
                    "zork_room_key": room_key,
                    "zork_user_id": int(user_id),
                },
                image_data=reference_images,
            )
        except Exception as e:
            logger.warning(f"Failed to enqueue zork composite scene image: {e}")
            return False
        return True

    @classmethod
    def _compose_avatar_prompt(
        cls,
        player_state: Dict[str, object],
        requested_prompt: str,
        fallback_name: str,
    ) -> str:
        identity = str(
            player_state.get("character_name") or fallback_name or "adventurer"
        ).strip()
        persona = str(player_state.get("persona") or "").strip()
        prompt_parts = [
            f"Single-character concept portrait of {identity}.",
            requested_prompt.strip(),
            "isolated subject",
            "full body",
            "centered composition",
        ]
        if persona:
            prompt_parts.insert(1, f"Persona/style notes: {persona}.")
        composed = " ".join([piece for piece in prompt_parts if piece])
        composed = re.sub(r"\s+", " ", composed).strip()
        return cls._trim_text(composed, 900)

    @classmethod
    async def enqueue_avatar_generation(
        cls,
        ctx,
        campaign: ZorkCampaign,
        player: ZorkPlayer,
        requested_prompt: str,
    ) -> Tuple[bool, str]:
        if not requested_prompt or not requested_prompt.strip():
            return False, "Avatar prompt cannot be empty."
        if not cls._gpu_worker_available():
            return False, "No GPU workers available right now."
        discord_wrapper = DiscordBot.get_instance()
        if discord_wrapper is None or discord_wrapper.bot is None:
            return False, "Bot runtime is not ready."
        generator = discord_wrapper.bot.get_cog("Generate")
        if generator is None:
            return False, "Image generation cog is not loaded."

        player_state = cls.get_player_state(player)
        composed_prompt = cls._compose_avatar_prompt(
            player_state,
            requested_prompt=requested_prompt,
            fallback_name=getattr(
                getattr(ctx, "author", None), "display_name", "adventurer"
            ),
        )

        campaign_state = cls.get_campaign_state(campaign)
        selected_model = campaign_state.get("avatar_image_model")
        if not isinstance(selected_model, str) or not selected_model.strip():
            selected_model = cls.DEFAULT_AVATAR_IMAGE_MODEL

        player_state["pending_avatar_prompt"] = cls._trim_text(
            requested_prompt.strip(), 500
        )
        player_state.pop("pending_avatar_url", None)
        player.state_json = cls._dump_json(player_state)
        player.updated = db.func.now()
        db.session.commit()

        cfg = AppConfig()
        user_config = cfg.get_user_config(user_id=ctx.author.id)
        user_config["auto_model"] = False
        user_config["model"] = selected_model
        user_config["steps"] = 16
        user_config["guidance_scaling"] = 3.0
        user_config["guidance_scale"] = 3.0
        user_config["resolution"] = {"width": 768, "height": 768}

        try:
            await generator.generate_from_user_config(
                ctx=ctx,
                user_config=user_config,
                user_id=ctx.author.id,
                prompt=composed_prompt,
                job_metadata={
                    "zork_scene": True,
                    "suppress_image_reactions": True,
                    "suppress_image_details": True,
                    "zork_store_avatar": True,
                    "zork_campaign_id": campaign.id,
                    "zork_avatar_user_id": player.user_id,
                },
            )
        except Exception as e:
            return False, f"Failed to queue avatar generation: {e}"
        return (
            True,
            "Avatar candidate queued. Use `!zork avatar accept` or `!zork avatar decline` after it arrives.",
        )

    @classmethod
    def get_player_attributes(cls, player: ZorkPlayer) -> Dict[str, int]:
        data = cls._load_json(player.attributes_json, {})
        return data if isinstance(data, dict) else {}

    @classmethod
    def get_player_state(cls, player: ZorkPlayer) -> Dict[str, object]:
        data = cls._load_json(player.state_json, {})
        return data if isinstance(data, dict) else {}

    @classmethod
    def get_campaign_state(cls, campaign: ZorkCampaign) -> Dict[str, object]:
        data = cls._load_json(campaign.state_json, {})
        return data if isinstance(data, dict) else {}

    @classmethod
    def get_campaign_characters(cls, campaign: ZorkCampaign) -> Dict[str, dict]:
        """Load the characters dict from campaign.characters_json."""
        data = cls._load_json(campaign.characters_json, {})
        return data if isinstance(data, dict) else {}

    @classmethod
    def _apply_character_updates(
        cls, existing: Dict[str, dict], updates: Dict[str, dict]
    ) -> Dict[str, dict]:
        """Merge character updates into existing characters dict.

        New slugs get all fields stored.  Existing slugs only get mutable
        fields updated — immutable fields are silently dropped.
        """
        if not isinstance(updates, dict):
            return existing
        for slug, fields in updates.items():
            if not isinstance(fields, dict):
                continue
            slug = str(slug).strip()
            if not slug:
                continue
            if slug in existing:
                # Existing character — only accept mutable fields.
                for key, value in fields.items():
                    if key not in cls.IMMUTABLE_CHARACTER_FIELDS:
                        existing[slug][key] = value
            else:
                # New character — store everything.
                existing[slug] = dict(fields)
        return existing

    @classmethod
    def _build_characters_for_prompt(
        cls,
        characters: Dict[str, dict],
        player_state: Dict[str, object],
        recent_text: str,
    ) -> list:
        """Build a tiered character list for the prompt.

        - Nearby (same location as player): full record
        - Recently mentioned in recent_text: condensed
        - Distant/deceased: minimal
        """
        if not characters:
            return []
        player_location = str(player_state.get("location") or "").strip().lower()
        recent_lower = recent_text.lower() if recent_text else ""

        nearby = []
        mentioned = []
        distant = []

        for slug, char in characters.items():
            char_location = str(char.get("location") or "").strip().lower()
            char_name = str(char.get("name") or slug).strip().lower()
            is_deceased = bool(char.get("deceased_reason"))

            if not is_deceased and player_location and char_location == player_location:
                # Full record for nearby characters.
                entry = dict(char)
                entry["_slug"] = slug
                nearby.append(entry)
            elif char_name in recent_lower or slug in recent_lower:
                # Condensed for recently mentioned.
                entry = {
                    "_slug": slug,
                    "name": char.get("name", slug),
                    "location": char.get("location"),
                    "current_status": char.get("current_status"),
                    "allegiance": char.get("allegiance"),
                }
                if is_deceased:
                    entry["deceased_reason"] = char.get("deceased_reason")
                mentioned.append(entry)
            else:
                # Minimal for distant/deceased.
                entry = {"_slug": slug, "name": char.get("name", slug)}
                if is_deceased:
                    entry["deceased_reason"] = char.get("deceased_reason")
                else:
                    entry["location"] = char.get("location")
                distant.append(entry)

        result = nearby + mentioned + distant
        return result[: cls.MAX_CHARACTERS_IN_PROMPT]

    @classmethod
    def _fit_characters_to_budget(cls, characters_list: list, max_chars: int) -> list:
        """Trim characters from the end until the JSON representation fits."""
        while characters_list:
            text = json.dumps(characters_list, ensure_ascii=True)
            if len(text) <= max_chars:
                return characters_list
            characters_list = characters_list[:-1]
        return []

    @classmethod
    def is_guardrails_enabled(cls, campaign: Optional[ZorkCampaign]) -> bool:
        if campaign is None:
            return False
        campaign_state = cls.get_campaign_state(campaign)
        return bool(campaign_state.get("guardrails_enabled", False))

    @classmethod
    def set_guardrails_enabled(
        cls, campaign: Optional[ZorkCampaign], enabled: bool
    ) -> bool:
        if campaign is None:
            return False
        campaign_state = cls.get_campaign_state(campaign)
        campaign_state["guardrails_enabled"] = bool(enabled)
        campaign.state_json = cls._dump_json(campaign_state)
        campaign.updated = db.func.now()
        db.session.commit()
        return True

    @classmethod
    def is_timed_events_enabled(cls, campaign: Optional[ZorkCampaign]) -> bool:
        if campaign is None:
            return False
        campaign_state = cls.get_campaign_state(campaign)
        return bool(campaign_state.get("timed_events_enabled", True))

    @classmethod
    def set_timed_events_enabled(
        cls, campaign: Optional[ZorkCampaign], enabled: bool
    ) -> bool:
        if campaign is None:
            return False
        campaign_state = cls.get_campaign_state(campaign)
        campaign_state["timed_events_enabled"] = bool(enabled)
        campaign.state_json = cls._dump_json(campaign_state)
        campaign.updated = db.func.now()
        db.session.commit()
        if not enabled:
            cls.cancel_pending_timer(campaign.id)
        return True

    @classmethod
    def cancel_pending_timer(cls, campaign_id: int) -> Optional[dict]:
        """Cancel a pending timer and return its context dict (or None)."""
        ctx_dict = cls._pending_timers.pop(campaign_id, None)
        if ctx_dict is None:
            return None
        task = ctx_dict.get("task")
        if task is not None and not task.done():
            task.cancel()
        # Schedule a message edit to remove the live countdown.
        message_id = ctx_dict.get("message_id")
        channel_id = ctx_dict.get("channel_id")
        if message_id and channel_id:
            event = ctx_dict.get("event", "unknown event")
            asyncio.ensure_future(
                cls._edit_timer_line(channel_id, message_id, f"\u2705 *Timer cancelled — you acted in time. (Averted: {event})*")
            )
        return ctx_dict

    @classmethod
    def register_timer_message(cls, campaign_id: int, message_id: int):
        """Called by the cog after sending a reply that contains a timer countdown."""
        ctx_dict = cls._pending_timers.get(campaign_id)
        if ctx_dict is not None:
            ctx_dict["message_id"] = message_id

    @classmethod
    async def _edit_timer_line(cls, channel_id: int, message_id: int, replacement: str):
        """Edit a Discord message to replace the ⏰ countdown line."""
        try:
            bot_instance = DiscordBot.get_instance()
            if bot_instance is None:
                return
            channel = await bot_instance.find_channel(channel_id)
            if channel is None:
                return
            message = await channel.fetch_message(message_id)
            if message is None:
                return
            content = message.content
            # Replace the ⏰ line with the new text.
            lines = content.split("\n")
            new_lines = []
            for line in lines:
                if line.startswith("\u23f0"):
                    new_lines.append(replacement)
                else:
                    new_lines.append(line)
            new_content = "\n".join(new_lines)
            if len(new_content) > 2000:
                new_content = new_content[:1997] + "..."
            if new_content != content:
                await message.edit(content=new_content)
        except Exception:
            logger.debug("Failed to edit timer message %s", message_id, exc_info=True)

    @classmethod
    def _build_rails_context(
        cls,
        player_state: Dict[str, object],
        party_snapshot: List[Dict[str, object]],
    ) -> Dict[str, object]:
        exits = player_state.get("exits")
        if not isinstance(exits, list):
            exits = []
        known_names = []
        for entry in party_snapshot:
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            known_names.append(name)
        inventory_rich = cls._get_inventory_rich(player_state)[:20]
        return {
            "room_title": player_state.get("room_title"),
            "room_summary": player_state.get("room_summary"),
            "location": player_state.get("location"),
            "exits": exits[:12],
            "inventory": inventory_rich,
            "known_characters": known_names[:12],
            "strict_action_shape": "one concrete action grounded in current room and items",
        }

    @classmethod
    def points_spent(cls, attributes: Dict[str, int]) -> int:
        total = 0
        for value in attributes.values():
            if isinstance(value, int):
                total += value
        return total

    @classmethod
    def set_attribute(
        cls,
        player: ZorkPlayer,
        name: str,
        value: int,
    ) -> Tuple[bool, str]:
        if value < 0 or value > cls.MAX_ATTRIBUTE_VALUE:
            return False, f"Value must be between 0 and {cls.MAX_ATTRIBUTE_VALUE}."
        attributes = cls.get_player_attributes(player)
        attributes[name] = value
        total_points = cls.total_points_for_level(player.level)
        spent = cls.points_spent(attributes)
        if spent > total_points:
            return False, f"Not enough points. You have {total_points} total points."
        player.attributes_json = cls._dump_json(attributes)
        player.updated = db.func.now()
        db.session.commit()
        return True, "Attribute updated."

    @classmethod
    def level_up(cls, player: ZorkPlayer) -> Tuple[bool, str]:
        needed = cls.xp_needed_for_level(player.level)
        if player.xp < needed:
            return False, f"Need {needed} XP to level up."
        player.xp -= needed
        player.level += 1
        player.updated = db.func.now()
        db.session.commit()
        return True, f"Leveled up to {player.level}."

    @classmethod
    def build_prompt(
        cls,
        campaign: ZorkCampaign,
        player: ZorkPlayer,
        action: str,
        turns: List[ZorkTurn],
        party_snapshot: Optional[List[Dict[str, object]]] = None,
        is_new_player: bool = False,
    ) -> Tuple[str, str]:
        summary = cls._strip_inventory_mentions(campaign.summary or "")
        summary = cls._trim_text(summary, cls.MAX_SUMMARY_CHARS)
        state = cls.get_campaign_state(campaign)
        state = cls._scrub_inventory_from_state(state)
        guardrails_enabled = bool(state.get("guardrails_enabled", False))
        model_state = cls._build_model_state(state)
        model_state = cls._fit_state_to_budget(model_state, cls.MAX_STATE_CHARS)
        attributes = cls.get_player_attributes(player)
        player_state = cls.get_player_state(player)
        if party_snapshot is None:
            party_snapshot = cls._build_party_snapshot_for_prompt(
                campaign, player, player_state
            )
        player_state_prompt = cls._build_player_state_for_prompt(player_state)
        total_points = cls.total_points_for_level(player.level)
        spent = cls.points_spent(attributes)
        player_card = {
            "level": player.level,
            "xp": player.xp,
            "points_total": total_points,
            "points_spent": spent,
            "attributes": attributes,
            "state": player_state_prompt,
        }

        recent_lines = []
        _OOC_RE = re.compile(r"^\s*\[OOC\b", re.IGNORECASE)
        _ERROR_PHRASES = ("a hollow silence answers", "the world shifts, but nothing clear emerges")
        for turn in turns:
            content = (turn.content or "").strip()
            if not content:
                continue
            if turn.kind == "player":
                # Skip OOC messages — those are meta-messages to the GM.
                if _OOC_RE.match(content):
                    continue
                clipped = cls._trim_text(content, cls.MAX_TURN_CHARS)
                clipped = cls._strip_inventory_mentions(clipped)
                recent_lines.append(f"PLAYER: {clipped}")
            elif turn.kind == "narrator":
                # Skip error/fallback narrations.
                if content.lower() in _ERROR_PHRASES:
                    continue
                clipped = cls._trim_text(content, cls.MAX_TURN_CHARS)
                clipped = cls._strip_inventory_mentions(clipped)
                recent_lines.append(f"NARRATOR: {clipped}")
        recent_text = "\n".join(recent_lines) if recent_lines else "None"
        rails_context = cls._build_rails_context(player_state, party_snapshot)

        characters = cls.get_campaign_characters(campaign)
        characters_for_prompt = cls._build_characters_for_prompt(
            characters, player_state, recent_text
        )
        characters_for_prompt = cls._fit_characters_to_budget(
            characters_for_prompt, cls.MAX_CHARACTERS_CHARS
        )

        user_prompt = (
            f"CAMPAIGN: {campaign.name}\n"
            f"PLAYER_ID: {player.user_id}\n"
            f"IS_NEW_PLAYER: {str(is_new_player).lower()}\n"
            f"GUARDRAILS_ENABLED: {str(guardrails_enabled).lower()}\n"
            f"RAILS_CONTEXT: {cls._dump_json(rails_context)}\n"
            f"WORLD_SUMMARY: {summary}\n"
            f"WORLD_STATE: {cls._dump_json(model_state)}\n"
            f"WORLD_CHARACTERS: {cls._dump_json(characters_for_prompt)}\n"
            f"PLAYER_CARD: {cls._dump_json(player_card)}\n"
            f"PARTY_SNAPSHOT: {cls._dump_json(party_snapshot)}\n"
            f"RECENT_TURNS:\n{recent_text}\n"
            f"PLAYER_ACTION: {action}\n"
        )
        system_prompt = cls.SYSTEM_PROMPT
        if guardrails_enabled:
            system_prompt = f"{system_prompt}{cls.GUARDRAILS_SYSTEM_PROMPT}"
        system_prompt = f"{system_prompt}{cls.MEMORY_TOOL_PROMPT}"
        if state.get("timed_events_enabled", True):
            system_prompt = f"{system_prompt}{cls.TIMER_TOOL_PROMPT}"
        return system_prompt, user_prompt

    @staticmethod
    def _is_tool_call(payload: dict) -> bool:
        """Return True when *payload* is a memory_search tool invocation."""
        return (
            isinstance(payload, dict)
            and "tool_call" in payload
            and "narration" not in payload
        )

    @classmethod
    def _extract_json(cls, text: str) -> Optional[str]:
        text = text.strip()
        if "```" in text:
            # Strip code fence markers (```json, ```, etc.) without
            # dropping lines that also contain JSON content.
            text = re.sub(r"```\w*", "", text).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        return text[start : end + 1]

    @staticmethod
    def _parse_json_lenient(text: str) -> Optional[dict]:
        """Parse a JSON object from *text*, tolerating trailing extra data.

        If the text contains multiple JSON objects (JSONL-style), parse each
        one and shallow-merge them into a single dict so no data is lost.
        """
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result
            return {}
        except json.JSONDecodeError as exc:
            if "Extra data" not in str(exc):
                raise
            # JSONL-style: decode successive objects and merge them.
            merged = {}
            decoder = json.JSONDecoder()
            idx = 0
            length = len(text)
            while idx < length:
                # Skip whitespace between objects.
                while idx < length and text[idx] in " \t\r\n":
                    idx += 1
                if idx >= length:
                    break
                try:
                    obj, end_idx = decoder.raw_decode(text, idx)
                    if isinstance(obj, dict):
                        merged.update(obj)
                    idx = end_idx
                except (json.JSONDecodeError, ValueError):
                    break
            if merged:
                return merged
            raise

    @classmethod
    def _clean_response(cls, response: str) -> str:
        """Strip text outside the JSON object so duplicate narration and fencing are removed."""
        if not response:
            return response
        json_text = cls._extract_json(response)
        if json_text:
            return json_text
        return response.strip()

    @classmethod
    def _extract_ascii_map(cls, text: str) -> str:
        if not text:
            return ""
        lines = []
        for line in text.splitlines():
            if "```" in line:
                continue
            lines.append(line.rstrip())
        return "\n".join(lines).strip()

    @classmethod
    def _assign_player_markers(
        cls, players: List["ZorkPlayer"], exclude_user_id: int
    ) -> List[dict]:
        markers = []
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        idx = 0
        for player in players:
            if player.user_id == exclude_user_id:
                continue
            if idx >= len(letters):
                break
            marker = letters[idx]
            idx += 1
            markers.append({"marker": marker, "player": player})
        return markers

    @classmethod
    def _apply_state_update(
        cls, state: Dict[str, object], update: Dict[str, object]
    ) -> Dict[str, object]:
        if not isinstance(update, dict):
            return state
        for key, value in update.items():
            if value is None:
                state.pop(key, None)
            elif isinstance(value, str) and value.strip().lower() in cls._COMPLETED_VALUES:
                # Resolved entries don't need to stay in active state.
                state.pop(key, None)
            else:
                state[key] = value
        return state

    @classmethod
    async def play_action(
        cls,
        ctx,
        action: str,
        command_prefix: str = "!",
        campaign_id: Optional[int] = None,
        manage_claim: bool = True,
    ) -> Optional[str]:
        app = AppConfig.get_flask()
        if app is None:
            raise RuntimeError("Flask app not initialized; cannot use ZorkEmulator.")
        should_clear_claim = manage_claim
        if campaign_id is None:
            campaign_id, error_text = await cls.begin_turn(
                ctx, command_prefix=command_prefix
            )
            if error_text is not None:
                return error_text
            if campaign_id is None:
                return None
            should_clear_claim = True
        lock = cls._get_lock(campaign_id)

        try:
            async with lock:
                with app.app_context():
                    campaign = ZorkCampaign.query.get(campaign_id)
                    player = cls.get_or_create_player(
                        campaign_id, ctx.author.id, campaign=campaign
                    )
                    cls.record_player_message(player)
                    player.last_active = db.func.now()
                    player.updated = db.func.now()
                    db.session.commit()
                    timer_interrupt_context = None
                    pending = cls._pending_timers.get(campaign_id)
                    if pending is not None:
                        if pending.get("interruptible", True):
                            cancelled_timer = cls.cancel_pending_timer(campaign_id)
                            if cancelled_timer:
                                cls.increment_player_stat(
                                    player, cls.PLAYER_STATS_TIMERS_AVERTED_KEY
                                )
                                player.updated = db.func.now()
                                db.session.commit()
                                interrupt_action = cancelled_timer.get("interrupt_action")
                                if interrupt_action:
                                    timer_interrupt_context = interrupt_action
                                # Persist the interruption as a turn so it appears in RECENT_TURNS.
                                event_desc = cancelled_timer.get("event", "an impending event")
                                interrupt_note = (
                                    f"[TIMER INTERRUPTED] The player acted before the timed event fired. "
                                    f"Averted event: \"{event_desc}\""
                                )
                                if interrupt_action:
                                    interrupt_note += f" Interruption context: \"{interrupt_action}\""
                                db.session.add(
                                    ZorkTurn(
                                        campaign_id=campaign.id,
                                        user_id=ctx.author.id,
                                        kind="narrator",
                                        content=interrupt_note,
                                    )
                                )
                                db.session.commit()
                        # Non-interruptible timers are left running.

                    player_state = cls.get_player_state(player)
                    action_clean = action.strip().lower()
                    is_thread_channel = isinstance(ctx.channel, discord.Thread)

                    has_character_name = bool(
                        player_state.get("character_name", "").strip()
                    )
                    campaign_has_content = bool((campaign.summary or "").strip())
                    other_players_exist = (
                        ZorkPlayer.query.filter(
                            ZorkPlayer.campaign_id == campaign.id,
                            ZorkPlayer.user_id != ctx.author.id,
                        ).count()
                        > 0
                    )
                    needs_identity = campaign_has_content and not has_character_name
                    is_new_player = not has_character_name and not campaign_has_content

                    if needs_identity:
                        return (
                            "This campaign already has adventurers. "
                            f"Set your identity first with `{command_prefix}zork identity <name>`. "
                            "Then return to the adventure."
                        )

                    # Deterministic onboarding in non-thread channels: bypass LLM until explicit choice.
                    onboarding_state = player_state.get("onboarding_state")
                    party_status = player_state.get("party_status")
                    if not is_thread_channel:
                        if not party_status and not onboarding_state:
                            player_state["onboarding_state"] = "await_party_choice"
                            player.state_json = cls._dump_json(player_state)
                            player.updated = db.func.now()
                            db.session.commit()
                            return (
                                "Mission rejected until path is selected. Reply with exactly one option:\n"
                                f"- `{cls.MAIN_PARTY_TOKEN}`\n"
                                f"- `{cls.NEW_PATH_TOKEN}`"
                            )

                        if onboarding_state == "await_party_choice":
                            if action_clean == cls.MAIN_PARTY_TOKEN:
                                player_state["party_status"] = "main_party"
                                player_state["onboarding_state"] = None
                                player.state_json = cls._dump_json(player_state)
                                player.updated = db.func.now()
                                db.session.commit()
                                return "Joined main party. Your next message will be treated as an in-world action."

                            if action_clean == cls.NEW_PATH_TOKEN:
                                player_state["onboarding_state"] = "await_campaign_name"
                                player.state_json = cls._dump_json(player_state)
                                player.updated = db.func.now()
                                db.session.commit()
                                options = cls._build_campaign_suggestion_text(
                                    ctx.guild.id
                                )
                                return (
                                    "Reply next with your campaign name (letters/numbers/spaces).\n"
                                    f"{options}\n"
                                    f"Hint: `{command_prefix}zork thread <name>` also creates your own path thread."
                                )

                            return (
                                "Mission rejected. Reply with exactly one option:\n"
                                f"- `{cls.MAIN_PARTY_TOKEN}`\n"
                                f"- `{cls.NEW_PATH_TOKEN}`"
                            )

                        if onboarding_state == "await_campaign_name":
                            campaign_name = cls._sanitize_campaign_name_text(action)
                            if not campaign_name:
                                return "Mission rejected. Reply with a campaign name using letters/numbers/spaces."
                            if len(campaign_name) < 2:
                                return "Mission rejected. Campaign name must be at least 2 characters."
                            if not isinstance(ctx.channel, discord.TextChannel):
                                return f"Could not create a new path thread here. Use `{command_prefix}zork thread {campaign_name}`."
                            thread_name = f"zork-{campaign_name}"[:90]
                            try:
                                thread = await ctx.channel.create_thread(
                                    name=thread_name,
                                    type=discord.ChannelType.public_thread,
                                    auto_archive_duration=1440,
                                )
                            except Exception as e:
                                return f"Could not create path thread: {e}"

                            thread_channel, _ = cls.enable_channel(
                                ctx.guild.id, thread.id, ctx.author.id
                            )
                            thread_campaign, _, _ = cls.set_active_campaign(
                                thread_channel,
                                ctx.guild.id,
                                campaign_name,
                                ctx.author.id,
                                enforce_activity_window=False,
                            )
                            thread_player = cls.get_or_create_player(
                                thread_campaign.id,
                                ctx.author.id,
                                campaign=thread_campaign,
                            )
                            thread_state = cls.get_player_state(thread_player)
                            thread_state = cls._copy_identity_fields(
                                player_state, thread_state
                            )
                            thread_state["party_status"] = "new_path"
                            thread_state["onboarding_state"] = None
                            thread_player.state_json = cls._dump_json(thread_state)
                            thread_player.updated = db.func.now()

                            player_state["party_status"] = "new_path"
                            player_state["onboarding_state"] = None
                            player.state_json = cls._dump_json(player_state)
                            player.updated = db.func.now()
                            db.session.commit()
                            return (
                                f"Created your path thread: {thread.mention}\n"
                                f"Campaign: `{thread_campaign.name}`\n"
                                "Continue your adventure there."
                            )

                    if action_clean in ("look", "l") and (
                        player_state.get("room_description")
                        or player_state.get("room_summary")
                    ):
                        title = (
                            player_state.get("room_title")
                            or player_state.get("location")
                            or "Unknown"
                        )
                        desc = (
                            player_state.get("room_description")
                            or player_state.get("room_summary")
                            or ""
                        )
                        exits = player_state.get("exits")
                        exits_text = f"\nExits: {', '.join(exits)}" if exits else ""
                        narration = f"{title}\n{desc}{exits_text}"
                        inventory_line = cls._format_inventory(player_state)
                        if inventory_line:
                            narration = f"{narration}\n\n{inventory_line}"
                        narration = cls._trim_text(narration, cls.MAX_NARRATION_CHARS)
                        db.session.add(
                            ZorkTurn(
                                campaign_id=campaign.id,
                                user_id=ctx.author.id,
                                kind="player",
                                content=action,
                            )
                        )
                        db.session.add(
                            ZorkTurn(
                                campaign_id=campaign.id,
                                user_id=ctx.author.id,
                                kind="narrator",
                                content=narration,
                            )
                        )
                        campaign.last_narration = narration
                        campaign.updated = db.func.now()
                        db.session.commit()
                        return narration
                    if action_clean in ("inventory", "inv", "i"):
                        narration = (
                            cls._format_inventory(player_state) or "Inventory: empty"
                        )
                        narration = cls._trim_text(narration, cls.MAX_NARRATION_CHARS)
                        db.session.add(
                            ZorkTurn(
                                campaign_id=campaign.id,
                                user_id=ctx.author.id,
                                kind="player",
                                content=action,
                            )
                        )
                        db.session.add(
                            ZorkTurn(
                                campaign_id=campaign.id,
                                user_id=ctx.author.id,
                                kind="narrator",
                                content=narration,
                            )
                        )
                        campaign.last_narration = narration
                        campaign.updated = db.func.now()
                        db.session.commit()
                        return narration

                    turns = cls.get_recent_turns(campaign.id)
                    party_snapshot = cls._build_party_snapshot_for_prompt(
                        campaign, player, player_state
                    )
                    system_prompt, user_prompt = cls.build_prompt(
                        campaign,
                        player,
                        action,
                        turns,
                        party_snapshot=party_snapshot,
                        is_new_player=is_new_player,
                    )
                    if timer_interrupt_context:
                        user_prompt = (
                            f"{user_prompt}\n"
                            f"TIMER_INTERRUPTED: The player acted before a timed event fired.\n"
                            f"The interrupted event was: \"{timer_interrupt_context}\"\n"
                            f"The player's action that interrupted it: \"{action}\"\n"
                            f"Incorporate the interruption naturally into your narration.\n"
                        )
                    if action_clean in ("time skip", "time-skip", "timeskip"):
                        user_prompt = (
                            f"{user_prompt}\n"
                            "TIME_SKIP: The player requests a time skip. Fast-forward past "
                            "any idle, repetitive, or low-stakes moments and jump ahead to "
                            "the next meaningful story beat — a new encounter, discovery, "
                            "twist, or decision point. Summarise skipped time in one brief "
                            "sentence, then narrate the new moment in full.\n"
                        )
                    gpt = GPT()
                    _zork_log(
                        f"TURN START campaign={campaign.id}",
                        f"--- SYSTEM PROMPT ---\n{system_prompt}\n\n--- USER PROMPT ---\n{user_prompt}",
                    )
                    response = await gpt.turbo_completion(
                        system_prompt, user_prompt, temperature=0.8, max_tokens=2048
                    )
                    if not response:
                        response = "A hollow silence answers. Try again."
                    else:
                        response = cls._clean_response(response)
                    _zork_log("INITIAL API RESPONSE", response)

                    # --- Tool-call detection (memory_search / set_timer) ---
                    json_text_tc = cls._extract_json(response)
                    if json_text_tc:
                        try:
                            first_payload = json.loads(json_text_tc)
                        except Exception:
                            first_payload = None
                        if first_payload and cls._is_tool_call(first_payload):
                            tool_name = str(first_payload.get("tool_call") or "").strip()

                            if tool_name == "memory_search":
                                # Support both "queries": [...] and legacy "query": "..."
                                raw_queries = first_payload.get("queries") or []
                                if not raw_queries:
                                    legacy = str(first_payload.get("query") or "").strip()
                                    if legacy:
                                        raw_queries = [legacy]
                                queries = [str(q).strip() for q in raw_queries if str(q).strip()]
                                if queries:
                                    _zork_log("MEMORY SEARCH", f"queries={queries}")
                                    recall_sections = []
                                    seen_turn_ids = set()
                                    for query in queries:
                                        logger.info(
                                            "Zork memory search requested: campaign=%s query=%r",
                                            campaign.id,
                                            query,
                                        )
                                        results = ZorkMemory.search(query, campaign.id, top_k=5)
                                        if results:
                                            top_score = max(s for _, _, _, s in results)
                                            _zork_log(
                                                f"MEMORY SCORES query={query!r}",
                                                "\n".join(
                                                    f"  turn={tid} score={s:.3f} {c[:80]}"
                                                    for tid, _, c, s in results
                                                ),
                                            )
                                        else:
                                            top_score = 0.0
                                        # Keep only results above relevance threshold.
                                        relevant = [
                                            (turn_id, kind, content, score)
                                            for turn_id, kind, content, score in results
                                            if score >= 0.35 and turn_id not in seen_turn_ids
                                        ]
                                        # Sort chronologically so the model sees events in order.
                                        relevant.sort(key=lambda t: t[0])
                                        recall_lines = []
                                        for turn_id, kind, content, score in relevant:
                                            seen_turn_ids.add(turn_id)
                                            recall_lines.append(
                                                f"- [{kind} turn {turn_id}, relevance {score:.2f}]: {content[:300]}"
                                            )
                                        if recall_lines:
                                            recall_sections.append(
                                                f"Results for '{query}':\n" + "\n".join(recall_lines)
                                            )
                                    if recall_sections:
                                        recall_block = (
                                            "MEMORY_RECALL (results from memory_search):\n"
                                            + "\n".join(recall_sections)
                                        )
                                    else:
                                        recall_block = "MEMORY_RECALL: No relevant memories found."
                                    _zork_log("MEMORY RECALL BLOCK", recall_block)
                                    augmented_prompt = f"{user_prompt}\n{recall_block}\n"
                                    response = await gpt.turbo_completion(
                                        system_prompt,
                                        augmented_prompt,
                                        temperature=0.8,
                                        max_tokens=2048,
                                    )
                                    if not response:
                                        response = "A hollow silence answers. Try again."
                                    else:
                                        response = cls._clean_response(response)
                                    _zork_log("AUGMENTED API RESPONSE", response)

                            elif tool_name == "set_timer" and cls.is_timed_events_enabled(campaign):
                                raw_delay = first_payload.get("delay_seconds", 60)
                                try:
                                    delay_seconds = int(raw_delay)
                                except (TypeError, ValueError):
                                    delay_seconds = 60
                                delay_seconds = max(30, min(300, delay_seconds))
                                event_description = str(
                                    first_payload.get("event_description") or "Something happens."
                                ).strip()[:500]

                                cls.cancel_pending_timer(campaign.id)
                                channel_id = ctx.channel.id
                                cls._schedule_timer(
                                    campaign.id, channel_id, delay_seconds, event_description
                                )
                                timer_scheduled_delay = delay_seconds
                                timer_scheduled_event = event_description
                                logger.info(
                                    "Zork timer set: campaign=%s delay=%ds event=%r",
                                    campaign.id,
                                    delay_seconds,
                                    event_description,
                                )
                                timer_block = (
                                    f"TIMER_SET (system confirmation): A timed event has been scheduled.\n"
                                    f'In {delay_seconds} seconds, if the player has not acted: "{event_description}".\n'
                                    f"Now narrate the current scene. You MUST mention the time pressure\n"
                                    f"and tell the player approximately how long they have."
                                )
                                _zork_log(
                                    "TIMER TOOL CALL",
                                    f"delay={delay_seconds}s event={event_description!r}",
                                )
                                augmented_prompt = f"{user_prompt}\n{timer_block}\n"
                                response = await gpt.turbo_completion(
                                    system_prompt,
                                    augmented_prompt,
                                    temperature=0.8,
                                    max_tokens=2048,
                                )
                                if not response:
                                    response = "A hollow silence answers. Try again."
                                else:
                                    response = cls._clean_response(response)

                        # Fallback: LLM returned set_timer alongside narration.
                        # _is_tool_call rejects that, but we still honour the timer.
                        elif (
                            first_payload
                            and isinstance(first_payload, dict)
                            and str(first_payload.get("tool_call") or "").strip() == "set_timer"
                            and "narration" in first_payload
                            and cls.is_timed_events_enabled(campaign)
                        ):
                            raw_delay = first_payload.get("delay_seconds", 60)
                            try:
                                delay_seconds = int(raw_delay)
                            except (TypeError, ValueError):
                                delay_seconds = 60
                            delay_seconds = max(30, min(300, delay_seconds))
                            event_description = str(
                                first_payload.get("event_description") or "Something happens."
                            ).strip()[:500]

                            cls.cancel_pending_timer(campaign.id)
                            channel_id = ctx.channel.id
                            cls._schedule_timer(
                                campaign.id, channel_id, delay_seconds, event_description
                            )
                            timer_scheduled_delay = delay_seconds
                            timer_scheduled_event = event_description
                            logger.info(
                                "Zork timer set (with narration): campaign=%s delay=%ds event=%r",
                                campaign.id,
                                delay_seconds,
                                event_description,
                            )

                    narration = response.strip()
                    state_update = {}
                    summary_update = None
                    xp_awarded = 0
                    player_state_update = {}
                    scene_image_prompt = None
                    character_updates = {}
                    timer_scheduled_delay = None
                    timer_scheduled_event = None
                    timer_scheduled_interruptible = True

                    json_text = cls._extract_json(response)
                    if json_text:
                        try:
                            payload = cls._parse_json_lenient(json_text)
                            narration = payload.get("narration", narration).strip()
                            state_update = payload.get("state_update", {}) or {}
                            summary_update = payload.get("summary_update")
                            xp_awarded = payload.get("xp_awarded", 0) or 0
                            player_state_update = (
                                payload.get("player_state_update", {}) or {}
                            )
                            scene_image_prompt = payload.get("scene_image_prompt")
                            character_updates = payload.get("character_updates", {}) or {}

                            # Inline timed event fields.
                            inline_timer_delay = payload.get("set_timer_delay")
                            inline_timer_event = payload.get("set_timer_event")
                            if (
                                inline_timer_delay is not None
                                and inline_timer_event
                                and cls.is_timed_events_enabled(campaign)
                            ):
                                # Block new timer if one is already running.
                                existing_timer = cls._pending_timers.get(campaign.id)
                                if existing_timer is not None:
                                    _zork_log(
                                        f"TIMER REJECTED campaign={campaign.id}",
                                        f"Existing timer still active — model tried to set a new one.\n"
                                        f"Existing event: {existing_timer.get('event')!r}\n"
                                        f"Rejected event: {str(inline_timer_event).strip()[:500]!r}",
                                    )
                                else:
                                    try:
                                        t_delay = int(inline_timer_delay)
                                    except (TypeError, ValueError):
                                        t_delay = 60
                                    t_delay = max(30, min(300, t_delay))
                                    t_event = str(inline_timer_event).strip()[:500]
                                    t_interruptible = bool(
                                        payload.get("set_timer_interruptible", True)
                                    )
                                    t_interrupt_action = payload.get("set_timer_interrupt_action")
                                    if isinstance(t_interrupt_action, str):
                                        t_interrupt_action = t_interrupt_action.strip()[:500] or None
                                    else:
                                        t_interrupt_action = None
                                    cls._schedule_timer(
                                        campaign.id, ctx.channel.id, t_delay, t_event,
                                        interruptible=t_interruptible,
                                        interrupt_action=t_interrupt_action,
                                    )
                                    timer_scheduled_delay = t_delay
                                    timer_scheduled_event = t_event
                                    timer_scheduled_interruptible = t_interruptible
                                    _zork_log(
                                        f"TIMER SET campaign={campaign.id}",
                                        f"delay={t_delay}s event={t_event!r} "
                                        f"interruptible={t_interruptible}",
                                    )
                                    logger.info(
                                        "Zork timer set (inline): campaign=%s delay=%ds event=%r interruptible=%s",
                                        campaign.id,
                                        t_delay,
                                        t_event,
                                        t_interruptible,
                                    )
                        except Exception as e:
                            logger.warning(f"Failed to parse Zork JSON response: {e}")

                    # Safety: if narration still looks like raw JSON, something
                    # went wrong during parsing.  Try to salvage the narration
                    # key so raw JSON never leaks to Discord or stored turns.
                    if narration.lstrip().startswith("{"):
                        try:
                            salvage = json.loads(
                                cls._extract_json(narration) or "{}"
                            )
                            if isinstance(salvage, dict) and salvage:
                                narration = str(
                                    salvage.get("narration", "")
                                ).strip() or "The world shifts, but nothing clear emerges."
                        except (json.JSONDecodeError, Exception):
                            narration = "The world shifts, but nothing clear emerges."

                    raw_narration = narration
                    narration = cls._trim_text(narration, cls.MAX_NARRATION_CHARS)
                    narration = cls._strip_inventory_from_narration(narration)

                    _zork_log(
                        f"TURN RESULT campaign={campaign.id}",
                        f"--- NARRATION ---\n{narration}\n\n"
                        f"--- STATE UPDATE ---\n{json.dumps(state_update, indent=2)}\n\n"
                        f"--- PLAYER STATE UPDATE ---\n{json.dumps(player_state_update, indent=2)}\n\n"
                        f"--- SUMMARY UPDATE ---\n{summary_update}\n\n"
                        f"--- XP AWARDED ---\n{xp_awarded}\n"
                        f"--- SCENE IMAGE PROMPT ---\n{scene_image_prompt}\n",
                    )

                    state_update, player_state_update = cls._split_room_state(
                        state_update, player_state_update
                    )
                    state_update = cls._scrub_inventory_from_state(state_update)

                    campaign_state = cls.get_campaign_state(campaign)
                    campaign_state = cls._apply_state_update(
                        campaign_state, state_update
                    )
                    campaign_state = cls._scrub_inventory_from_state(campaign_state)
                    campaign.state_json = cls._dump_json(campaign_state)

                    if character_updates and isinstance(character_updates, dict):
                        existing_chars = cls.get_campaign_characters(campaign)
                        existing_chars = cls._apply_character_updates(
                            existing_chars, character_updates
                        )
                        campaign.characters_json = cls._dump_json(existing_chars)
                        _zork_log(
                            f"CHARACTER UPDATES campaign={campaign.id}",
                            json.dumps(character_updates, indent=2),
                        )

                    if summary_update:
                        summary_update = summary_update.strip()
                        summary_update = cls._strip_inventory_mentions(summary_update)
                        campaign.summary = cls._append_summary(
                            campaign.summary, summary_update
                        )

                    player_state = cls.get_player_state(player)
                    player_state_update = cls._sanitize_player_state_update(
                        player_state,
                        player_state_update,
                        action_text=action,
                        narration_text=raw_narration,
                    )
                    player_state = cls._apply_state_update(
                        player_state, player_state_update
                    )
                    player.state_json = cls._dump_json(player_state)

                    if isinstance(xp_awarded, int) and xp_awarded > 0:
                        player.xp += xp_awarded

                    inventory_line = (
                        cls._format_inventory(player_state) or "Inventory: empty"
                    )
                    if narration:
                        narration = f"{narration}\n\n{inventory_line}"
                    else:
                        narration = inventory_line

                    if timer_scheduled_delay is not None:
                        expiry_ts = int(time.time()) + timer_scheduled_delay
                        event_hint = timer_scheduled_event or "Something happens"
                        if timer_scheduled_interruptible:
                            interrupt_hint = "act to prevent!"
                        else:
                            interrupt_hint = "unavoidable"
                        narration = (
                            f"{narration}\n\n"
                            f"\u23f0 <t:{expiry_ts}:R>: {event_hint} ({interrupt_hint})"
                        )

                    campaign.last_narration = narration
                    campaign.updated = db.func.now()
                    player.updated = db.func.now()

                    # Don't store OOC meta-messages in turn history.
                    _is_ooc = bool(re.match(r"\s*\[OOC\b", action, re.IGNORECASE))
                    if not _is_ooc:
                        db.session.add(
                            ZorkTurn(
                                campaign_id=campaign.id,
                                user_id=ctx.author.id,
                                kind="player",
                                content=action,
                            )
                        )
                    narrator_turn = ZorkTurn(
                        campaign_id=campaign.id,
                        user_id=ctx.author.id,
                        kind="narrator",
                        content=narration,
                    )
                    db.session.add(narrator_turn)
                    db.session.commit()

                    # Fire-and-forget: embed the narrator turn for memory search.
                    try:
                        ZorkMemory.store_turn_embedding(
                            narrator_turn.id,
                            campaign.id,
                            ctx.author.id,
                            "narrator",
                            narration,
                        )
                    except Exception:
                        logger.debug(
                            "Zork memory embedding skipped for turn %s",
                            narrator_turn.id,
                            exc_info=True,
                        )

                    if isinstance(scene_image_prompt, str):
                        refreshed_party_snapshot = cls._build_party_snapshot_for_prompt(
                            campaign, player, player_state
                        )
                        cleaned_scene_prompt = cls._enrich_scene_image_prompt(
                            scene_image_prompt,
                            player_state=player_state,
                            party_snapshot=refreshed_party_snapshot,
                        )
                        if cleaned_scene_prompt:
                            await cls._enqueue_scene_image(
                                ctx,
                                cleaned_scene_prompt,
                                campaign_id=campaign.id,
                                room_key=cls._room_key_from_player_state(player_state),
                            )

                    return narration
        finally:
            if should_clear_claim:
                cls._clear_inflight_turn(campaign_id, ctx.author.id)

    @classmethod
    def _schedule_timer(
        cls,
        campaign_id: int,
        channel_id: int,
        delay_seconds: int,
        event_description: str,
        interruptible: bool = True,
        interrupt_action: Optional[str] = None,
    ):
        task = asyncio.create_task(
            cls._timer_task(campaign_id, channel_id, delay_seconds, event_description)
        )
        cls._pending_timers[campaign_id] = {
            "task": task,
            "channel_id": channel_id,
            "message_id": None,
            "event": event_description,
            "delay": delay_seconds,
            "interruptible": interruptible,
            "interrupt_action": interrupt_action,
        }

    @classmethod
    async def _timer_task(
        cls,
        campaign_id: int,
        channel_id: int,
        delay_seconds: int,
        event_description: str,
    ):
        try:
            await asyncio.sleep(delay_seconds)
        except asyncio.CancelledError:
            return
        timer_ctx = cls._pending_timers.pop(campaign_id, None)
        # Edit the original message to replace live countdown.
        if timer_ctx:
            msg_id = timer_ctx.get("message_id")
            ch_id = timer_ctx.get("channel_id")
            if msg_id and ch_id:
                asyncio.ensure_future(
                    cls._edit_timer_line(ch_id, msg_id, f"\u26a0\ufe0f *Timer expired — {event_description}*")
                )
        try:
            await cls._execute_timed_event(campaign_id, channel_id, event_description)
        except Exception:
            logger.exception(
                "Zork timed event failed: campaign=%s event=%r",
                campaign_id,
                event_description,
            )

    @classmethod
    async def _execute_timed_event(
        cls,
        campaign_id: int,
        channel_id: int,
        event_description: str,
    ):
        app = AppConfig.get_flask()
        if app is None:
            return
        lock = cls._get_lock(campaign_id)
        async with lock:
            with app.app_context():
                campaign = ZorkCampaign.query.get(campaign_id)
                if campaign is None:
                    return
                if not cls.is_timed_events_enabled(campaign):
                    return

                # Safety: skip if a player acted very recently (race guard).
                latest_turn = (
                    ZorkTurn.query.filter_by(campaign_id=campaign_id)
                    .order_by(ZorkTurn.id.desc())
                    .first()
                )
                if latest_turn and latest_turn.kind == "player":
                    if latest_turn.created:
                        age = (datetime.datetime.utcnow() - latest_turn.created).total_seconds()
                        if age < 5:
                            return

                # Find most recently active player for context.
                active_player = (
                    ZorkPlayer.query.filter_by(campaign_id=campaign_id)
                    .order_by(ZorkPlayer.last_active.desc())
                    .first()
                )
                if active_player is None:
                    return

                cls.increment_player_stat(
                    active_player, cls.PLAYER_STATS_TIMERS_MISSED_KEY
                )
                active_player.updated = db.func.now()
                db.session.commit()
                action = f"[SYSTEM EVENT - TIMED]: {event_description}"
                turns = cls.get_recent_turns(campaign_id)
                system_prompt, user_prompt = cls.build_prompt(
                    campaign,
                    active_player,
                    action,
                    turns,
                    is_new_player=False,
                )

                gpt = GPT()
                response = await gpt.turbo_completion(
                    system_prompt, user_prompt, temperature=0.8, max_tokens=2048
                )
                if not response:
                    return
                response = cls._clean_response(response)

                narration = response.strip()
                state_update = {}
                summary_update = None
                xp_awarded = 0
                player_state_update = {}
                character_updates = {}

                json_text = cls._extract_json(response)
                if json_text:
                    try:
                        payload = cls._parse_json_lenient(json_text)
                        narration = payload.get("narration", narration).strip()
                        state_update = payload.get("state_update", {}) or {}
                        summary_update = payload.get("summary_update")
                        xp_awarded = payload.get("xp_awarded", 0) or 0
                        player_state_update = payload.get("player_state_update", {}) or {}
                        character_updates = payload.get("character_updates", {}) or {}
                    except Exception as e:
                        logger.warning(f"Failed to parse timed event JSON response: {e}")

                if narration.lstrip().startswith("{"):
                    try:
                        salvage = json.loads(
                            cls._extract_json(narration) or "{}"
                        )
                        if isinstance(salvage, dict) and salvage:
                            narration = str(
                                salvage.get("narration", "")
                            ).strip() or "The world shifts, but nothing clear emerges."
                    except (json.JSONDecodeError, Exception):
                        narration = "The world shifts, but nothing clear emerges."

                narration = cls._trim_text(narration, cls.MAX_NARRATION_CHARS)
                narration = cls._strip_inventory_from_narration(narration)

                state_update, player_state_update = cls._split_room_state(
                    state_update, player_state_update
                )
                state_update = cls._scrub_inventory_from_state(state_update)

                campaign_state = cls.get_campaign_state(campaign)
                campaign_state = cls._apply_state_update(campaign_state, state_update)
                campaign_state = cls._scrub_inventory_from_state(campaign_state)
                campaign.state_json = cls._dump_json(campaign_state)

                if character_updates and isinstance(character_updates, dict):
                    existing_chars = cls.get_campaign_characters(campaign)
                    existing_chars = cls._apply_character_updates(
                        existing_chars, character_updates
                    )
                    campaign.characters_json = cls._dump_json(existing_chars)
                    _zork_log(
                        f"CHARACTER UPDATES (timed event) campaign={campaign.id}",
                        json.dumps(character_updates, indent=2),
                    )

                if summary_update:
                    summary_update = summary_update.strip()
                    summary_update = cls._strip_inventory_mentions(summary_update)
                    campaign.summary = cls._append_summary(
                        campaign.summary, summary_update
                    )

                player_state = cls.get_player_state(active_player)
                player_state_update = cls._sanitize_player_state_update(
                    player_state,
                    player_state_update,
                    action_text=action,
                    narration_text=narration,
                )
                player_state = cls._apply_state_update(player_state, player_state_update)
                active_player.state_json = cls._dump_json(player_state)

                if isinstance(xp_awarded, int) and xp_awarded > 0:
                    active_player.xp += xp_awarded

                campaign.last_narration = narration
                campaign.updated = db.func.now()
                active_player.updated = db.func.now()

                narrator_turn = ZorkTurn(
                    campaign_id=campaign.id,
                    user_id=None,
                    kind="narrator",
                    content=f"[TIMED EVENT] {narration}",
                )
                db.session.add(narrator_turn)
                db.session.commit()

                target_user_id = active_player.user_id

        # Post to Discord outside the lock / app context.
        bot_instance = DiscordBot.get_instance()
        if bot_instance is None:
            return
        channel = await bot_instance.find_channel(channel_id)
        if channel is None:
            return
        mention = f"<@{target_user_id}>" if target_user_id else ""
        output = f"**[Timed Event]** {mention}\n{narration}"
        await DiscordBot.send_large_message(channel, output)

    @classmethod
    async def generate_map(cls, ctx, command_prefix: str = "!") -> str:
        app = AppConfig.get_flask()
        if app is None:
            raise RuntimeError("Flask app not initialized; cannot use ZorkEmulator.")

        with app.app_context():
            channel = cls.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if not channel.enabled:
                return f"Adventure mode is disabled in this channel. Run `{command_prefix}zork` to enable it."
            if channel.active_campaign_id is None:
                _, campaign = cls.enable_channel(
                    ctx.guild.id, ctx.channel.id, ctx.author.id
                )
            else:
                campaign = ZorkCampaign.query.get(channel.active_campaign_id)
                if campaign is None:
                    _, campaign = cls.enable_channel(
                        ctx.guild.id, ctx.channel.id, ctx.author.id
                    )
            campaign_id = campaign.id

        with app.app_context():
            campaign = ZorkCampaign.query.get(campaign_id)
            player = cls.get_or_create_player(
                campaign_id, ctx.author.id, campaign=campaign
            )
            player_state = cls.get_player_state(player)
            room_summary = player_state.get("room_summary")
            room_title = player_state.get("room_title")
            exits = player_state.get("exits")

            if not room_summary and not room_title:
                return "No map data yet. Try `look` first."

            other_players = (
                ZorkPlayer.query.filter_by(campaign_id=campaign.id)
                .order_by(ZorkPlayer.user_id.asc())
                .all()
            )
            marker_data = cls._assign_player_markers(other_players, ctx.author.id)
            other_entries = []
            for entry in marker_data:
                other = entry["player"]
                other_state = cls.get_player_state(other)
                other_room = (
                    other_state.get("room_summary")
                    or other_state.get("room_title")
                    or other_state.get("location")
                )
                if not other_room:
                    continue
                other_name = (
                    other_state.get("character_name")
                    or f"Adventurer-{str(other.user_id)[-4:]}"
                )
                other_entries.append(
                    {
                        "marker": entry["marker"],
                        "user_id": other.user_id,
                        "character_name": other_name,
                        "room": other_room,
                        "party_status": other_state.get("party_status"),
                    }
                )

            player_name = (
                player_state.get("character_name")
                or f"Adventurer-{str(ctx.author.id)[-4:]}"
            )
            map_prompt = (
                f"CAMPAIGN: {campaign.name}\n"
                f"PLAYER_NAME: {player_name}\n"
                f"PLAYER_ROOM_TITLE: {room_title or 'Unknown'}\n"
                f"PLAYER_ROOM_SUMMARY: {room_summary or ''}\n"
                f"PLAYER_EXITS: {exits or []}\n"
                f"WORLD_SUMMARY: {cls._trim_text(campaign.summary or '', 1200)}\n"
                f"OTHER_PLAYERS: {cls._dump_json(other_entries)}\n"
                "Draw a compact map with @ marking the player's location.\n"
            )
            gpt = GPT()
            response = await gpt.turbo_completion(
                cls.MAP_SYSTEM_PROMPT,
                map_prompt,
                temperature=0.2,
                max_tokens=400,
            )
            ascii_map = cls._extract_ascii_map(response)
            if not ascii_map:
                return "Map is foggy. Try again."
            return ascii_map
