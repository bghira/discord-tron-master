import ast
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
from discord_tron_master.classes.openai.tokens import glm_token_count
from discord_tron_master.classes.zork_memory import ZorkMemory
from discord_tron_master.bot import DiscordBot
from discord_tron_master.models.base import db
from discord_tron_master.models.zork import (
    ZorkCampaign,
    ZorkChannel,
    ZorkPlayer,
    ZorkSnapshot,
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
    ATTACHMENT_MAX_BYTES = 500_000
    ATTACHMENT_CHUNK_TOKENS = 2_000       # minimum tokens per chunk
    ATTACHMENT_MODEL_CTX_TOKENS = 200_000 # GLM-5 context window
    ATTACHMENT_PROMPT_OVERHEAD_TOKENS = 6_000  # reserve for system + user + IMDB + storyline JSON
    ATTACHMENT_RESPONSE_RESERVE_TOKENS = 4_000 # max_tokens used by finalize response
    ATTACHMENT_MAX_PARALLEL = 4
    ATTACHMENT_GUARD_TOKEN = "--COMPLETED SUMMARY--"
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
    MODEL_STATE_EXCLUDE_KEYS = ROOM_STATE_KEYS | {
        "last_narration",
        "room_scene_images",
        "scene_image_model",
        "default_persona",
        "start_room",
        "story_outline",
        "current_chapter",
        "current_scene",
        "setup_phase",
        "setup_data",
        "speed_multiplier",
        "game_time",
        "calendar",
    }
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
        "- give_item: object (REQUIRED when the acting player gives/hands/passes an item to another player character. "
        "Keys: 'item' (string, exact item name from acting player's inventory), "
        "'to_discord_mention' (string, discord_mention of the recipient from PARTY_SNAPSHOT, e.g. '<@123456>'). "
        "The emulator handles removing from the giver and adding to the recipient automatically. "
        "Do NOT use inventory_remove for the given item — give_item handles both sides. "
        "Only use when both players are in the same room per PARTY_SNAPSHOT. Only one item per turn.)\n"
        "- calendar_update: object (optional; see CALENDAR & GAME TIME SYSTEM below)\n"
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
        "- NEVER include any inventory listing, summary, or 'Inventory:' line in narration. The emulator appends authoritative inventory automatically. "
        "Do not list, enumerate, or summarise what the player is carrying anywhere in the narration text — not at the end, not inline, not as a parenthetical.\n"
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
        "- CRITICAL — OTHER PLAYER CHARACTERS ARE OFF-LIMITS:\n"
        "  PARTY_SNAPSHOT entries (except the acting player) are REAL HUMANS controlling their own characters.\n"
        "  You MUST NOT write ANY of the following for another player character:\n"
        "    * Dialogue or quoted speech\n"
        "    * Actions, movements, or decisions (e.g. 'she draws her sword', 'he follows you')\n"
        "    * Emotional reactions, facial expressions, or gestures in response to events\n"
        "    * Plot advancement involving them (e.g. 'together you storm the gate')\n"
        "    * Moving them to a new location or changing their state in any way\n"
        "  You MAY reference another player character in two cases:\n"
        "    1. Static presence — note they are in the room (e.g. 'X is here'), nothing more.\n"
        "    2. Continuing a prior action — if RECENT_TURNS shows that player ALREADY performed an action on their own turn\n"
        "       (e.g. 'I toss the key to you', 'I hold the door open'), you may narrate the CONSEQUENCE of that\n"
        "       established action as it affects the acting player (e.g. 'You catch the key X tossed'). \n"
        "       You are acknowledging what they did, not inventing new behaviour for them.\n"
        "  In ALL other cases, treat other player characters as scenery — they exist but do nothing until THEY act.\n"
        "  This turn's narration concerns ONLY the acting player identified by PLAYER_ACTION.\n"
        "- When mentioning a player character in narration, use their Discord mention from PARTY_SNAPSHOT followed by their name in parentheses, e.g. '<@123456> (Bruce Wayne)'. This pings the player in Discord so they know they were referenced.\n"
        "- NEVER skip or fast-forward time when a player sleeps, rests, or waits. Narrate only the moment of settling in (closing eyes, finding a spot to rest). Do NOT write 'hours pass', 'you wake at dawn', or advance to morning/next day. Other players share this world and time must not jump for one player's action. End the turn in the present moment.\n"
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
        'NOT: {"tool_call": "memory_search", "queries": ["Marcus Anastasia relationship"]}\n'
        "USE memory_search AGGRESSIVELY — it is cheap and fast. Prefer searching too often over guessing.\n"
        "You SHOULD use memory_search on MOST turns. Specifically:\n"
        "- ANY time a character, NPC, or named entity appears or is mentioned — even if they were in recent turns. "
        "Memory may contain richer detail than the truncated recent context.\n"
        "- ANY time the player references past events, locations, objects, or conversations.\n"
        "- ANY time you are about to narrate a scene involving an established NPC — search their name first.\n"
        "- ANY time you need to describe a location the player has visited before.\n"
        "- At the START of most turns, search for the current location and any NPCs present to refresh your context.\n"
        "- When the player asks questions, investigates, or examines something — search for related terms.\n"
        "- When you are unsure about ANY detail from earlier in the campaign.\n"
        "The cost of an unnecessary search is zero. The cost of hallucinating a detail is broken continuity.\n"
        "When in doubt, SEARCH. Do not guess, improvise, or rely solely on RECENT_TURNS.\n"
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
        "- Your narration should hint at urgency narratively (e.g. 'the footsteps grow louder') but NEVER include countdowns, timestamps, emoji clocks, or explicit seconds. The system adds its own countdown display automatically.\n"
        "- Use at least once every few turns when dramatic pacing allows. Do not use on consecutive turns.\n"
    )

    ON_RAILS_SYSTEM_PROMPT = (
        "\nON-RAILS MODE IS ENABLED.\n"
        "- You CANNOT create new characters not in WORLD_CHARACTERS. New character slugs will be rejected.\n"
        "- You CANNOT introduce locations/landmarks not in story_outline or landmarks list.\n"
        "- You CANNOT add new chapters or scenes beyond STORY_CONTEXT.\n"
        "- You MUST advance along the current chapter/scene trajectory.\n"
        "- Adjust pacing/details within scenes, but major plot points must match the outline.\n"
        "- Use state_update.current_chapter / state_update.current_scene to advance.\n"
        "- If player tries to derail, steer back via NPC actions or environmental events.\n"
    )
    STORY_OUTLINE_TOOL_PROMPT = (
        "\nYou have a story_outline tool. To use it, return ONLY:\n"
        '{"tool_call": "story_outline", "chapter": "chapter-slug"}\n'
        "No other keys alongside tool_call.\n"
        "Returns full expanded chapter with all scene details.\n"
        "Use when you need details about a chapter not fully shown in STORY_CONTEXT.\n"
    )

    CALENDAR_TOOL_PROMPT = (
        "\nCALENDAR & GAME TIME SYSTEM:\n"
        "The campaign tracks in-game time via CURRENT_GAME_TIME shown in the user prompt.\n"
        "Every turn, you MUST advance game_time in state_update by a plausible amount "
        "(minutes for quick actions, hours for travel, etc.). "
        "Scale the advance by SPEED_MULTIPLIER — at 2x, time passes roughly twice as fast per turn.\n"
        "Update these fields in state_update:\n"
        '- "game_time": {"day": int, "hour": int (0-23), "minute": int (0-59), '
        '"period": "morning"|"afternoon"|"evening"|"night", '
        '"date_label": "Day N, Period"}\n'
        "Advance hour/minute naturally; when hour >= 24, increment day and wrap hour.\n"
        "Set period based on hour: 5-11=morning, 12-16=afternoon, 17-20=evening, 21-4=night.\n\n"
        "You may also return a calendar_update key (object) to manage scheduled events:\n"
        '- "calendar_update": {"add": [...], "remove": [...]} where each add entry is '
        '{"name": str, "time_remaining": int, "time_unit": "hours"|"days", "description": str} '
        "and each remove entry is a string matching an event name.\n"
        "The harness enforces max 10 calendar events and auto-prunes expired ones.\n"
        "Use calendar events for approaching deadlines, NPC appointments, world events, "
        "and anything with narrative timing pressure.\n"
    )

    ROSTER_PROMPT = (
        "\nCHARACTER ROSTER & PORTRAITS:\n"
        "The harness maintains a character roster (WORLD_CHARACTERS). "
        "When you create or update a character via character_updates, the 'appearance' field "
        "is used by the harness to auto-generate a portrait image. Write 'appearance' as a "
        "detailed visual description suitable for image generation: physical features, clothing, "
        "distinguishing marks, pose, and art style cues. Keep it 1-3 sentences, "
        "70-150 words, vivid and concrete.\n"
        "Do NOT include image_url in character_updates — the harness manages that field.\n"
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

    # ------------------------------------------------------------------
    # Snapshot / Rewind helpers
    # ------------------------------------------------------------------

    @classmethod
    def _create_snapshot(cls, narrator_turn: "ZorkTurn", campaign: "ZorkCampaign"):
        """Persist a full world-state snapshot tied to *narrator_turn*."""
        try:
            players = ZorkPlayer.query.filter_by(campaign_id=campaign.id).all()
            players_data = [
                {
                    "player_id": p.id,
                    "user_id": p.user_id,
                    "level": p.level,
                    "xp": p.xp,
                    "attributes_json": p.attributes_json,
                    "state_json": p.state_json,
                }
                for p in players
            ]
            snapshot = ZorkSnapshot(
                turn_id=narrator_turn.id,
                campaign_id=campaign.id,
                campaign_state_json=campaign.state_json or "{}",
                campaign_characters_json=campaign.characters_json or "{}",
                campaign_summary=campaign.summary or "",
                campaign_last_narration=campaign.last_narration,
                players_json=json.dumps(players_data),
            )
            db.session.add(snapshot)
            db.session.commit()
        except Exception:
            logger.exception(
                "Zork: failed to create snapshot for turn %s campaign %s",
                narrator_turn.id,
                campaign.id,
            )

    @classmethod
    def record_turn_message_ids(
        cls, campaign_id: int, user_message_id: int, bot_message_id: int
    ):
        """Stamp Discord message IDs onto the most recent narrator + player turn pair."""
        try:
            narrator_turn = (
                ZorkTurn.query.filter_by(campaign_id=campaign_id, kind="narrator")
                .order_by(ZorkTurn.id.desc())
                .first()
            )
            if narrator_turn is not None:
                narrator_turn.discord_message_id = bot_message_id
                narrator_turn.user_message_id = user_message_id

            player_turn = (
                ZorkTurn.query.filter_by(campaign_id=campaign_id, kind="player")
                .order_by(ZorkTurn.id.desc())
                .first()
            )
            if player_turn is not None:
                player_turn.user_message_id = user_message_id

            db.session.commit()
        except Exception:
            logger.exception(
                "Zork: failed to record message IDs for campaign %s", campaign_id
            )

    @classmethod
    def execute_rewind(
        cls, campaign_id: int, target_discord_message_id: int, channel_id: int = None
    ) -> Optional[Tuple[int, int]]:
        """Restore campaign state to the snapshot at *target_discord_message_id*.

        When *channel_id* is provided, only turns from that channel are deleted.
        Returns ``(turn_id, deleted_count)`` on success, or ``None`` if the
        target turn/snapshot could not be found.
        """
        # 1. Find the narrator turn by discord_message_id
        target_turn = ZorkTurn.query.filter_by(
            campaign_id=campaign_id,
            discord_message_id=target_discord_message_id,
        ).first()

        # Fallback: check user_message_id and find companion narrator turn
        if target_turn is None:
            player_turn = ZorkTurn.query.filter_by(
                campaign_id=campaign_id,
                user_message_id=target_discord_message_id,
            ).first()
            if player_turn is not None:
                # Find the narrator turn that immediately follows
                target_turn = (
                    ZorkTurn.query.filter(
                        ZorkTurn.campaign_id == campaign_id,
                        ZorkTurn.kind == "narrator",
                        ZorkTurn.id >= player_turn.id,
                    )
                    .order_by(ZorkTurn.id.asc())
                    .first()
                )

        if target_turn is None:
            return None

        # 2. Load snapshot
        snapshot = ZorkSnapshot.query.filter_by(turn_id=target_turn.id).first()
        if snapshot is None:
            return None

        # 3. Restore campaign state
        campaign = ZorkCampaign.query.get(campaign_id)
        if campaign is None:
            return None

        campaign.state_json = snapshot.campaign_state_json
        campaign.characters_json = snapshot.campaign_characters_json
        campaign.summary = snapshot.campaign_summary
        campaign.last_narration = snapshot.campaign_last_narration
        campaign.updated = db.func.now()

        # 4. Restore player states
        players_data = json.loads(snapshot.players_json)
        for pdata in players_data:
            player = ZorkPlayer.query.get(pdata["player_id"])
            if player is None:
                continue
            player.level = pdata["level"]
            player.xp = pdata["xp"]
            player.attributes_json = pdata["attributes_json"]
            player.state_json = pdata["state_json"]
            player.updated = db.func.now()

        # 5. Build channel-scoped filter for deletion
        turn_filter = [
            ZorkTurn.campaign_id == campaign_id,
            ZorkTurn.id > target_turn.id,
        ]
        if channel_id is not None:
            turn_filter.append(ZorkTurn.channel_id == channel_id)

        # Collect turn IDs to delete so we can remove their snapshots first (FK).
        turn_ids_to_delete = [
            t.id
            for t in ZorkTurn.query.filter(*turn_filter)
            .with_entities(ZorkTurn.id)
            .all()
        ]

        if turn_ids_to_delete:
            ZorkSnapshot.query.filter(
                ZorkSnapshot.turn_id.in_(turn_ids_to_delete),
            ).delete(synchronize_session=False)

            deleted_count = ZorkTurn.query.filter(
                ZorkTurn.id.in_(turn_ids_to_delete),
            ).delete(synchronize_session=False)
        else:
            deleted_count = 0

        db.session.commit()

        # 6. Clean embeddings for deleted turns
        try:
            if channel_id is not None and turn_ids_to_delete:
                conn = ZorkMemory._get_conn()
                placeholders = ",".join("?" for _ in turn_ids_to_delete)
                conn.execute(
                    f"DELETE FROM turn_embeddings WHERE turn_id IN ({placeholders})",
                    turn_ids_to_delete,
                )
                conn.commit()
            else:
                ZorkMemory.delete_turns_after(campaign_id, target_turn.id)
        except Exception:
            logger.debug(
                "Zork rewind: embedding cleanup failed for campaign %s",
                campaign_id,
                exc_info=True,
            )

        return (target_turn.id, deleted_count)

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
                stats[
                    cls.PLAYER_STATS_ATTENTION_SECONDS_KEY
                ] = cls._coerce_non_negative_int(
                    stats.get(cls.PLAYER_STATS_ATTENTION_SECONDS_KEY), 0
                ) + int(
                    gap_seconds
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

    # ── Campaign Setup State Machine ──────────────────────────────────────

    IMDB_SUGGEST_URL = "https://v2.sg.media-imdb.com/suggestion/{first}/{query}.json"
    IMDB_TIMEOUT = 5

    @classmethod
    def _imdb_search_single(cls, query: str, max_results: int = 3) -> List[dict]:
        """Single IMDB suggestion API call. Returns list of result dicts."""
        clean = re.sub(r"[^\w\s]", "", query.strip().lower())
        if not clean:
            return []
        first = clean[0] if clean[0].isalpha() else "a"
        encoded = clean.replace(" ", "_")
        url = cls.IMDB_SUGGEST_URL.format(first=first, query=encoded)
        resp = requests.get(
            url,
            timeout=cls.IMDB_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        results = []
        for item in data.get("d", [])[:max_results]:
            title = item.get("l")
            if not title:
                continue
            results.append(
                {
                    "imdb_id": item.get("id", ""),
                    "title": title,
                    "year": item.get("y"),
                    "type": item.get(
                        "q", ""
                    ),  # "TV series", "feature", "TV episode", etc.
                    "stars": item.get("s", ""),
                }
            )
        return results

    @classmethod
    def _imdb_search(cls, query: str, max_results: int = 3) -> List[dict]:
        """Search IMDB suggestion API with progressive fallback.

        Tries the full query first, then strips episode/season markers and
        trailing words until results are found or the query is exhausted.
        Returns a list of dicts with keys: imdb_id, title, year, type, stars.
        """
        try:
            results = cls._imdb_search_single(query, max_results)
            if results:
                return results

            # Strip common episode markers (S01E02, season 1, ep 3, etc.)
            stripped = re.sub(
                r"\b(s\d+e\d+|season\s*\d+|episode\s*\d+|ep\s*\d+)\b",
                "",
                query,
                flags=re.IGNORECASE,
            ).strip()
            if stripped and stripped != query:
                results = cls._imdb_search_single(stripped, max_results)
                if results:
                    return results

            # Try progressively shorter word prefixes (drop trailing words).
            words = query.strip().split()
            for length in range(len(words) - 1, 1, -1):
                sub = " ".join(words[:length])
                results = cls._imdb_search_single(sub, max_results)
                if results:
                    return results

            return []
        except Exception as e:
            logger.debug("IMDB search failed for %r: %s", query, e)
            return []

    @classmethod
    def _imdb_fetch_details(cls, imdb_id: str) -> dict:
        """Fetch synopsis/description from an IMDB title page via JSON-LD.

        Returns a dict with optional keys: description, genre, actors.
        Returns empty dict on failure.
        """
        if not imdb_id or not imdb_id.startswith("tt"):
            return {}
        url = f"https://www.imdb.com/title/{imdb_id}/"
        try:
            resp = requests.get(
                url,
                timeout=cls.IMDB_TIMEOUT + 3,
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            if resp.status_code != 200:
                return {}
            # Extract JSON-LD block from <script type="application/ld+json">
            match = re.search(
                r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
                resp.text,
                re.DOTALL,
            )
            if not match:
                return {}
            ld_data = json.loads(match.group(1))
            details = {}
            if ld_data.get("description"):
                details["description"] = ld_data["description"]
            genre = ld_data.get("genre")
            if genre:
                details["genre"] = genre if isinstance(genre, list) else [genre]
            actors = ld_data.get("actor", [])
            if actors and isinstance(actors, list):
                details["actors"] = [
                    a.get("name", "") for a in actors[:6] if a.get("name")
                ]
            return details
        except Exception as e:
            logger.debug("IMDB detail fetch failed for %s: %s", imdb_id, e)
            return {}

    @classmethod
    def _imdb_enrich_results(
        cls, results: List[dict], max_enrich: int = 1
    ) -> List[dict]:
        """Enrich the top N IMDB results with synopsis via _imdb_fetch_details."""
        for r in results[:max_enrich]:
            imdb_id = r.get("imdb_id", "")
            if imdb_id:
                details = cls._imdb_fetch_details(imdb_id)
                if details.get("description"):
                    r["description"] = details["description"]
                if details.get("genre"):
                    r["genre"] = details["genre"]
                if details.get("actors"):
                    r["stars"] = ", ".join(details["actors"])
        return results

    @classmethod
    def _format_imdb_results(cls, results: List[dict]) -> str:
        """Format IMDB results into a short text block for LLM context."""
        if not results:
            return ""
        lines = []
        for r in results:
            year_str = f" ({r['year']})" if r.get("year") else ""
            type_str = f" [{r['type']}]" if r.get("type") else ""
            stars_str = f" — {r['stars']}" if r.get("stars") else ""
            genre_str = ""
            if r.get("genre"):
                genre_str = (
                    f" [{', '.join(r['genre'])}]"
                    if isinstance(r["genre"], list)
                    else f" [{r['genre']}]"
                )
            desc_str = ""
            if r.get("description"):
                desc_str = f"\n  Synopsis: {r['description']}"
            lines.append(
                f"- {r['title']}{year_str}{type_str}{genre_str}{stars_str}{desc_str}"
            )
        return "\n".join(lines)

    @classmethod
    def is_in_setup_mode(cls, campaign) -> bool:
        """Check if a campaign is still in interactive setup."""
        state = cls.get_campaign_state(campaign)
        return bool(state.get("setup_phase"))

    @classmethod
    async def start_campaign_setup(
        cls, campaign, raw_name: str, attachment_summary: str = None
    ) -> str:
        """Step 1: IMDB lookup + LLM classify, stores result, returns message."""
        gpt = GPT()

        # Search IMDB first to give the LLM concrete data.
        imdb_results = cls._imdb_search(raw_name)
        imdb_text = cls._format_imdb_results(imdb_results)
        _zork_log(
            f"SETUP CLASSIFY campaign={campaign.id}",
            f"raw_name={raw_name!r}\nIMDB results:\n{imdb_text or '(none)'}"
            f"\nattachment_summary={'yes (' + str(len(attachment_summary)) + ' chars)' if attachment_summary else 'no'}",
        )

        imdb_context = ""
        if imdb_text:
            imdb_context = (
                f"\nIMDB search results for '{raw_name}':\n{imdb_text}\n"
                "Use these results to help identify the work.\n"
            )

        attachment_context = ""
        if attachment_summary:
            attachment_context = (
                f"\nThe user also uploaded source material. Summary of uploaded text:\n"
                f"{attachment_summary}\n"
                "Use this to identify the work.\n"
            )

        classify_system = (
            "You classify whether text references a known published work "
            "(movie, book, TV show, video game, etc).\n"
            "Return ONLY valid JSON with these keys:\n"
            '- "is_known_work": boolean\n'
            '- "work_type": string (e.g. "film", "novel", "tv_series", "tv_episode", "video_game", "other") or null\n'
            '- "work_description": string (1-2 sentence description of the work) or null\n'
            '- "suggested_title": string (the canonical full title if known, else the raw name)\n'
            "No markdown, no code fences."
        )
        classify_user = (
            f"The user wants to play a campaign called: '{raw_name}'.\n"
            f"{imdb_context}"
            f"{attachment_context}"
            "Is this a known published work? Provide the canonical title and description."
        )
        try:
            response = await gpt.turbo_completion(
                classify_system, classify_user, temperature=0.3, max_tokens=300
            )
            response = cls._clean_response(response or "{}")
            json_text = cls._extract_json(response)
            result = cls._parse_json_lenient(json_text) if json_text else {}
        except Exception as e:
            logger.warning(f"Campaign classify failed: {e}")
            result = {}

        is_known = bool(result.get("is_known_work", False))
        work_type = result.get("work_type")
        work_desc = result.get("work_description") or ""
        suggested = result.get("suggested_title") or raw_name

        # If LLM missed it but IMDB found results, promote the top hit.
        if not is_known and imdb_results:
            top = imdb_results[0]
            # Enrich with synopsis for a better description
            cls._imdb_enrich_results([top], max_enrich=1)
            is_known = True
            suggested = top["title"]
            year_str = f" ({top['year']})" if top.get("year") else ""
            work_type = (top.get("type") or "").lower().replace(" ", "_") or "other"
            work_desc = top.get("description") or ""
            if not work_desc:
                stars = top.get("stars", "")
                work_desc = f"{top['title']}{year_str}"
                if stars:
                    work_desc += f" starring {stars}"
            _zork_log(
                "SETUP CLASSIFY IMDB OVERRIDE",
                f"LLM missed, using IMDB top hit: {suggested}",
            )

        setup_data = {
            "raw_name": suggested if is_known else raw_name,
            "is_known_work": is_known,
            "work_type": work_type,
            "work_description": work_desc,
            "imdb_results": imdb_results or [],
        }
        if attachment_summary:
            setup_data["attachment_summary"] = attachment_summary

        state = cls.get_campaign_state(campaign)
        state["setup_phase"] = "classify_confirm"
        state["setup_data"] = setup_data
        campaign.state_json = cls._dump_json(state)
        campaign.updated = db.func.now()
        db.session.commit()

        if is_known:
            msg = (
                f"I recognize **{suggested}** as a known {work_type or 'work'}.\n"
                f"_{work_desc}_\n\n"
                f"Is this correct? Reply **yes** to confirm, or tell me what it actually is."
            )
        else:
            msg = (
                f"I don't recognize **{raw_name}** as a known published work. "
                f"I'll treat it as an original setting.\n\n"
                f"Is this correct? Reply **yes** to confirm, or tell me what it actually is "
                f"(e.g. 'it's a movie called ...')."
            )
        return msg

    @classmethod
    async def handle_setup_message(
        cls, ctx, content: str, campaign, command_prefix: str = "!"
    ) -> str:
        """Router: dispatch to the correct phase handler."""
        app = AppConfig.get_flask()
        state = cls.get_campaign_state(campaign)
        phase = state.get("setup_phase")
        setup_data = state.get("setup_data") or {}

        if phase == "classify_confirm":
            return await cls._setup_handle_classify_confirm(
                ctx, content, campaign, state, setup_data
            )
        elif phase == "storyline_pick":
            return await cls._setup_handle_storyline_pick(
                ctx, content, campaign, state, setup_data
            )
        elif phase == "novel_questions":
            return await cls._setup_handle_novel_questions(
                ctx, content, campaign, state, setup_data
            )
        elif phase == "finalize":
            return await cls._setup_finalize(campaign, state, setup_data, user_id=ctx.author.id)
        else:
            # Unknown phase — clear setup and let normal play proceed.
            state.pop("setup_phase", None)
            state.pop("setup_data", None)
            campaign.state_json = cls._dump_json(state)
            campaign.updated = db.func.now()
            db.session.commit()
            return "Setup cleared. You can now play normally."

    @classmethod
    async def _setup_handle_classify_confirm(
        cls, ctx, content, campaign, state, setup_data
    ) -> str:
        """Parse confirmation, then generate storyline variants."""
        answer = content.strip().lower()

        if answer in ("yes", "y", "correct", "yep", "yeah"):
            # Confirmed — filter IMDB results to just the best match
            confirmed_name = setup_data.get("raw_name", "").lower()
            old_results = setup_data.get("imdb_results", [])
            if old_results and confirmed_name:
                # Keep only the result whose title best matches the confirmed name
                best = None
                for r in old_results:
                    if (
                        r.get("title", "").lower() in confirmed_name
                        or confirmed_name in r.get("title", "").lower()
                    ):
                        best = r
                        break
                setup_data["imdb_results"] = [best] if best else [old_results[0]]
            # Enrich with synopsis
            if setup_data.get("imdb_results"):
                cls._imdb_enrich_results(setup_data["imdb_results"], max_enrich=1)
        elif answer in ("no", "n", "nope"):
            # User says it's NOT a known work — flip to novel
            setup_data["is_known_work"] = False
            setup_data["work_type"] = None
            setup_data["work_description"] = ""
            setup_data["imdb_results"] = []
        else:
            # User is providing a correction — IMDB search + re-classify
            imdb_results = cls._imdb_search(content)
            imdb_text = cls._format_imdb_results(imdb_results)
            _zork_log(
                f"SETUP RE-CLASSIFY campaign={campaign.id}",
                f"user_input={content!r}\nIMDB results:\n{imdb_text or '(none)'}",
            )

            imdb_context = ""
            if imdb_text:
                imdb_context = (
                    f"\nIMDB search results for '{content}':\n{imdb_text}\n"
                    "Use these results to help identify the work.\n"
                )

            gpt = GPT()
            re_classify_system = (
                "You classify whether text references a known published work "
                "(movie, book, TV show, video game, etc).\n"
                "Return ONLY valid JSON with keys: is_known_work (bool), "
                "work_type (string or null), work_description (string or null), "
                "suggested_title (string — the canonical full title).\n"
                "No markdown, no code fences."
            )
            re_classify_user = (
                f"The user clarified their campaign: '{content}'\n"
                f"Original input was: '{setup_data.get('raw_name', '')}'\n"
                f"{imdb_context}"
                "Is this a known published work? Provide the canonical title and a description."
            )
            try:
                response = await gpt.turbo_completion(
                    re_classify_system,
                    re_classify_user,
                    temperature=0.3,
                    max_tokens=300,
                )
                response = cls._clean_response(response or "{}")
                json_text = cls._extract_json(response)
                result = cls._parse_json_lenient(json_text) if json_text else {}
            except Exception:
                result = {}
            setup_data["is_known_work"] = bool(result.get("is_known_work", False))
            setup_data["work_type"] = result.get("work_type")
            setup_data["work_description"] = result.get("work_description") or ""
            suggested = result.get("suggested_title") or content.strip()
            setup_data["raw_name"] = suggested

            # If LLM still missed it but IMDB found results, promote top hit.
            if not setup_data["is_known_work"] and imdb_results:
                top = imdb_results[0]
                setup_data["is_known_work"] = True
                setup_data["raw_name"] = top["title"]
                year_str = f" ({top['year']})" if top.get("year") else ""
                setup_data["work_type"] = (top.get("type") or "").lower().replace(
                    " ", "_"
                ) or "other"
                # Use enriched description if available (enrichment happens below)
                setup_data["work_description"] = f"{top['title']}{year_str}"

            # Filter to confirmed match only
            confirmed_name = setup_data.get("raw_name", "").lower()
            if imdb_results and confirmed_name:
                best = None
                for r in imdb_results:
                    if (
                        r.get("title", "").lower() in confirmed_name
                        or confirmed_name in r.get("title", "").lower()
                    ):
                        best = r
                        break
                setup_data["imdb_results"] = [best] if best else [imdb_results[0]]
            else:
                setup_data["imdb_results"] = imdb_results or []
            # Enrich with synopsis
            if setup_data.get("imdb_results"):
                cls._imdb_enrich_results(setup_data["imdb_results"], max_enrich=1)
                # Update work_description with enriched synopsis if available
                top_enriched = setup_data["imdb_results"][0]
                if top_enriched.get("description") and not setup_data.get(
                    "work_description"
                ):
                    setup_data["work_description"] = top_enriched["description"]

        # After confirmation (all paths), update work_description from enriched IMDB if still shallow
        if setup_data.get("imdb_results") and setup_data.get("is_known_work"):
            top = setup_data["imdb_results"][0]
            if top.get("description") and len(
                setup_data.get("work_description", "")
            ) < len(top["description"]):
                setup_data["work_description"] = top["description"]
            _zork_log(
                f"SETUP POST-CONFIRM IMDB campaign={campaign.id}",
                f"filtered_results={len(setup_data['imdb_results'])} "
                f"top={setup_data['imdb_results'][0].get('title', '?')!r} "
                f"has_synopsis={bool(setup_data['imdb_results'][0].get('description'))}",
            )

        # Check for .txt attachment
        att_text = await cls._extract_attachment_text(ctx)
        if isinstance(att_text, str) and att_text.startswith("ERROR:"):
            await ctx.channel.send(att_text.replace("ERROR:", "", 1))
        elif att_text:
            summary = await cls._summarise_long_text(att_text, ctx)
            if summary:
                setup_data["attachment_summary"] = summary

        # Generate storyline variants
        variants_msg = await cls._setup_generate_storyline_variants(
            campaign, setup_data
        )
        state["setup_phase"] = "storyline_pick"
        state["setup_data"] = setup_data
        campaign.state_json = cls._dump_json(state)
        campaign.updated = db.func.now()
        db.session.commit()
        return variants_msg

    @classmethod
    async def _setup_generate_storyline_variants(
        cls, campaign, setup_data, user_guidance: str = None
    ) -> str:
        """LLM generates 2-3 storyline variants, returns formatted message."""
        gpt = GPT()
        is_known = setup_data.get("is_known_work", False)
        raw_name = setup_data.get("raw_name", "unknown")
        work_desc = setup_data.get("work_description", "")
        work_type = setup_data.get("work_type", "work")

        system_prompt = (
            "You are a creative game designer who builds interactive text-adventure campaigns.\n"
            "All characters in the game are adults (18+), regardless of source material ages.\n"
            "Return ONLY valid JSON with a single key 'variants' containing an array of 2-3 objects.\n"
            "Each object must have:\n"
            '- "id": string (e.g. "variant-1")\n'
            '- "title": string (short catchy title)\n'
            '- "summary": string (2-3 sentences describing the storyline)\n'
            '- "main_character": string (protagonist name and brief role)\n'
            '- "essential_npcs": array of strings (3-5 key NPC names)\n'
            '- "chapter_outline": array of objects with "title" and "summary" (3-5 chapters)\n'
            "No markdown, no code fences, no explanation. ONLY the JSON object."
        )

        # Include IMDB data if available for richer context.
        imdb_results = setup_data.get("imdb_results", [])
        imdb_context = ""
        if imdb_results:
            imdb_text = cls._format_imdb_results(imdb_results)
            imdb_context = f"\nIMDB reference data:\n{imdb_text}\n"

        attachment_context = ""
        attachment_summary = setup_data.get("attachment_summary")
        if attachment_summary:
            attachment_context = (
                f"\nDetailed source material summary:\n{attachment_summary}\n"
                "Use this summary to create accurate, faithful storyline variants.\n"
            )

        guidance_context = ""
        if user_guidance:
            guidance_context = (
                f"\nThe user gave this direction for the variants:\n"
                f"{user_guidance}\n"
                "Follow these instructions closely when designing the variants.\n"
            )

        if is_known:
            user_prompt = (
                f"Generate 2-3 storyline variants for an interactive text-adventure campaign "
                f"based on the {work_type or 'work'}: '{raw_name}'.\n"
                f"Description: {work_desc}\n"
                f"{imdb_context}"
                f"{attachment_context}"
                f"{guidance_context}\n"
                f"Use the ACTUAL characters, locations, and plot points from '{raw_name}'. "
                f"Variant ideas: faithful retelling from a character's perspective, "
                f"alternate timeline, prequel/sequel, or a 'what-if' divergence.\n"
                f"Each variant must reference real characters and events from the source material."
            )
        else:
            user_prompt = (
                f"Generate 2-3 storyline variants for an original text-adventure campaign "
                f"called '{raw_name}'.\n"
                f"{attachment_context}"
                f"{guidance_context}"
                f"Each variant should have a different tone, central conflict, or protagonist archetype. "
                f"Be creative and specific with character names and chapter titles."
            )

        _zork_log(
            f"SETUP VARIANT GENERATION campaign={campaign.id}",
            f"is_known={is_known} raw_name={raw_name!r} work_desc={work_desc!r}\n"
            f"--- SYSTEM ---\n{system_prompt}\n--- USER ---\n{user_prompt}",
        )
        result = {}
        for attempt in range(2):
            try:
                cur_prompt = user_prompt
                if attempt == 1:
                    # Retry with simplified prompt on empty first response.
                    cur_prompt = (
                        f"Generate 2-3 adventure storyline variants for an adult text-adventure "
                        f"game inspired by '{raw_name}'. All characters are adults. "
                        f"Focus on the setting, survival themes, and exploration.\n"
                        f"{imdb_context}"
                    )
                    _zork_log(f"SETUP VARIANT RETRY campaign={campaign.id}", cur_prompt)
                response = await gpt.turbo_completion(
                    system_prompt, cur_prompt, temperature=0.8, max_tokens=3000
                )
                _zork_log("SETUP VARIANT RAW RESPONSE", response or "(empty)")
                response = cls._clean_response(response or "{}")
                json_text = cls._extract_json(response)
                result = cls._parse_json_lenient(json_text) if json_text else {}
                if isinstance(result.get("variants"), list) and result["variants"]:
                    break
            except Exception as e:
                logger.warning(
                    f"Storyline variant generation failed (attempt {attempt}): {e}"
                )
                _zork_log("SETUP VARIANT GENERATION FAILED", str(e))
                result = {}

        variants = result.get("variants", [])
        if not isinstance(variants, list) or not variants:
            _zork_log(
                "SETUP VARIANT FALLBACK",
                f"result keys={list(result.keys()) if isinstance(result, dict) else 'not-dict'}",
            )
            # Build a richer fallback from IMDB data when available.
            top_imdb = imdb_results[0] if imdb_results else {}
            cast = top_imdb.get("cast", [])
            main_char = cast[0] if cast else "The Protagonist"
            npcs = cast[1:5] if len(cast) > 1 else []
            synopsis = top_imdb.get("synopsis") or work_desc or ""
            variants = [
                {
                    "id": "variant-1",
                    "title": f"{raw_name}: Faithful Retelling",
                    "summary": synopsis[:300]
                    if synopsis
                    else f"An interactive adventure set in the world of {raw_name}.",
                    "main_character": main_char,
                    "essential_npcs": npcs,
                    "chapter_outline": [
                        {
                            "title": "Chapter 1: The Beginning",
                            "summary": "The adventure begins.",
                        },
                        {
                            "title": "Chapter 2: The Challenge",
                            "summary": "Obstacles arise.",
                        },
                        {
                            "title": "Chapter 3: The Resolution",
                            "summary": "The story concludes.",
                        },
                    ],
                }
            ]

        setup_data["storyline_variants"] = variants

        # Format for Discord
        lines = ["**Choose a storyline variant:**\n"]
        for i, v in enumerate(variants, 1):
            lines.append(f"**{i}. {v.get('title', 'Untitled')}**")
            lines.append(f"_{v.get('summary', '')}_")
            lines.append(f"Main character: {v.get('main_character', 'TBD')}")
            npcs = v.get("essential_npcs", [])
            if npcs:
                lines.append(f"Key NPCs: {', '.join(npcs)}")
            chapters = v.get("chapter_outline", [])
            if chapters:
                ch_titles = [ch.get("title", "?") for ch in chapters]
                lines.append(f"Chapters: {' → '.join(ch_titles)}")
            lines.append("")

        lines.append(
            f"Reply with **1**, **2**, or **3** to pick your storyline, "
            f"or **retry: <guidance>** to regenerate (e.g. `retry: make it darker`)."
        )
        return "\n".join(lines)

    @classmethod
    async def _setup_handle_storyline_pick(
        cls, ctx, content, campaign, state, setup_data
    ) -> str:
        """Parse user's choice. Known work → finalize. Novel → novel_questions.
        Supports ``retry: <guidance>`` to regenerate variants."""
        choice = content.strip()
        variants = setup_data.get("storyline_variants", [])

        # Handle retry with guidance
        if choice.lower().startswith("retry"):
            guidance = choice.split(":", 1)[1].strip() if ":" in choice else ""
            variants_msg = await cls._setup_generate_storyline_variants(
                campaign, setup_data, user_guidance=guidance or None
            )
            state["setup_data"] = setup_data
            campaign.state_json = cls._dump_json(state)
            campaign.updated = db.func.now()
            db.session.commit()
            return variants_msg

        try:
            idx = int(choice) - 1
        except (ValueError, TypeError):
            return (
                f"Please reply with a number (1-{len(variants)}), "
                f"or **retry: <guidance>** to regenerate."
            )

        if idx < 0 or idx >= len(variants):
            return f"Please reply with a number between 1 and {len(variants)}."

        chosen = variants[idx]
        setup_data["chosen_variant_id"] = chosen.get("id", f"variant-{idx + 1}")

        is_known = setup_data.get("is_known_work", False)
        if is_known:
            # Known works go straight to finalize with on_rails=true default
            state["setup_phase"] = "finalize"
            state["setup_data"] = setup_data
            campaign.state_json = cls._dump_json(state)
            campaign.updated = db.func.now()
            db.session.commit()
            return await cls._setup_finalize(campaign, state, setup_data, user_id=ctx.author.id)
        else:
            # Novel stories get extra questions
            state["setup_phase"] = "novel_questions"
            state["setup_data"] = setup_data
            campaign.state_json = cls._dump_json(state)
            campaign.updated = db.func.now()
            db.session.commit()
            return (
                "A few more questions for your original campaign:\n\n"
                "1. **On-rails mode?** Should the story strictly follow the chapter outline, "
                "or allow freeform exploration? (reply **on-rails** or **freeform**)\n"
            )

    @classmethod
    async def _setup_handle_novel_questions(
        cls, ctx, content, campaign, state, setup_data
    ) -> str:
        """Parse preferences, then finalize."""
        answer = content.strip().lower()
        prefs = setup_data.get("novel_preferences", {})

        if answer in ("on-rails", "onrails", "on rails", "rails", "strict"):
            prefs["on_rails"] = True
        else:
            prefs["on_rails"] = False

        setup_data["novel_preferences"] = prefs
        state["setup_phase"] = "finalize"
        state["setup_data"] = setup_data
        campaign.state_json = cls._dump_json(state)
        campaign.updated = db.func.now()
        db.session.commit()
        return await cls._setup_finalize(campaign, state, setup_data, user_id=ctx.author.id)

    @classmethod
    async def _setup_finalize(cls, campaign, state, setup_data, user_id: int = None) -> str:
        """LLM generates the full world. Populates characters, outline, summary, etc."""
        gpt = GPT()
        variants = setup_data.get("storyline_variants", [])
        chosen_id = setup_data.get("chosen_variant_id", "variant-1")
        chosen = None
        for v in variants:
            if v.get("id") == chosen_id:
                chosen = v
                break
        if chosen is None and variants:
            chosen = variants[0]
        if chosen is None:
            chosen = {
                "title": "Adventure",
                "summary": "",
                "main_character": "The Protagonist",
                "essential_npcs": [],
                "chapter_outline": [],
            }

        is_known = setup_data.get("is_known_work", False)
        raw_name = setup_data.get("raw_name", "unknown")
        novel_prefs = setup_data.get("novel_preferences", {})

        # Determine on_rails
        if is_known:
            on_rails = True
        else:
            on_rails = bool(novel_prefs.get("on_rails", False))

        finalize_system = (
            "You are a world-builder for interactive text-adventure campaigns.\n"
            "All characters in the game are adults (18+), regardless of source material ages.\n"
            "Return ONLY valid JSON with these keys:\n"
            '- "characters": object keyed by slug-id (lowercase-hyphenated). Each character has: '
            "name, personality, background, appearance, location, current_status, allegiance, relationship.\n"
            '- "story_outline": object with "chapters" array. Each chapter has: slug, title, summary, '
            "scenes (array of: slug, title, summary, setting, key_characters).\n"
            '- "summary": string (2-4 sentence world summary)\n'
            '- "start_room": object with room_title, room_summary, room_description, exits, location\n'
            '- "landmarks": array of strings (key locations)\n'
            '- "setting": string (one-line setting description)\n'
            '- "tone": string (tone/mood)\n'
            '- "default_persona": string (1-2 sentence protagonist persona, max 140 chars)\n'
            '- "opening_narration": string (vivid second-person opening, 200-400 chars)\n'
            "No markdown, no code fences, no explanation. ONLY the JSON object."
        )
        # Include IMDB data for richer world-building.
        imdb_results = setup_data.get("imdb_results", [])
        imdb_context = ""
        if imdb_results:
            imdb_text = cls._format_imdb_results(imdb_results)
            imdb_context = f"\nIMDB reference data:\n{imdb_text}\n"

        attachment_context = ""
        attachment_summary = setup_data.get("attachment_summary")
        if attachment_summary:
            attachment_context = (
                f"\nDetailed source material:\n{attachment_summary}\n"
                "Use this to create an accurate world with faithful characters, locations, and plot.\n"
            )

        finalize_user = (
            f"Build the complete world for: '{raw_name}'\n"
            f"Known work: {is_known}\n"
            f"Description: {setup_data.get('work_description', '')}\n"
            f"{imdb_context}"
            f"{attachment_context}"
            f"Chosen storyline:\n{json.dumps(chosen, indent=2)}\n\n"
            f"Include the main character '{chosen.get('main_character', '')}' and all essential NPCs "
            f"({', '.join(chosen.get('essential_npcs', []))}).\n"
            f"Expand the chapter outline into full chapters with 2-4 scenes each. "
            f"Use real names, locations, and plot points from the source material if this is a known work."
        )
        _zork_log(
            f"SETUP FINALIZE campaign={campaign.id}",
            f"--- SYSTEM ---\n{finalize_system}\n--- USER ---\n{finalize_user}",
        )
        world = {}
        for attempt in range(2):
            try:
                cur_user = finalize_user
                if attempt == 1:
                    cur_user = (
                        f"Build the complete world for an adult text-adventure game "
                        f"inspired by '{raw_name}'. All characters are adults.\n"
                        f"Focus on the setting, atmosphere, and adventure.\n"
                        f"{imdb_context}"
                        f"Chosen storyline:\n{json.dumps(chosen, indent=2)}\n\n"
                        f"Include all essential NPCs and expand chapters into scenes."
                    )
                    _zork_log(f"SETUP FINALIZE RETRY campaign={campaign.id}", cur_user)
                response = await gpt.turbo_completion(
                    finalize_system, cur_user, temperature=0.7, max_tokens=4000
                )
                _zork_log("SETUP FINALIZE RAW RESPONSE", response or "(empty)")
                response = cls._clean_response(response or "{}")
                json_text = cls._extract_json(response)
                world = cls._parse_json_lenient(json_text) if json_text else {}
                if world and world.get("characters") or world.get("start_room"):
                    break
            except Exception as e:
                logger.warning(f"Campaign finalize failed (attempt {attempt}): {e}")
                _zork_log("SETUP FINALIZE FAILED", str(e))
                world = {}

        # Populate characters_json
        characters = world.get("characters", {})
        if isinstance(characters, dict) and characters:
            campaign.characters_json = cls._dump_json(characters)

        # Populate campaign state
        story_outline = world.get("story_outline", {})
        start_room = world.get("start_room", {})
        landmarks = world.get("landmarks", [])
        setting = world.get("setting", "")
        tone = world.get("tone", "")
        default_persona = world.get("default_persona", "")
        summary = world.get("summary", "")
        opening = world.get("opening_narration", "")

        if summary:
            campaign.summary = summary

        # Build final state — remove setup keys
        state.pop("setup_phase", None)
        state.pop("setup_data", None)

        if isinstance(story_outline, dict):
            state["story_outline"] = story_outline
            state["current_chapter"] = 0
            state["current_scene"] = 0
        if isinstance(start_room, dict):
            state["start_room"] = start_room
        if isinstance(landmarks, list):
            state["landmarks"] = landmarks
        if setting:
            state["setting"] = setting
        if tone:
            state["tone"] = tone
        if default_persona:
            state["default_persona"] = default_persona
        state["on_rails"] = on_rails

        campaign.state_json = cls._dump_json(state)
        campaign.updated = db.func.now()

        # Set opening narration
        if opening:
            # Build narration with room details
            room_title = start_room.get("room_title", "")
            narration = f"{room_title}\n{opening}" if room_title else opening
            exits = start_room.get("exits")
            if exits and isinstance(exits, list):
                exit_labels = []
                for e in exits:
                    if isinstance(e, dict):
                        exit_labels.append(
                            e.get("direction") or e.get("name") or str(e)
                        )
                    else:
                        exit_labels.append(str(e))
                narration += f"\nExits: {', '.join(exit_labels)}"
            campaign.last_narration = narration
        db.session.commit()

        # Auto-set the setup creator's character name and start room.
        if user_id is not None:
            player = cls.get_or_create_player(campaign.id, user_id, campaign=campaign)
            player_state = cls.get_player_state(player)
            main_char = chosen.get("main_character", "")
            if main_char and not player_state.get("character_name"):
                player_state["character_name"] = main_char
            if default_persona and not player_state.get("persona"):
                player_state["persona"] = cls._trim_text(default_persona, cls.MAX_PERSONA_PROMPT_CHARS)
            if isinstance(start_room, dict):
                for key in ("room_title", "room_summary", "room_description", "exits", "location"):
                    val = start_room.get(key)
                    if val is not None:
                        player_state[key] = val
            player.state_json = cls._dump_json(player_state)
            player.updated = db.func.now()
            db.session.commit()

        rails_label = "**On-Rails**" if on_rails else "**Freeform**"
        char_count = len(characters) if isinstance(characters, dict) else 0
        chapter_count = (
            len(story_outline.get("chapters", []))
            if isinstance(story_outline, dict)
            else 0
        )

        result_msg = (
            f"Campaign **{raw_name}** is ready! ({rails_label} mode)\n"
            f"Characters: {char_count} | Chapters: {chapter_count} | "
            f"Landmarks: {len(landmarks) if isinstance(landmarks, list) else 0}\n\n"
        )
        if opening:
            room_title = start_room.get("room_title", "")
            result_msg += f"**{room_title}**\n{opening}" if room_title else opening
            exits = start_room.get("exits")
            if exits and isinstance(exits, list):
                exit_labels = []
                for e in exits:
                    if isinstance(e, dict):
                        exit_labels.append(
                            e.get("direction") or e.get("name") or str(e)
                        )
                    else:
                        exit_labels.append(str(e))
                result_msg += f"\nExits: {', '.join(exit_labels)}"

        _zork_log(
            f"CAMPAIGN SETUP FINALIZED campaign={campaign.id}",
            f"characters={char_count} chapters={chapter_count} on_rails={on_rails}",
        )
        return result_msg

    # ── Attachment helpers ─────────────────────────────────────────────

    @classmethod
    async def _extract_attachment_text(cls, message) -> Optional[str]:
        """Return raw text from first .txt attachment, error string, or None."""
        attachments = getattr(message, "attachments", None)
        if not attachments:
            return None
        txt_att = None
        for att in attachments:
            if att.filename and att.filename.lower().endswith(".txt"):
                txt_att = att
                break
        if txt_att is None:
            return None
        if txt_att.size and txt_att.size > cls.ATTACHMENT_MAX_BYTES:
            size_kb = txt_att.size // 1024
            limit_kb = cls.ATTACHMENT_MAX_BYTES // 1024
            return f"ERROR:File too large ({size_kb}KB, limit {limit_kb}KB)"
        try:
            raw = await txt_att.read()
        except Exception as e:
            logger.warning(f"Attachment read failed: {e}")
            return None
        if not raw:
            return None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        text = text.strip()
        return text if text else None

    ATTACHMENT_MAX_CHUNKS = 8  # dynamic chunk sizing target

    @classmethod
    async def _summarise_long_text(cls, text: str, ctx_message, channel=None) -> str:
        """Chunk, summarise in parallel, condense to budget. Returns summary.
        *channel* overrides ctx_message.channel for progress messages.
        All sizing uses the GLM tokenizer for accurate token counts."""
        gpt = GPT()
        # Budget = whatever the context window can fit alongside the prompt
        budget_tokens = (
            cls.ATTACHMENT_MODEL_CTX_TOKENS
            - cls.ATTACHMENT_PROMPT_OVERHEAD_TOKENS
            - cls.ATTACHMENT_RESPONSE_RESERVE_TOKENS
        )
        min_chunk_tokens = cls.ATTACHMENT_CHUNK_TOKENS
        max_parallel = cls.ATTACHMENT_MAX_PARALLEL
        guard = cls.ATTACHMENT_GUARD_TOKEN
        progress_channel = channel or ctx_message.channel

        # Step 1 — token-aware dynamic chunking
        total_tokens = glm_token_count(text)
        # Target chunk size: at least min_chunk_tokens, but never more than MAX_CHUNKS
        target_chunk_tokens = max(min_chunk_tokens, total_tokens // cls.ATTACHMENT_MAX_CHUNKS)
        # Convert token target to a char estimate for paragraph-boundary splitting
        # Use measured ratio from this text for accuracy
        chars_per_tok = len(text) / max(total_tokens, 1)
        chunk_char_target = int(target_chunk_tokens * chars_per_tok)

        paragraphs = text.split("\n\n")
        chunks: List[str] = []
        current_chunk: List[str] = []
        current_len = 0
        for para in paragraphs:
            para_len = len(para)
            if current_len + para_len + 2 > chunk_char_target and current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = [para]
                current_len = para_len
            else:
                current_chunk.append(para)
                current_len += para_len + 2
        if current_chunk:
            chunks.append("\n\n".join(current_chunk))

        if not chunks:
            return ""

        # If single chunk within budget, return as-is
        if len(chunks) == 1 and glm_token_count(chunks[0]) <= budget_tokens:
            return chunks[0]

        total = len(chunks)
        _zork_log(
            "ATTACHMENT SUMMARISE",
            f"text_len={len(text)} total_tokens={total_tokens} "
            f"chunk_char_target={chunk_char_target} total_chunks={total}",
        )
        status_msg = await progress_channel.send(
            f"Summarising uploaded file... [0/{total}]"
        )

        # Step 2 — parallel summarise
        # Scale max_tokens with chunk size so larger chunks get more summary room
        summary_max_tokens = min(1500, max(800, target_chunk_tokens // 4))
        summarise_system = (
            "Summarise the following text passage for a text-adventure campaign. "
            "Preserve all character names, plot points, locations, and key events. "
            f"Be detailed but concise. End with the exact line: {guard}"
        )

        async def _summarise_chunk(chunk_text: str) -> str:
            try:
                result = await gpt.turbo_completion(
                    summarise_system, chunk_text,
                    max_tokens=summary_max_tokens, temperature=0.3,
                )
                result = (result or "").strip()
                if guard not in result:
                    logger.warning("Guard token missing, retrying chunk")
                    result = await gpt.turbo_completion(
                        summarise_system, chunk_text,
                        max_tokens=summary_max_tokens, temperature=0.3,
                    )
                    result = (result or "").strip()
                    if guard not in result:
                        logger.warning("Guard token still missing, accepting as-is")
                return result.replace(guard, "").strip()
            except Exception as e:
                logger.warning(f"Chunk summarisation failed: {e}")
                return ""

        summaries: List[str] = []
        processed = 0
        for batch_start in range(0, total, max_parallel):
            batch = chunks[batch_start : batch_start + max_parallel]
            tasks = [_summarise_chunk(c) for c in batch]
            results = await asyncio.gather(*tasks)
            summaries.extend(results)
            processed += len(batch)
            try:
                await status_msg.edit(
                    content=f"Summarising uploaded file... [{processed}/{total}]"
                )
            except Exception:
                pass

        # Filter empty summaries
        summaries = [s for s in summaries if s]
        if not summaries:
            logger.error("All chunk summaries failed")
            try:
                await status_msg.edit(content="Summary failed — continuing without attachment.")
                await asyncio.sleep(5)
                await status_msg.delete()
            except Exception:
                pass
            return ""

        # Step 3 — check total token length
        joined = "\n\n".join(summaries)
        joined_tokens = glm_token_count(joined)
        if joined_tokens <= budget_tokens:
            _zork_log(
                "ATTACHMENT SUMMARY DONE",
                f"tokens={joined_tokens} chars={len(joined)} (within budget)",
            )
            try:
                file_kb = len(text) // 1024
                await status_msg.edit(
                    content=f"Summary complete. ({joined_tokens} tokens from {file_kb}KB file)"
                )
                await asyncio.sleep(5)
                await status_msg.delete()
            except Exception:
                pass
            return joined

        # Step 4 — condensation pass (token-aware)
        num_summaries = len(summaries)
        target_tokens_per = budget_tokens // num_summaries
        # Convert per-summary token target to rough char target for the prompt
        target_chars_per = int(target_tokens_per * chars_per_tok)

        # Sort indices by token count descending (longest first)
        summary_tok_counts = [glm_token_count(s) for s in summaries]
        indexed = sorted(
            enumerate(summaries),
            key=lambda x: summary_tok_counts[x[0]],
            reverse=True,
        )
        to_condense = [
            (i, s) for i, s in indexed if summary_tok_counts[i] > target_tokens_per
        ]

        if to_condense:
            condense_total = len(to_condense)
            condense_done = 0
            try:
                await status_msg.edit(
                    content=f"Condensing summaries... [0/{condense_total}]"
                )
            except Exception:
                pass

            async def _condense(idx: int, summary_text: str) -> Tuple[int, str]:
                condense_system = (
                    f"Condense this summary to roughly {target_tokens_per} tokens "
                    f"(~{target_chars_per} characters) "
                    "while preserving all character names, plot points, and locations. "
                    f"End with: {guard}"
                )
                try:
                    result = await gpt.turbo_completion(
                        condense_system, summary_text,
                        max_tokens=target_tokens_per + 50, temperature=0.2,
                    )
                    result = (result or "").strip()
                    if guard not in result:
                        logger.warning("Guard token missing in condensation, accepting as-is")
                    return idx, result.replace(guard, "").strip()
                except Exception as e:
                    logger.warning(f"Condensation failed: {e}")
                    return idx, summary_text

            for batch_start in range(0, len(to_condense), max_parallel):
                batch = to_condense[batch_start : batch_start + max_parallel]
                tasks = [_condense(i, s) for i, s in batch]
                results = await asyncio.gather(*tasks)
                for idx, condensed in results:
                    if condensed:
                        summaries[idx] = condensed
                condense_done += len(batch)
                try:
                    await status_msg.edit(
                        content=f"Condensing summaries... [{condense_done}/{condense_total}]"
                    )
                except Exception:
                    pass

        joined = "\n\n".join(summaries)
        joined_tokens = glm_token_count(joined)
        # Hard-truncate if still over budget (rare after condensation)
        if joined_tokens > budget_tokens:
            # Trim by chars using the ratio — slightly conservative
            max_chars = int(budget_tokens * chars_per_tok * 0.9)
            if len(joined) > max_chars:
                joined = joined[: max_chars - len("... [truncated]")] + "... [truncated]"
                joined_tokens = glm_token_count(joined)

        # Step 5 — final edit + cleanup
        _zork_log(
            "ATTACHMENT SUMMARY DONE",
            f"tokens={joined_tokens} chars={len(joined)} chunks={total} "
            f"condensed={len(to_condense) if to_condense else 0}",
        )
        try:
            file_kb = len(text) // 1024
            await status_msg.edit(
                content=f"Summary complete. ({joined_tokens} tokens from {file_kb}KB file)"
            )
            await asyncio.sleep(5)
            await status_msg.delete()
        except Exception:
            pass

        return joined

    # ── End Campaign Setup ─────────────────────────────────────────────

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
        "complete",
        "completed",
        "done",
        "resolved",
        "finished",
        "concluded",
        "vacated",
        "dispersed",
        "avoided",
        "departed",
    }

    # Value patterns (strings) that indicate a past/resolved state.
    _STALE_VALUE_PATTERNS = _COMPLETED_VALUES | {
        "secured",
        "confirmed",
        "received",
        "granted",
        "initiated",
        "accepted",
        "placed",
        "offered",
    }

    @classmethod
    def _prune_stale_state(cls, state: Dict[str, object]) -> Dict[str, object]:
        """Remove keys from *state* that look like stale ephemeral tracking entries."""
        pruned = {}
        for key, value in state.items():
            # Drop string values that signal completion/past events.
            if (
                isinstance(value, str)
                and value.strip().lower() in cls._STALE_VALUE_PATTERNS
            ):
                continue
            # Drop boolean True flags whose key name indicates a past one-shot event.
            if value is True and any(
                key.endswith(s)
                for s in (
                    "_complete",
                    "_arrived",
                    "_announced",
                    "_revealed",
                    "_concluded",
                    "_departed",
                    "_dispatched",
                    "_offered",
                    "_introduced",
                    "_unlocked",
                )
            ):
                continue
            # Drop stale ETA/countdown/elapsed keys with numeric values.
            if isinstance(value, (int, float)) and any(
                key.endswith(s)
                for s in (
                    "_eta_minutes",
                    "_eta",
                    "_countdown_minutes",
                    "_countdown_hours",
                    "_countdown",
                    "_deadline_seconds",
                    "_time_elapsed",
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
    def _build_story_context(cls, campaign_state: Dict[str, object]) -> Optional[str]:
        """Build STORY_CONTEXT section for the prompt from story_outline.

        Returns None if no story_outline exists (freeform campaigns unaffected).
        """
        outline = campaign_state.get("story_outline")
        if not isinstance(outline, dict):
            return None
        chapters = outline.get("chapters")
        if not isinstance(chapters, list) or not chapters:
            return None

        current_ch = campaign_state.get("current_chapter", 0)
        current_sc = campaign_state.get("current_scene", 0)
        if not isinstance(current_ch, int):
            current_ch = 0
        if not isinstance(current_sc, int):
            current_sc = 0

        lines: List[str] = []

        # Previous chapter (condensed)
        if current_ch > 0 and current_ch - 1 < len(chapters):
            prev = chapters[current_ch - 1]
            lines.append(f"PREVIOUS CHAPTER: {prev.get('title', 'Untitled')}")
            lines.append(f"  Summary: {prev.get('summary', '')}")
            lines.append("")

        # Current chapter (expanded)
        if current_ch < len(chapters):
            cur = chapters[current_ch]
            lines.append(f"CURRENT CHAPTER: {cur.get('title', 'Untitled')}")
            lines.append(f"  Summary: {cur.get('summary', '')}")
            scenes = cur.get("scenes")
            if isinstance(scenes, list):
                for i, scene in enumerate(scenes):
                    marker = " >>> CURRENT SCENE <<<" if i == current_sc else ""
                    lines.append(
                        f"  Scene {i + 1}: {scene.get('title', 'Untitled')}{marker}"
                    )
                    lines.append(f"    Summary: {scene.get('summary', '')}")
                    setting = scene.get("setting")
                    if setting:
                        lines.append(f"    Setting: {setting}")
                    key_chars = scene.get("key_characters")
                    if key_chars:
                        lines.append(f"    Key characters: {', '.join(key_chars)}")
            lines.append("")

        # Next chapter (preview)
        if current_ch + 1 < len(chapters):
            nxt = chapters[current_ch + 1]
            lines.append(f"NEXT CHAPTER: {nxt.get('title', 'Untitled')}")
            summary = nxt.get("summary", "")
            # One-line preview
            if summary:
                lines.append(f"  Preview: {summary[:200]}")

        return "\n".join(lines) if lines else None

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
                    "discord_mention": f"<@{entry.user_id}>",
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
    def _strip_narration_footer(cls, text: str) -> str:
        """Remove trailing '---' section from narration if it contains XP info.

        Models sometimes echo the debug footer (XP Awarded, State Update, etc.)
        into the narration field.  Storing it causes the model to repeat it on
        every subsequent turn because the footer leaks into RECENT_TURNS context.
        """
        if not text:
            return text
        idx = text.rfind("---")
        if idx == -1:
            return text
        tail = text[idx:]
        if "xp" in tail.lower():
            return text[:idx].rstrip()
        return text

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
    def _get_inventory_rich(
        cls, player_state: Dict[str, object]
    ) -> List[Dict[str, str]]:
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
                name = str(
                    item.get("name") or item.get("item") or item.get("title") or ""
                ).strip()
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
        first_sentence = re.split(r"(?<=[.!?])\s", source, maxsplit=1)[0]
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
            w
            for w in re.findall(r"[a-z0-9]+", item_l)
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
                previous_inventory_rich,
                inventory_add,
                inventory_remove,
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
            if (
                str(new_location).strip().lower()
                != str(old_location or "").strip().lower()
            ):
                if "room_description" not in cleaned:
                    cleaned["room_description"] = None
                if "room_title" not in cleaned:
                    cleaned["room_title"] = None

        return cleaned

    _INVENTORY_LINE_PREFIXES = (
        "inventory:",
        "inventory -",
        "items:",
        "items carried:",
        "you are carrying:",
        "you carry:",
        "your inventory:",
        "current inventory:",
    )

    @classmethod
    def _strip_inventory_from_narration(cls, narration: str) -> str:
        if not narration:
            return ""
        # Drop any model-authored inventory line(s); we append canonical inventory later.
        kept_lines = []
        for line in narration.splitlines():
            stripped = line.strip().lower()
            if any(stripped.startswith(p) for p in cls._INVENTORY_LINE_PREFIXES):
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
        cls,
        existing: Dict[str, dict],
        updates: Dict[str, dict],
        on_rails: bool = False,
    ) -> Dict[str, dict]:
        """Merge character updates into existing characters dict.

        New slugs get all fields stored.  Existing slugs only get mutable
        fields updated — immutable fields are silently dropped.
        When *on_rails* is True, new slugs are rejected entirely.
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
                if on_rails:
                    logger.info("On-rails: rejected new character slug %r", slug)
                    continue
                # New character — store everything.
                existing[slug] = dict(fields)
        return existing

    @classmethod
    def _apply_calendar_update(
        cls,
        campaign_state: Dict[str, object],
        calendar_update: dict,
    ) -> Dict[str, object]:
        """Process calendar add/remove ops, enforce max 10 events, prune expired."""
        if not isinstance(calendar_update, dict):
            return campaign_state
        calendar = list(campaign_state.get("calendar") or [])
        game_time = campaign_state.get("game_time") or {}
        current_day = game_time.get("day", 1)
        current_hour = game_time.get("hour", 8)

        # Remove named events.
        to_remove = calendar_update.get("remove")
        if isinstance(to_remove, list):
            remove_set = {str(n).strip().lower() for n in to_remove if n}
            calendar = [
                e for e in calendar
                if str(e.get("name", "")).strip().lower() not in remove_set
            ]

        # Add new events.
        to_add = calendar_update.get("add")
        if isinstance(to_add, list):
            for entry in to_add:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or "").strip()
                if not name:
                    continue
                event = {
                    "name": name,
                    "time_remaining": entry.get("time_remaining", 1),
                    "time_unit": entry.get("time_unit", "days"),
                    "created_day": current_day,
                    "created_hour": current_hour,
                    "description": str(entry.get("description") or "")[:200],
                }
                calendar.append(event)

        # Auto-prune expired: events with time_remaining <= 0.
        calendar = [
            e for e in calendar
            if not (
                isinstance(e.get("time_remaining"), (int, float))
                and e["time_remaining"] <= 0
            )
        ]

        # Enforce max 10 events (keep the newest).
        if len(calendar) > 10:
            calendar = calendar[-10:]

        campaign_state["calendar"] = calendar
        return campaign_state

    @classmethod
    def _compose_character_portrait_prompt(cls, name: str, appearance: str) -> str:
        """Build an image-generation prompt from character name + appearance."""
        prompt_parts = [
            f"Character portrait of {name}.",
            appearance.strip() if appearance else "",
            "single character",
            "centered composition",
            "detailed fantasy illustration",
        ]
        composed = " ".join([p for p in prompt_parts if p])
        composed = re.sub(r"\s+", " ", composed).strip()
        return cls._trim_text(composed, 900)

    @classmethod
    async def _enqueue_character_portrait(
        cls,
        ctx,
        campaign: ZorkCampaign,
        character_slug: str,
        name: str,
        appearance: str,
    ) -> bool:
        """Queue portrait generation for an NPC character."""
        if not appearance or not appearance.strip():
            return False
        if not cls._gpu_worker_available():
            return False
        discord_wrapper = DiscordBot.get_instance()
        if discord_wrapper is None or discord_wrapper.bot is None:
            return False
        generator = discord_wrapper.bot.get_cog("Generate")
        if generator is None:
            return False

        composed_prompt = cls._compose_character_portrait_prompt(name, appearance)
        campaign_state = cls.get_campaign_state(campaign)
        selected_model = campaign_state.get("avatar_image_model")
        if not isinstance(selected_model, str) or not selected_model.strip():
            selected_model = cls.DEFAULT_AVATAR_IMAGE_MODEL

        cfg = AppConfig()
        user_id = getattr(getattr(ctx, "author", None), "id", 0)
        user_config = cfg.get_user_config(user_id=user_id)
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
                user_id=user_id,
                prompt=composed_prompt,
                job_metadata={
                    "zork_scene": True,
                    "suppress_image_reactions": True,
                    "suppress_image_details": True,
                    "zork_store_character_portrait": True,
                    "zork_campaign_id": campaign.id,
                    "zork_character_slug": character_slug,
                },
            )
        except Exception as e:
            logger.warning(f"Failed to enqueue character portrait for {character_slug}: {e}")
            return False
        return True

    @classmethod
    def record_character_portrait_url(
        cls, campaign_id: int, character_slug: str, image_url: str
    ) -> bool:
        """Store a portrait URL on a character in characters_json."""
        campaign = ZorkCampaign.query.get(campaign_id)
        if campaign is None:
            return False
        characters = cls.get_campaign_characters(campaign)
        if character_slug not in characters:
            return False
        characters[character_slug]["image_url"] = image_url
        campaign.characters_json = cls._dump_json(characters)
        campaign.updated = db.func.now()
        db.session.commit()
        return True

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
                # Full record for nearby characters (strip image_url — harness-managed).
                entry = {k: v for k, v in char.items() if k != "image_url"}
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
    def is_on_rails(cls, campaign: Optional[ZorkCampaign]) -> bool:
        if campaign is None:
            return False
        campaign_state = cls.get_campaign_state(campaign)
        return bool(campaign_state.get("on_rails", False))

    @classmethod
    def set_on_rails(cls, campaign: Optional[ZorkCampaign], enabled: bool) -> bool:
        if campaign is None:
            return False
        campaign_state = cls.get_campaign_state(campaign)
        campaign_state["on_rails"] = bool(enabled)
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
    def get_speed_multiplier(cls, campaign: Optional[ZorkCampaign]) -> float:
        if campaign is None:
            return 1.0
        campaign_state = cls.get_campaign_state(campaign)
        raw = campaign_state.get("speed_multiplier", 1.0)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 1.0

    @classmethod
    def set_speed_multiplier(
        cls, campaign: Optional[ZorkCampaign], multiplier: float
    ) -> bool:
        if campaign is None:
            return False
        multiplier = max(0.1, min(10.0, float(multiplier)))
        campaign_state = cls.get_campaign_state(campaign)
        campaign_state["speed_multiplier"] = multiplier
        campaign.state_json = cls._dump_json(campaign_state)
        campaign.updated = db.func.now()
        db.session.commit()
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
                cls._edit_timer_line(
                    channel_id,
                    message_id,
                    f"\u2705 *Timer cancelled — you acted in time. (Averted: {event})*",
                )
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
        # Seed game_time if missing.
        if "game_time" not in state:
            state["game_time"] = {
                "day": 1, "hour": 8, "minute": 0,
                "period": "morning", "date_label": "Day 1, Morning",
            }
            campaign.state_json = cls._dump_json(state)
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
        _ERROR_PHRASES = (
            "a hollow silence answers",
            "the world shifts, but nothing clear emerges",
        )
        # Build user_id → character_name map for turn labels.
        _player_names: Dict[int, str] = {}
        _turn_user_ids = {t.user_id for t in turns if t.user_id is not None}
        if _turn_user_ids:
            _all_players = ZorkPlayer.query.filter(
                ZorkPlayer.campaign_id == campaign.id,
                ZorkPlayer.user_id.in_(_turn_user_ids),
            ).all()
            for p in _all_players:
                ps = cls.get_player_state(p)
                name = ps.get("character_name") or ""
                if name:
                    _player_names[p.user_id] = name

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
                name = _player_names.get(turn.user_id)
                mention = f"<@{turn.user_id}>" if turn.user_id else ""
                if name and mention:
                    label = f"PLAYER {mention} ({name.upper()})"
                elif name:
                    label = f"PLAYER ({name.upper()})"
                elif mention:
                    label = f"PLAYER {mention}"
                else:
                    label = "PLAYER"
                recent_lines.append(f"{label}: {clipped}")
            elif turn.kind == "narrator":
                # Skip error/fallback narrations.
                if content.lower() in _ERROR_PHRASES:
                    continue
                # Strip timer countdown and inventory lines from context.
                clipped_lines = []
                for line in content.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("\u23f0"):
                        continue
                    if stripped.lower().startswith("inventory:"):
                        continue
                    clipped_lines.append(line)
                clipped = "\n".join(clipped_lines).strip()
                clipped = cls._strip_narration_footer(clipped)
                if not clipped:
                    continue
                clipped = cls._trim_text(clipped, cls.MAX_TURN_CHARS)
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

        story_context = cls._build_story_context(state)
        on_rails = bool(state.get("on_rails", False))

        _game_time = state.get("game_time", {})
        _speed_mult = state.get("speed_multiplier", 1.0)
        _calendar = state.get("calendar", [])
        user_prompt = (
            f"CAMPAIGN: {campaign.name}\n"
            f"PLAYER_ID: {player.user_id}\n"
            f"IS_NEW_PLAYER: {str(is_new_player).lower()}\n"
            f"GUARDRAILS_ENABLED: {str(guardrails_enabled).lower()}\n"
            f"RAILS_CONTEXT: {cls._dump_json(rails_context)}\n"
            f"WORLD_SUMMARY: {summary}\n"
            f"WORLD_STATE: {cls._dump_json(model_state)}\n"
            f"CURRENT_GAME_TIME: {cls._dump_json(_game_time)}\n"
            f"SPEED_MULTIPLIER: {_speed_mult}\n"
            f"CALENDAR: {cls._dump_json(_calendar)}\n"
        )
        if story_context:
            user_prompt += f"STORY_CONTEXT:\n{story_context}\n"
        _active_name = (player_state.get("character_name") or "").strip()
        _active_mention = f"<@{player.user_id}>" if player.user_id else ""
        if _active_name and _active_mention:
            _action_label = f"PLAYER_ACTION {_active_mention} ({_active_name.upper()})"
        elif _active_name:
            _action_label = f"PLAYER_ACTION ({_active_name.upper()})"
        else:
            _action_label = "PLAYER_ACTION"
        user_prompt += (
            f"WORLD_CHARACTERS: {cls._dump_json(characters_for_prompt)}\n"
            f"PLAYER_CARD: {cls._dump_json(player_card)}\n"
            f"PARTY_SNAPSHOT: {cls._dump_json(party_snapshot)}\n"
            f"RECENT_TURNS:\n{recent_text}\n"
            f"{_action_label}: {action}\n"
        )
        system_prompt = cls.SYSTEM_PROMPT
        if guardrails_enabled:
            system_prompt = f"{system_prompt}{cls.GUARDRAILS_SYSTEM_PROMPT}"
        if on_rails:
            system_prompt = f"{system_prompt}{cls.ON_RAILS_SYSTEM_PROMPT}"
        system_prompt = f"{system_prompt}{cls.MEMORY_TOOL_PROMPT}"
        if state.get("timed_events_enabled", True):
            system_prompt = f"{system_prompt}{cls.TIMER_TOOL_PROMPT}"
        if story_context:
            system_prompt = f"{system_prompt}{cls.STORY_OUTLINE_TOOL_PROMPT}"
        system_prompt = f"{system_prompt}{cls.CALENDAR_TOOL_PROMPT}"
        system_prompt = f"{system_prompt}{cls.ROSTER_PROMPT}"
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
    def _coerce_python_dict(text: str) -> Optional[dict]:
        """Try to parse *text* as a Python dict literal (single-quoted keys/values).

        Handles JSON keywords (null/true/false) by replacing them with Python
        equivalents before calling ast.literal_eval.
        """
        try:
            # Replace JSON keywords with Python equivalents.
            # Only target standalone tokens — \b prevents matching inside words.
            fixed = re.sub(r"\bnull\b", "None", text)
            fixed = re.sub(r"\btrue\b", "True", fixed)
            fixed = re.sub(r"\bfalse\b", "False", fixed)
            result = ast.literal_eval(fixed)
            if isinstance(result, dict):
                return result
        except Exception:
            pass
        return None

    @classmethod
    def _parse_json_lenient(cls, text: str) -> dict:
        """Parse a JSON object from *text*, tolerating common LLM quirks.

        Tries in order:
        1. Standard json.loads
        2. Python dict literal (single quotes) via ast.literal_eval
        3. JSONL-style (multiple JSON objects) via raw_decode + merge
        """
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result
            return {}
        except json.JSONDecodeError as exc:
            # Try coercing single-quoted Python dict before anything else.
            coerced = cls._coerce_python_dict(text)
            if coerced is not None:
                return coerced

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
            elif (
                isinstance(value, str)
                and value.strip().lower() in cls._COMPLETED_VALUES
            ):
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

                    # Defensive guard: if still in setup mode, skip gameplay.
                    if cls.is_in_setup_mode(campaign):
                        return "Campaign setup is still in progress. Please complete setup first."

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
                                interrupt_action = cancelled_timer.get(
                                    "interrupt_action"
                                )
                                if interrupt_action:
                                    timer_interrupt_context = interrupt_action
                                # Persist the interruption as a turn so it appears in RECENT_TURNS.
                                event_desc = cancelled_timer.get(
                                    "event", "an impending event"
                                )
                                interrupt_note = (
                                    f"[TIMER INTERRUPTED] The player acted before the timed event fired. "
                                    f'Averted event: "{event_desc}"'
                                )
                                if interrupt_action:
                                    interrupt_note += (
                                        f' Interruption context: "{interrupt_action}"'
                                    )
                                db.session.add(
                                    ZorkTurn(
                                        campaign_id=campaign.id,
                                        user_id=ctx.author.id,
                                        kind="narrator",
                                        content=interrupt_note,
                                        channel_id=ctx.channel.id,
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
                        if exits and isinstance(exits, list):
                            _el = [
                                (e.get("direction") or e.get("name") or str(e))
                                if isinstance(e, dict) else str(e)
                                for e in exits
                            ]
                            exits_text = f"\nExits: {', '.join(_el)}"
                        else:
                            exits_text = ""
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
                                channel_id=ctx.channel.id,
                            )
                        )
                        db.session.add(
                            ZorkTurn(
                                campaign_id=campaign.id,
                                user_id=ctx.author.id,
                                kind="narrator",
                                content=narration,
                                channel_id=ctx.channel.id,
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
                                channel_id=ctx.channel.id,
                            )
                        )
                        db.session.add(
                            ZorkTurn(
                                campaign_id=campaign.id,
                                user_id=ctx.author.id,
                                kind="narrator",
                                content=narration,
                                channel_id=ctx.channel.id,
                            )
                        )
                        campaign.last_narration = narration
                        campaign.updated = db.func.now()
                        db.session.commit()
                        return narration

                    # --- Intercepted: calendar ---
                    if action_clean in ("calendar", "cal", "events"):
                        campaign_state = cls.get_campaign_state(campaign)
                        game_time = campaign_state.get("game_time", {})
                        calendar = campaign_state.get("calendar", [])
                        date_label = game_time.get(
                            "date_label",
                            f"Day {game_time.get('day', '?')}, {game_time.get('period', '?').title()}",
                        )
                        lines = [f"**Game Time:** {date_label}"]
                        if calendar:
                            lines.append("**Upcoming Events:**")
                            for ev in calendar:
                                remaining = ev.get("time_remaining", "?")
                                unit = ev.get("time_unit", "?")
                                desc = ev.get("description", "")
                                lines.append(
                                    f"- **{ev.get('name', 'Unknown')}** — {remaining} {unit} remaining"
                                    + (f" ({desc})" if desc else "")
                                )
                        else:
                            lines.append("No upcoming events.")
                        narration = "\n".join(lines)
                        return narration

                    # --- Intercepted: roster ---
                    if action_clean in ("roster", "characters", "npcs"):
                        characters = cls.get_campaign_characters(campaign)
                        if not characters:
                            return "No characters in the roster yet."
                        lines = ["**Character Roster:**"]
                        for slug, char in characters.items():
                            name = char.get("name", slug)
                            loc = char.get("location", "unknown")
                            status = char.get("current_status", "")
                            bg = char.get("background", "")
                            origin = bg.split(".")[0].strip() if bg else ""
                            portrait = char.get("image_url", "")
                            deceased = char.get("deceased_reason")
                            entry = f"- **{name}** ({slug})"
                            if deceased:
                                entry += f" [DECEASED: {deceased}]"
                            else:
                                entry += f" — {loc}"
                                if status:
                                    entry += f" | {status}"
                            if origin:
                                entry += f"\n  *{origin}.*"
                            if portrait:
                                entry += f"\n  Portrait: {portrait}"
                            lines.append(entry)
                        narration = "\n".join(lines)
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
                            f'The interrupted event was: "{timer_interrupt_context}"\n'
                            f'The player\'s action that interrupted it: "{action}"\n'
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
                            tool_name = str(
                                first_payload.get("tool_call") or ""
                            ).strip()

                            if tool_name == "memory_search":
                                # Support both "queries": [...] and legacy "query": "..."
                                raw_queries = first_payload.get("queries") or []
                                if not raw_queries:
                                    legacy = str(
                                        first_payload.get("query") or ""
                                    ).strip()
                                    if legacy:
                                        raw_queries = [legacy]
                                queries = [
                                    str(q).strip()
                                    for q in raw_queries
                                    if str(q).strip()
                                ]
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
                                        results = ZorkMemory.search(
                                            query, campaign.id, top_k=5
                                        )
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
                                            if score >= 0.35
                                            and turn_id not in seen_turn_ids
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
                                                f"Results for '{query}':\n"
                                                + "\n".join(recall_lines)
                                            )
                                    if recall_sections:
                                        recall_block = (
                                            "MEMORY_RECALL (results from memory_search):\n"
                                            + "\n".join(recall_sections)
                                        )
                                    else:
                                        recall_block = (
                                            "MEMORY_RECALL: No relevant memories found."
                                        )
                                    _zork_log("MEMORY RECALL BLOCK", recall_block)
                                    augmented_prompt = (
                                        f"{user_prompt}\n{recall_block}\n"
                                    )
                                    response = await gpt.turbo_completion(
                                        system_prompt,
                                        augmented_prompt,
                                        temperature=0.8,
                                        max_tokens=2048,
                                    )
                                    if not response:
                                        response = (
                                            "A hollow silence answers. Try again."
                                        )
                                    else:
                                        response = cls._clean_response(response)
                                    _zork_log("AUGMENTED API RESPONSE", response)

                            elif (
                                tool_name == "set_timer"
                                and cls.is_timed_events_enabled(campaign)
                            ):
                                raw_delay = first_payload.get("delay_seconds", 60)
                                try:
                                    delay_seconds = int(raw_delay)
                                except (TypeError, ValueError):
                                    delay_seconds = 60
                                _speed = cls.get_speed_multiplier(campaign)
                                if _speed > 0:
                                    delay_seconds = int(delay_seconds / _speed)
                                delay_seconds = max(15, min(300, delay_seconds))
                                event_description = str(
                                    first_payload.get("event_description")
                                    or "Something happens."
                                ).strip()[:500]

                                cls.cancel_pending_timer(campaign.id)
                                channel_id = ctx.channel.id
                                cls._schedule_timer(
                                    campaign.id,
                                    channel_id,
                                    delay_seconds,
                                    event_description,
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
                                    f"Now narrate the current scene. Hint at urgency narratively but do NOT include "
                                    f"countdowns, timestamps, emoji clocks, or explicit seconds — the system adds its own countdown."
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

                            elif tool_name == "story_outline":
                                chapter_slug = str(
                                    first_payload.get("chapter") or ""
                                ).strip()
                                _zork_log(
                                    "STORY OUTLINE TOOL CALL",
                                    f"chapter={chapter_slug!r}",
                                )
                                outline_result = ""
                                campaign_state_so = cls.get_campaign_state(campaign)
                                so = campaign_state_so.get("story_outline")
                                if isinstance(so, dict):
                                    for ch in so.get("chapters", []):
                                        if ch.get("slug") == chapter_slug:
                                            outline_result = json.dumps(ch, indent=2)
                                            break
                                if not outline_result:
                                    outline_result = (
                                        f"No chapter found with slug '{chapter_slug}'."
                                    )
                                outline_block = f"STORY_OUTLINE_RESULT (chapter={chapter_slug}):\n{outline_result}\n"
                                augmented_prompt = f"{user_prompt}\n{outline_block}\n"
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
                                _zork_log("STORY OUTLINE AUGMENTED RESPONSE", response)

                        # Fallback: LLM returned set_timer alongside narration.
                        # _is_tool_call rejects that, but we still honour the timer.
                        elif (
                            first_payload
                            and isinstance(first_payload, dict)
                            and str(first_payload.get("tool_call") or "").strip()
                            == "set_timer"
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
                                first_payload.get("event_description")
                                or "Something happens."
                            ).strip()[:500]

                            cls.cancel_pending_timer(campaign.id)
                            channel_id = ctx.channel.id
                            cls._schedule_timer(
                                campaign.id,
                                channel_id,
                                delay_seconds,
                                event_description,
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
                    give_item = None
                    calendar_update = None
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
                            character_updates = (
                                payload.get("character_updates", {}) or {}
                            )
                            give_item = payload.get("give_item")
                            calendar_update = payload.get("calendar_update")

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
                                    _speed = cls.get_speed_multiplier(campaign)
                                    if _speed > 0:
                                        t_delay = int(t_delay / _speed)
                                    t_delay = max(15, min(300, t_delay))
                                    t_event = str(inline_timer_event).strip()[:500]
                                    t_interruptible = bool(
                                        payload.get("set_timer_interruptible", True)
                                    )
                                    t_interrupt_action = payload.get(
                                        "set_timer_interrupt_action"
                                    )
                                    if isinstance(t_interrupt_action, str):
                                        t_interrupt_action = (
                                            t_interrupt_action.strip()[:500] or None
                                        )
                                    else:
                                        t_interrupt_action = None
                                    cls._schedule_timer(
                                        campaign.id,
                                        ctx.channel.id,
                                        t_delay,
                                        t_event,
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
                        except json.JSONDecodeError as e:
                            logger.warning(
                                f"Failed to parse Zork JSON response: {e} — retrying"
                            )
                            _zork_log(
                                "JSON PARSE RETRY",
                                f"error={e}\nresponse={response[:500]}",
                            )
                            response = await gpt.turbo_completion(
                                system_prompt,
                                user_prompt,
                                temperature=0.7,
                                max_tokens=2048,
                            )
                            if response:
                                response = cls._clean_response(response)
                                json_text = cls._extract_json(response)
                                if json_text:
                                    try:
                                        payload = cls._parse_json_lenient(json_text)
                                        narration = payload.get(
                                            "narration", narration
                                        ).strip()
                                        state_update = (
                                            payload.get("state_update", {}) or {}
                                        )
                                        summary_update = payload.get("summary_update")
                                        xp_awarded = payload.get("xp_awarded", 0) or 0
                                        player_state_update = (
                                            payload.get("player_state_update", {}) or {}
                                        )
                                        scene_image_prompt = payload.get(
                                            "scene_image_prompt"
                                        )
                                        character_updates = (
                                            payload.get("character_updates", {}) or {}
                                        )
                                        calendar_update = payload.get("calendar_update")
                                    except Exception as e2:
                                        logger.warning(
                                            f"Retry also failed to parse JSON: {e2}"
                                        )
                        except Exception as e:
                            logger.warning(f"Failed to parse Zork JSON response: {e}")

                    # Safety: if narration still looks like raw JSON, something
                    # went wrong during parsing.  Try to salvage the narration
                    # key so raw JSON never leaks to Discord or stored turns.
                    if narration.lstrip().startswith("{"):
                        try:
                            salvage = json.loads(cls._extract_json(narration) or "{}")
                            if isinstance(salvage, dict) and salvage:
                                narration = (
                                    str(salvage.get("narration", "")).strip()
                                    or "The world shifts, but nothing clear emerges."
                                )
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

                    # Chapter/scene advancement from state_update
                    story_outline = campaign_state.get("story_outline")
                    if isinstance(story_outline, dict):
                        new_ch = state_update.get("current_chapter")
                        new_sc = state_update.get("current_scene")
                        old_ch = campaign_state.get("current_chapter", 0)
                        if isinstance(new_ch, int) and new_ch != old_ch:
                            chapters = story_outline.get("chapters", [])
                            if isinstance(chapters, list) and 0 <= old_ch < len(
                                chapters
                            ):
                                chapters[old_ch]["completed"] = True
                            campaign_state["current_chapter"] = new_ch
                        if isinstance(new_sc, int):
                            campaign_state["current_scene"] = new_sc
                        # Remove from state_update so they don't pollute model state
                        state_update.pop("current_chapter", None)
                        state_update.pop("current_scene", None)
                        campaign.state_json = cls._dump_json(campaign_state)

                    _on_rails = bool(campaign_state.get("on_rails", False))
                    if character_updates and isinstance(character_updates, dict):
                        existing_chars = cls.get_campaign_characters(campaign)
                        _pre_slugs = set(existing_chars.keys())
                        existing_chars = cls._apply_character_updates(
                            existing_chars,
                            character_updates,
                            on_rails=_on_rails,
                        )
                        campaign.characters_json = cls._dump_json(existing_chars)
                        _zork_log(
                            f"CHARACTER UPDATES campaign={campaign.id}",
                            json.dumps(character_updates, indent=2),
                        )
                        # Auto-generate portraits for new characters with appearance.
                        for _slug in character_updates:
                            if _slug not in _pre_slugs and _slug in existing_chars:
                                _char = existing_chars[_slug]
                                _appearance = str(_char.get("appearance") or "").strip()
                                if _appearance and not _char.get("image_url"):
                                    _char_name = _char.get("name", _slug)
                                    asyncio.ensure_future(
                                        cls._enqueue_character_portrait(
                                            ctx, campaign, _slug, _char_name, _appearance,
                                        )
                                    )

                    if calendar_update and isinstance(calendar_update, dict):
                        campaign_state = cls._apply_calendar_update(
                            campaign_state, calendar_update
                        )
                        campaign.state_json = cls._dump_json(campaign_state)

                    if summary_update:
                        summary_update = summary_update.strip()
                        summary_update = cls._strip_inventory_mentions(summary_update)
                        campaign.summary = cls._append_summary(
                            campaign.summary, summary_update
                        )

                    player_state = cls.get_player_state(player)
                    _pre_update_inv = {
                        e["name"].lower(): e["name"]
                        for e in cls._get_inventory_rich(player_state)
                    }
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

                    # --- give_item: cross-player item transfer ---
                    # Heuristic fallback: if model forgot give_item but removed
                    # items + narration mentions giving to another player, infer it.
                    if give_item is None:
                        _cur_inv_names = {
                            e["name"].lower()
                            for e in cls._get_inventory_rich(player_state)
                        }
                        _removed = [
                            _pre_update_inv[k]
                            for k in _pre_update_inv
                            if k not in _cur_inv_names
                        ]
                        if _removed:
                            _give_re = re.compile(
                                r"\b(?:give|hand|pass|toss|offer|slide)\b",
                                re.IGNORECASE,
                            )
                            _refuse_re = re.compile(
                                r"\b(?:doesn'?t take|does not take|refuse[sd]?|reject[sd]?|decline[sd]?"
                                r"|push(?:es|ed)? (?:it |the \w+ )?(?:back|away)"
                                r"|won'?t (?:take|accept)|shake[sd]? (?:his|her|their) head"
                                r"|hands? it back|gives? it back|returns? (?:it|the))\b",
                                re.IGNORECASE,
                            )
                            if (_give_re.search(action) or _give_re.search(raw_narration or "")) and not _refuse_re.search(raw_narration or ""):
                                # Find first other-player mention in narration
                                _mention_re = re.compile(r"<@!?(\d+)>")
                                for _m in _mention_re.finditer(raw_narration or ""):
                                    _target_uid = int(_m.group(1))
                                    if _target_uid != player.user_id:
                                        _inferred_item = _removed[0] if len(_removed) == 1 else None
                                        if _inferred_item is None:
                                            # Multiple items removed; try matching action text
                                            _action_lower = action.lower()
                                            for _ri in _removed:
                                                if _ri.lower() in _action_lower:
                                                    _inferred_item = _ri
                                                    break
                                        if _inferred_item:
                                            give_item = {
                                                "item": _inferred_item,
                                                "to_discord_mention": f"<@{_target_uid}>",
                                            }
                                            logger.info(
                                                "give_item inferred from action/narration: %s -> user %s",
                                                _inferred_item, _target_uid,
                                            )
                                            break

                    if isinstance(give_item, dict):
                        gi_item_name = str(give_item.get("item") or "").strip()
                        gi_mention = str(give_item.get("to_discord_mention") or "").strip()
                        # Parse user id from mention format <@123456>
                        gi_target_uid = None
                        if gi_mention.startswith("<@") and gi_mention.endswith(">"):
                            try:
                                gi_target_uid = int(gi_mention.strip("<@!>"))
                            except (ValueError, TypeError):
                                pass
                        if gi_item_name and gi_target_uid and gi_target_uid != player.user_id:
                            # Check if item is still in giver's inventory or was
                            # already removed by inventory_remove (pre-update had it).
                            giver_inv = cls._get_inventory_rich(player_state)
                            giver_has_now = any(
                                e["name"].lower() == gi_item_name.lower() for e in giver_inv
                            )
                            giver_had_before = gi_item_name.lower() in _pre_update_inv
                            if giver_has_now or giver_had_before:
                                target_player = ZorkPlayer.query.filter_by(
                                    campaign_id=campaign.id, user_id=gi_target_uid
                                ).first()
                                if target_player is not None:
                                    # Remove from giver if still present
                                    if giver_has_now:
                                        player_state["inventory"] = cls._apply_inventory_delta(
                                            giver_inv, [], [gi_item_name], origin_hint=""
                                        )
                                        player.state_json = cls._dump_json(player_state)
                                    # Add to receiver
                                    target_state = cls.get_player_state(target_player)
                                    target_inv = cls._get_inventory_rich(target_state)
                                    received_origin = f"Received from <@{player.user_id}>"
                                    target_state["inventory"] = cls._apply_inventory_delta(
                                        target_inv, [gi_item_name], [], origin_hint=received_origin
                                    )
                                    target_player.state_json = cls._dump_json(target_state)
                                    target_player.updated = db.func.now()
                                    logger.info(
                                        "give_item: '%s' transferred from user %s to user %s (campaign %s)",
                                        gi_item_name, player.user_id, gi_target_uid, campaign.id,
                                    )
                                else:
                                    logger.warning(
                                        "give_item: target user %s not found in campaign %s",
                                        gi_target_uid, campaign.id,
                                    )
                            else:
                                logger.warning(
                                    "give_item: item '%s' not in giver's inventory", gi_item_name
                                )

                    if isinstance(xp_awarded, int) and xp_awarded > 0:
                        player.xp += xp_awarded

                    narration = cls._strip_narration_footer(narration)
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
                                channel_id=ctx.channel.id,
                            )
                        )
                    narrator_turn = ZorkTurn(
                        campaign_id=campaign.id,
                        user_id=ctx.author.id,
                        kind="narrator",
                        content=narration,
                        channel_id=ctx.channel.id,
                    )
                    db.session.add(narrator_turn)
                    db.session.commit()

                    cls._create_snapshot(narrator_turn, campaign)

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
                    cls._edit_timer_line(
                        ch_id,
                        msg_id,
                        f"\u26a0\ufe0f *Timer expired — {event_description}*",
                    )
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
        timed_narrator_turn_id = None
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
                        age = (
                            datetime.datetime.utcnow() - latest_turn.created
                        ).total_seconds()
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
                calendar_update = None

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
                        character_updates = payload.get("character_updates", {}) or {}
                        calendar_update = payload.get("calendar_update")
                    except json.JSONDecodeError as e:
                        logger.warning(
                            f"Failed to parse timed event JSON: {e} — retrying"
                        )
                        response = await gpt.turbo_completion(
                            system_prompt, user_prompt, temperature=0.7, max_tokens=2048
                        )
                        if response:
                            response = cls._clean_response(response)
                            narration = response.strip()
                            json_text = cls._extract_json(response)
                            if json_text:
                                try:
                                    payload = cls._parse_json_lenient(json_text)
                                    narration = payload.get(
                                        "narration", narration
                                    ).strip()
                                    state_update = payload.get("state_update", {}) or {}
                                    summary_update = payload.get("summary_update")
                                    xp_awarded = payload.get("xp_awarded", 0) or 0
                                    player_state_update = (
                                        payload.get("player_state_update", {}) or {}
                                    )
                                    character_updates = (
                                        payload.get("character_updates", {}) or {}
                                    )
                                    calendar_update = payload.get("calendar_update")
                                except Exception as e2:
                                    logger.warning(
                                        f"Timed event retry also failed: {e2}"
                                    )
                    except Exception as e:
                        logger.warning(
                            f"Failed to parse timed event JSON response: {e}"
                        )

                if narration.lstrip().startswith("{"):
                    try:
                        salvage = json.loads(cls._extract_json(narration) or "{}")
                        if isinstance(salvage, dict) and salvage:
                            narration = (
                                str(salvage.get("narration", "")).strip()
                                or "The world shifts, but nothing clear emerges."
                            )
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

                _on_rails = bool(campaign_state.get("on_rails", False))
                if character_updates and isinstance(character_updates, dict):
                    existing_chars = cls.get_campaign_characters(campaign)
                    _pre_slugs = set(existing_chars.keys())
                    existing_chars = cls._apply_character_updates(
                        existing_chars,
                        character_updates,
                        on_rails=_on_rails,
                    )
                    campaign.characters_json = cls._dump_json(existing_chars)
                    _zork_log(
                        f"CHARACTER UPDATES (timed event) campaign={campaign.id}",
                        json.dumps(character_updates, indent=2),
                    )
                    # Auto-generate portraits for new characters with appearance.
                    _channel_obj = await DiscordBot.get_instance().find_channel(channel_id) if DiscordBot.get_instance() else None
                    if _channel_obj is not None:
                        _synth_ctx = cls._build_synthetic_generation_context(
                            _channel_obj, active_player.user_id
                        )
                        for _slug in character_updates:
                            if _slug not in _pre_slugs and _slug in existing_chars:
                                _char = existing_chars[_slug]
                                _appearance = str(_char.get("appearance") or "").strip()
                                if _appearance and not _char.get("image_url"):
                                    _char_name = _char.get("name", _slug)
                                    asyncio.ensure_future(
                                        cls._enqueue_character_portrait(
                                            _synth_ctx, campaign, _slug, _char_name, _appearance,
                                        )
                                    )

                if calendar_update and isinstance(calendar_update, dict):
                    campaign_state = cls._apply_calendar_update(
                        campaign_state, calendar_update
                    )
                    campaign.state_json = cls._dump_json(campaign_state)

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
                player_state = cls._apply_state_update(
                    player_state, player_state_update
                )
                active_player.state_json = cls._dump_json(player_state)

                if isinstance(xp_awarded, int) and xp_awarded > 0:
                    active_player.xp += xp_awarded

                narration = cls._strip_narration_footer(narration)
                campaign.last_narration = narration
                campaign.updated = db.func.now()
                active_player.updated = db.func.now()

                narrator_turn = ZorkTurn(
                    campaign_id=campaign.id,
                    user_id=None,
                    kind="narrator",
                    content=f"[TIMED EVENT] {narration}",
                    channel_id=channel_id,
                )
                db.session.add(narrator_turn)
                db.session.commit()

                cls._create_snapshot(narrator_turn, campaign)

                target_user_id = active_player.user_id
                timed_narrator_turn_id = narrator_turn.id

        # Post to Discord outside the lock / app context.
        bot_instance = DiscordBot.get_instance()
        if bot_instance is None:
            return
        channel = await bot_instance.find_channel(channel_id)
        if channel is None:
            return
        mention = f"<@{target_user_id}>" if target_user_id else ""
        output = f"**[Timed Event]** {mention}\n{narration}"
        msg = await DiscordBot.send_large_message(channel, output)
        if msg is not None and timed_narrator_turn_id is not None:
            app = AppConfig.get_flask()
            if app is not None:
                with app.app_context():
                    try:
                        turn = ZorkTurn.query.get(timed_narrator_turn_id)
                        if turn is not None:
                            turn.discord_message_id = msg.id
                            db.session.commit()
                    except Exception:
                        logger.debug(
                            "Zork: failed to record timed event message ID",
                            exc_info=True,
                        )

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

            # Enhanced map context
            campaign_state = cls.get_campaign_state(campaign)
            model_state = cls._build_model_state(campaign_state)
            model_state = cls._fit_state_to_budget(model_state, 800)

            landmarks = campaign_state.get("landmarks", [])
            landmarks_text = (
                ", ".join(landmarks)
                if isinstance(landmarks, list) and landmarks
                else "none"
            )

            # Condensed characters: name + location only, living, max 20
            characters = cls.get_campaign_characters(campaign)
            char_entries = []
            if isinstance(characters, dict):
                for slug, info in list(characters.items())[:20]:
                    if not isinstance(info, dict):
                        continue
                    if info.get("deceased_reason"):
                        continue
                    char_name = info.get("name", slug)
                    char_loc = info.get("location", "unknown")
                    char_entries.append(f"{char_name} ({char_loc})")
            chars_text = ", ".join(char_entries) if char_entries else "none"

            # Story progress
            story_progress = ""
            outline = campaign_state.get("story_outline")
            if isinstance(outline, dict):
                chapters = outline.get("chapters", [])
                try:
                    cur_ch = int(campaign_state.get("current_chapter", 0))
                except (ValueError, TypeError):
                    cur_ch = 0
                try:
                    cur_sc = int(campaign_state.get("current_scene", 0))
                except (ValueError, TypeError):
                    cur_sc = 0
                if isinstance(chapters, list) and 0 <= cur_ch < len(chapters):
                    ch = chapters[cur_ch]
                    ch_title = ch.get("title", "")
                    scenes = ch.get("scenes", [])
                    sc_title = ""
                    if isinstance(scenes, list) and 0 <= cur_sc < len(scenes):
                        sc_title = scenes[cur_sc].get("title", "")
                    story_progress = (
                        f"{ch_title} / {sc_title}" if sc_title else ch_title
                    )

            map_prompt = (
                f"CAMPAIGN: {campaign.name}\n"
                f"PLAYER_NAME: {player_name}\n"
                f"PLAYER_ROOM_TITLE: {room_title or 'Unknown'}\n"
                f"PLAYER_ROOM_SUMMARY: {room_summary or ''}\n"
                f"PLAYER_EXITS: {exits or []}\n"
                f"WORLD_SUMMARY: {cls._trim_text(campaign.summary or '', 1200)}\n"
                f"WORLD_STATE: {cls._dump_json(model_state)}\n"
                f"LANDMARKS: {landmarks_text}\n"
                f"WORLD_CHARACTERS: {chars_text}\n"
            )
            if story_progress:
                map_prompt += f"STORY_PROGRESS: {story_progress}\n"
            map_prompt += (
                f"OTHER_PLAYERS: {cls._dump_json(other_entries)}\n"
                "Draw a compact map with @ marking the player's location.\n"
            )
            gpt = GPT()
            response = await gpt.turbo_completion(
                cls.MAP_SYSTEM_PROMPT,
                map_prompt,
                temperature=0.2,
                max_tokens=600,
            )
            ascii_map = cls._extract_ascii_map(response)
            if not ascii_map:
                return "Map is foggy. Try again."
            return ascii_map
