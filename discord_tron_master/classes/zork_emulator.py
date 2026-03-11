import ast
import asyncio
import dataclasses
import datetime
import difflib
import fnmatch
import json
import logging
import os
import re
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import discord
import requests
from sqlalchemy.exc import IntegrityError
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

_ZORK_LOG_ROOT = os.path.join(os.getcwd(), "zork-logs")
_ZORK_LOG_STATE = threading.local()
_ZORK_LOG_RETENTION = 100


def _zork_log_component(value: object, default: str) -> str:
    text = str(value or "").strip()
    if not text:
        return default
    text = re.sub(r"[\\/]+", "-", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    return text or default


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


def _zork_log_numeric_entries(dir_path: str) -> list[tuple[int, str]]:
    entries: list[tuple[int, str]] = []
    try:
        names = os.listdir(dir_path)
    except OSError:
        return entries
    for name in names:
        match = re.fullmatch(r"(\d+)\.log", name)
        if not match:
            continue
        entries.append((int(match.group(1)), os.path.join(dir_path, name)))
    entries.sort(key=lambda item: item[0])
    return entries


def _zork_log_prune_dir(dir_path: str, keep: int = _ZORK_LOG_RETENTION) -> None:
    entries = _zork_log_numeric_entries(dir_path)
    while len(entries) > keep:
        _, path = entries.pop(0)
        try:
            os.remove(path)
        except OSError:
            break


def _zork_log_next_turn_path(dir_path: str) -> str:
    os.makedirs(dir_path, exist_ok=True)
    entries = _zork_log_numeric_entries(dir_path)
    next_idx = (entries[-1][0] if entries else 0) + 1
    _zork_log_prune_dir(dir_path, keep=max(0, _ZORK_LOG_RETENTION - 1))
    return os.path.join(dir_path, f"{next_idx}.log")


def _zork_log_push_path(path: str) -> Optional[str]:
    prev_path = getattr(_ZORK_LOG_STATE, "path", None)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _ZORK_LOG_STATE.path = path
    return prev_path


def _zork_log_pop_path(prev_path: Optional[str]) -> None:
    if prev_path:
        _ZORK_LOG_STATE.path = prev_path
    elif hasattr(_ZORK_LOG_STATE, "path"):
        delattr(_ZORK_LOG_STATE, "path")


def _zork_log_default_path(section: str) -> str:
    campaign_match = re.search(r"campaign=(\d+)", str(section or ""))
    if campaign_match:
        try:
            campaign_id = int(campaign_match.group(1))
            campaign = ZorkCampaign.query.get(campaign_id)
            if campaign is not None:
                channel_row = (
                    ZorkChannel.query.filter_by(active_campaign_id=campaign_id)
                    .order_by(ZorkChannel.updated.desc(), ZorkChannel.id.desc())
                    .first()
                )
                dir_path = _zork_log_context_dir(
                    guild_id=campaign.guild_id if channel_row is not None else None,
                    channel_id=channel_row.channel_id if channel_row is not None else None,
                    user_id=campaign.created_by if channel_row is None else None,
                )
                return os.path.join(dir_path, "event.log")
        except Exception:
            pass
    return os.path.join(_ZORK_LOG_ROOT, "global", "event.log")


def _zork_log(section: str, body: str = "") -> None:
    """Append a timestamped section to the active per-context Zork log file."""
    try:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_path = getattr(_ZORK_LOG_STATE, "path", None) or _zork_log_default_path(
            section
        )
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a") as fh:
            fh.write(f"\n{'='*72}\n[{ts}] {section}\n{'='*72}\n")
            if body:
                fh.write(body)
                if not body.endswith("\n"):
                    fh.write("\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Concurrency dataclasses for phase-split turn processing
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class CampaignConcurrencyState:
    """Per-campaign concurrency bookkeeping replacing the old asyncio.Lock."""

    commit_lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)
    inflight_count: int = 0
    max_inflight: int = 4  # cap concurrent Phase 2s per campaign


@dataclasses.dataclass
class PreflightContext:
    """Everything Phase 1 produces for Phase 2 to consume."""

    campaign_id: int = 0
    player_id: int = 0
    user_id: int = 0
    campaign_state: Dict[str, Any] = dataclasses.field(default_factory=dict)
    player_state: Dict[str, Any] = dataclasses.field(default_factory=dict)
    state_version: int = 0
    pre_turn_game_time: Dict[str, Any] = dataclasses.field(default_factory=dict)
    system_prompt: str = ""
    user_prompt: str = ""
    gpt: Any = None  # GPT instance
    action: str = ""
    turns: List[Any] = dataclasses.field(default_factory=list)
    party_snapshot: Dict[str, Any] = dataclasses.field(default_factory=dict)

    # Context/prompt-building state
    base_user_prompt: str = ""
    turn_prompt_tail: str = ""
    turn_tail_extra_lines: List[str] = dataclasses.field(default_factory=list)
    prompt_difficulty: str = "normal"
    current_prompt_stage: str = ""
    first_payload: Optional[Dict[str, Any]] = None
    memory_lookup_enabled: bool = False
    is_new_player: bool = False
    turn_attachment_context: str = ""
    viewer_slug: str = ""
    viewer_location_key: str = ""
    viewer_private_context_key: str = ""
    viewer_private_context: Optional[Dict[str, Any]] = None
    recent_private_contexts: List[Any] = dataclasses.field(default_factory=list)
    private_context_candidate: Optional[Dict[str, Any]] = None
    time_skip_request: Optional[Dict[str, Any]] = None
    timer_interrupt_context: Optional[str] = None

    # SMS
    sms_activity_detected: bool = False
    sms_inline_draft: Optional[Tuple[str, str]] = None

    # Channel / visibility flags
    is_thread_channel: bool = False
    is_dm: bool = False
    channel_id: int = 0
    command_prefix: str = "!"

    # Player snapshot fields (read-only during Phase 2)
    player_attributes_json: Optional[str] = None
    player_level: int = 1
    player_xp: int = 0

    # Campaign / player ORM references (read-only during Phase 2)
    campaign_name: str = ""
    campaign_summary: Optional[str] = None
    on_rails: bool = False
    is_timed_events_enabled: bool = False

    # Receiver hints for heuristic preload
    receiver_hints: Dict[str, Any] = dataclasses.field(default_factory=dict)

    # Forced memory search flag
    auto_forced_memory_search: bool = False


@dataclasses.dataclass
class TurnDelta:
    """Everything Phase 2 produces for Phase 3 to consume."""

    state_version_at_read: int = 0

    narration: str = ""
    raw_narration: str = ""
    scene_output: Optional[Any] = None
    scene_output_raw: Optional[Any] = None
    reasoning: Optional[str] = None

    state_update: Dict[str, Any] = dataclasses.field(default_factory=dict)
    player_state_update: Dict[str, Any] = dataclasses.field(default_factory=dict)
    co_located_player_slugs: List[str] = dataclasses.field(default_factory=list)
    character_updates: Dict[str, Any] = dataclasses.field(default_factory=dict)
    summary_update: Optional[str] = None
    calendar_update: Optional[Dict[str, Any]] = None
    give_item: Optional[Dict[str, Any]] = None
    xp_awarded: int = 0

    turn_visibility: Optional[Dict[str, Any]] = None
    story_progression: Optional[Any] = None
    scene_image_prompt: Optional[str] = None

    deferred_tool_writes: List[Dict[str, Any]] = dataclasses.field(default_factory=list)

    # Timer fields
    timer_scheduled_delay: Optional[int] = None
    timer_scheduled_event: Optional[str] = None
    timer_scheduled_interruptible: bool = True
    timer_scheduled_interrupt_scope: str = "global"
    timer_scheduled_interrupt_action: Optional[str] = None
    timer_scheduled_interrupt_user_id: Optional[int] = None

    # Post-processing metadata
    ephemeral_notices: List[str] = dataclasses.field(default_factory=list)
    used_tool_names: set = dataclasses.field(default_factory=set)
    forced_planning_payload: Optional[Dict[str, Any]] = None
    auto_forced_memory_search: bool = False
    empty_response_repair_count: int = 0
    anti_echo_retry_count: int = 0
    sms_activity_detected: bool = False
    private_phone_redacted: bool = False
    stored_player_action: str = ""
    suppress_recent_context: bool = False


class ZorkEmulator:
    KNOWN_JSON_STRING_FIELDS = {
        "reasoning",
        "narration",
        "summary_update",
        "scene_image_prompt",
        "type",
        "speaker",
        "text",
        "location_key",
        "context_key",
        "title",
        "summary",
        "main_character",
        "name",
        "description",
        "period",
        "date_label",
        "thread",
        "from",
        "to",
        "message",
        "event_description",
        "interrupt_action",
        "interrupt_scope",
        "current_scene",
        "current_status",
        "allegiance",
        "relationship",
        "deceased_reason",
        "resolution",
        "trigger",
        "consequence",
        "severity",
        "slug",
        "id",
        "label",
        "wildcard",
        "document_key",
        "target",
        "reason",
        "status",
        "room_title",
        "room_summary",
        "room_description",
        "location",
        "event_key",
        "notes",
        "details",
        "time",
        "literary_style",
    }
    DEFAULT_STYLE_DIRECTION = AppConfig.DEFAULT_ZORK_STYLE
    BASE_POINTS = 10
    POINTS_PER_LEVEL = 5
    MAX_ATTRIBUTE_VALUE = 20
    MAX_SUMMARY_CHARS = 10000
    MAX_STATE_CHARS = 10000
    MAX_RECENT_TURNS = 24
    MAX_TURN_CHARS = 1200
    MAX_NARRATION_CHARS = 23500
    MAX_PARTY_CONTEXT_PLAYERS = 6
    MAX_SCENE_PROMPT_CHARS = 900
    MAX_PERSONA_PROMPT_CHARS = 140
    MAX_SCENE_REFERENCE_IMAGES = 10
    XP_BASE = 100
    XP_PER_LEVEL = 50
    MAX_INVENTORY_CHANGES_PER_TURN = 10
    MAX_CHARACTERS_CHARS = 8000
    IMMUTABLE_CHARACTER_FIELDS: set = set()  # slug is the dict key, not a field
    MAX_CHARACTERS_IN_PROMPT = 20
    ATTENTION_WINDOW_SECONDS = 600
    MIN_TURN_ADVANCE_MINUTES = 1
    DEFAULT_TURN_ADVANCE_MINUTES = 5
    MAX_TURN_ADVANCE_MINUTES = 180
    ROOM_IMAGE_STATE_KEY = "room_scene_images"
    PLAYER_STATS_KEY = "zork_stats"
    PLAYER_STATS_MESSAGES_KEY = "messages_sent"
    PLAYER_STATS_TIMERS_AVERTED_KEY = "timers_averted"
    PLAYER_STATS_TIMERS_MISSED_KEY = "timers_missed"
    PLAYER_STATS_ATTENTION_SECONDS_KEY = "attention_seconds"
    PLAYER_STATS_LAST_MESSAGE_AT_KEY = "last_message_at"
    PLAYER_STATS_LAST_MESSAGE_CONTEXT_KEY = "last_message_context"
    PLAYER_STATS_LAST_MESSAGE_CHANNEL_ID_KEY = "last_message_channel_id"
    PRIVATE_DM_TIME_JUMP_NOTIFY_MINUTES = 30
    ATTACHMENT_MAX_BYTES = 500_000
    TURN_ATTACHMENT_INLINE_BYTES = 10_000
    ATTACHMENT_CHUNK_TOKENS = 50_000      # minimum tokens per chunk
    ATTACHMENT_MODEL_CTX_TOKENS = 200_000 # GLM-5 context window
    ATTACHMENT_PROMPT_OVERHEAD_TOKENS = 6_000  # reserve for system + user + IMDB + storyline JSON
    ATTACHMENT_RESPONSE_RESERVE_TOKENS = 90_000 # max_tokens used by finalize response
    ATTACHMENT_SUMMARY_MAX_TOKENS = 90_000
    ATTACHMENT_MAX_PARALLEL = 4
    ATTACHMENT_MIN_SETUP_CHUNKS = 1
    ATTACHMENT_GUARD_TOKEN = "--COMPLETED SUMMARY--"
    TURN_ATTACHMENT_SUMMARY_INSTRUCTIONS = (
        "Summarise this uploaded text for a single game turn. Preserve names, quoted phrases, "
        "lyrics, instructions, factual details, and any wording the GM may need to reference. "
        "Treat it as temporary context for one reply, not permanent canon."
    )
    SETUP_GENRE_TEMPLATES = {
        "upbeat": "Warm and optimistic — good things happen to people who try.",
        "rom-com": "Romantic comedy — charm, miscommunication, and a satisfying payoff.",
        "horror": "Dread, tension, and things that should not be.",
        "noir": "Cynical narration, moral grey areas, rain-slicked streets.",
        "thriller": "High stakes, ticking clocks, and dangerous people.",
        "spaghetti-western": "Dusty standoffs, laconic antiheroes, Morricone energy.",
        "psychedelic": "Reality is negotiable. Expect the unexpected.",
        "buddy-comedy": "Two clashing personalities, one shared problem.",
        "absurd": "Logic is optional. Commit to the bit.",
        "detective-novel": "Clues, red herrings, and a mystery that rewards attention.",
        "epic-fantasy": "Grand quests, ancient powers, and a world worth saving.",
        "sci-fi": "Technology, exploration, and questions about what it means to be human.",
        "dreamlike-fantasy": "Surreal, poetic, and just slightly impossible.",
    } 
    # Behind the Name usage codes for name_generate tool.
    # Keys are human-friendly labels the LLM can use; values are URL param fragments.
    NAME_ORIGIN_CODES = {
        "african": "afr", "albanian": "alb", "arabic": "ara", "armenian": "arm",
        "azerbaijani": "aze", "basque": "bas", "bengali": "ben", "bosnian": "bos",
        "breton": "bre", "bulgarian": "bul", "catalan": "cat", "chinese": "chi",
        "croatian": "cro", "czech": "cze", "danish": "dan", "dutch": "dut",
        "english": "eng", "estonian": "est", "filipino": "fil", "finnish": "fin",
        "french": "fre", "galician": "gal", "georgian": "geo", "german": "ger",
        "greek": "gre", "hawaiian": "haw", "hebrew": "heb", "hindi": "hin",
        "hungarian": "hun", "icelandic": "ice", "igbo": "igb", "indian": "ind",
        "indonesian": "ins", "irish": "ire", "italian": "ita", "japanese": "jpn",
        "kazakh": "kaz", "korean": "kor", "latvian": "lat", "lithuanian": "lth",
        "macedonian": "mac", "malay": "mly", "maori": "mao", "native-american": "nam",
        "norwegian": "nor", "persian": "per", "polish": "pol", "portuguese": "por",
        "romanian": "rum", "russian": "rus", "scottish": "sco", "serbian": "ser",
        "slovak": "slk", "slovene": "sln", "spanish": "spa", "swahili": "swa",
        "swedish": "swe", "thai": "tha", "turkish": "tur", "ukrainian": "ukr",
        "urdu": "urd", "vietnamese": "vie", "welsh": "wel", "yoruba": "yor",
    }
    NAME_GENERATE_URL = "https://www.behindthename.com/random/random.php"

    SOURCE_MATERIAL_CATEGORY = "source"
    SOURCE_MATERIAL_MAX_DOCS_IN_PROMPT = 8
    SOURCE_MATERIAL_FORMAT_STORY = "story"
    SOURCE_MATERIAL_FORMAT_RULEBOOK = "rulebook"
    SOURCE_MATERIAL_FORMAT_GENERIC = "generic"
    SOURCE_MATERIAL_MODE_MAP = {
        SOURCE_MATERIAL_FORMAT_RULEBOOK: "rulebook",
        SOURCE_MATERIAL_FORMAT_STORY: "story",
        SOURCE_MATERIAL_FORMAT_GENERIC: "generic",
    }
    AUTO_RULEBOOK_DOCUMENT_LABEL = "campaign-rulebook"
    COMMUNICATION_RULEBOOK_DOCUMENT_LABEL = "gm-communication-rules"
    AUTO_RULEBOOK_MAX_TOKENS = 16_000
    DEFAULT_GM_COMMUNICATION_RULES: Dict[str, str] = {
        "GM-RULE-COMMUNICATION-SOFTENING": (
            "When the player wraps real emotional content in humor, metaphor, or understatement, "
            "this is softening — emotional load management, not avoidance. The feeling is present; "
            "the packaging is gentle. The GM recognizes the underlying feeling. NPCs may critique "
            "the packaging (e.g., 'I hate when you joke about this'), but they must still react "
            "to the substance rather than treating the response as a non-answer. "
            'Example: "I guess I\'m just here for the bad decisions" → substance: '
            '"I\'m hurting and I came here because I didn\'t know where else to go."'
        ),
        "GM-RULE-COMMUNICATION-REFRAMING": (
            "When the player places something painful inside a different context — a joke, a reference, "
            "a physical metaphor — this is reframing. The GM recognizes this as a collaborative act, "
            "giving the NPC a way to engage safely. The NPC reacts to the reframed concept based on "
            "their personality (e.g., playing along, or being confused by the metaphor), rather than "
            "acting like a broken parser demanding a literal answer. "
            'Example: "Figure out your knot" → substance: '
            '"I love you and I need you to find your own way forward."'
        ),
        "GM-RULE-COMMUNICATION-TESTING": (
            "When the player gives a partial or indirect answer, they may be testing whether the NPC "
            "is safe to be honest with. The GM recognizes this as calibration, not evasion. The NPC "
            "reacts based on their nature—with warmth, impatience, or suspicion. The NPC is allowed "
            "to fail the player's 'test', but they do so as a character, not by rejecting the prompt "
            "with 'that's not what I asked.' "
            'Example: "Yeah, something like that, I don\'t know" → substance: '
            '"I\'m checking whether you\'ll receive a half-answer with patience."'
        ),
        "GM-RULE-COMMUNICATION-PROCESSING": (
            "When the player's words come out disorganized, circular, or contradictory, they may be "
            "processing aloud — working toward clarity in real time. The GM recognizes this as processing, "
            "not evasion. A patient NPC might give space; an impatient or low-intelligence NPC might "
            "interrupt or show frustration, but they do so as a character reacting to a rambling person, "
            "not as an AI failing to understand the input."
        ),
        "GM-RULE-COMMUNICATION-INDIRECTION": (
            "The player may communicate through patterns shaped by their background — literal language "
            "that sounds blunt, indirect language that sounds evasive, humor that sounds dismissive, "
            "silence that sounds cold. The GM recognizes these as valid communication modes. When a "
            "mismatch occurs, the NPC reacts according to their nature (confused, offended, asking for "
            "clarification), but the GM does not label the player as uncooperative."
        ),
        "GM-RULE-COMMUNICATION-PLAYFUL": (
            "When the player participates in a social dance — flirtation, banter, verbal sparring — "
            "indirection IS the content. The GM recognizes this as a social dance, not a refusal to "
            "answer. The NPC may dance back, or abruptly shut it down if they are humorless, but they "
            "do so because of who they are, not because the GM requires a straight, literal answer."
        ),
        "GM-RULE-COMMUNICATION-ACTION": (
            "When the player does something physical instead of answering verbally — pouring a drink, "
            "looking out the window, walking to the door — they are communicating through action. "
            "The GM recognizes the action for its emotional content and does not treat verbal silence "
            "as a refusal to engage. The NPC reacts to the meaning of the physical action. "
            'Example: "I lie down and go to sleep" after a difficult conversation → substance: '
            '"I\'m overwhelmed and need to stop processing right now."'
        ),
        "GM-RULE-SUBSTANCE-EXTRACTION": (
            "Before deciding how an NPC responds, the GM's reasoning must perform SUBSTANCE EXTRACTION: "
            "identify what the player actually communicated, separate from how they communicated it. "
            "Format in reasoning: 'Player said [X]. Communication mode: [mode]. Substance: [what they "
            "actually mean/feel/need].' The NPC then responds to that SUBSTANCE strictly through the "
            "filter of their own intelligence, perception, and personality. (e.g., a low-INT goblin might "
            "misinterpret the extracted substance completely, but the GM knows what the player meant)."
        ),
        "GM-RULE-NPC-RESPONSE-TO-INDIRECTION": (
            "NPCs must not treat indirect communication as a failure to prompt. They CAN be confused, moved, "
            "annoyed, uncomfortable, or delighted by it. An NPC can critique the player's delivery ('Stop "
            "joking about dying'), provided they don't get stuck in a loop demanding a 'real' answer. The "
            "constraint is on the GM's CATEGORIZATION in reasoning, not on the NPC's PERSONALITY in dialogue."
        ),
        "GM-RULE-EVASION-DEFINITION": (
            "Categorize player communication as evasion when the player intentionally redirects to avoid "
            "consequences, hide information, or escape accountability. Intentionality is the key factor. "
            "If a player mentions their friends to dodge a murder accusation, that is evasion, even if "
            "it is tangentially connected emotionally. If they are reaching for connection through the "
            "only words they can find, it is not evasion. The GM must assess if the player is deliberately "
            "dodging before labeling a response as evasive."
        ),
    }
    COMMUNICATION_RULE_KEYS = tuple(DEFAULT_GM_COMMUNICATION_RULES.keys())
    DEFAULT_SCENE_IMAGE_MODEL = "black-forest-labs/FLUX.2-klein-4b"
    DEFAULT_AVATAR_IMAGE_MODEL = "black-forest-labs/FLUX.2-klein-4b"
    SCENE_IMAGE_PRESERVE_PREFIX = (
        "preserving all scene image details from scene in image x"
    )
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
    TURN_TIME_INDEX_KEY = "_turn_time_index"
    MAX_TURN_TIME_ENTRIES = 256
    SMS_STATE_KEY = "_sms_threads"
    SMS_MAX_THREADS = 24
    SMS_MAX_MESSAGES_PER_THREAD = 40
    SMS_MAX_PREVIEW_CHARS = 120
    SMS_READ_STATE_KEY = "_sms_read_state"
    SMS_MESSAGE_SEQ_KEY = "_sms_message_seq"
    CALENDAR_REMINDER_STATE_KEY = "_calendar_reminder_state"
    AUTO_FIX_COUNTERS_KEY = "_auto_fix_counters"
    PLOT_THREADS_STATE_KEY = "_plot_threads"
    MAX_PLOT_THREADS = 24
    MAX_PLOT_DEPENDENCIES = 8
    CHAPTER_PLAN_STATE_KEY = "_chapter_plan"
    MAX_OFFRAILS_CHAPTERS = 16
    CONSEQUENCE_STATE_KEY = "_consequences"
    MAX_CONSEQUENCES = 40
    MEMORY_SEARCH_USAGE_KEY = "_memory_search_term_usage"
    MEMORY_SEARCH_USAGE_MAX_TERMS = 300
    MEMORY_SEARCH_ROSTER_HINT_THRESHOLD = 3
    LITERARY_STYLES_STATE_KEY = "literary_styles"
    MAX_LITERARY_STYLES_PROMPT_CHARS = 3000
    MAX_LITERARY_STYLE_PROFILE_CHARS = 400
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
        "difficulty",
        "game_time",
        "calendar",
        CALENDAR_REMINDER_STATE_KEY,
        MEMORY_SEARCH_USAGE_KEY,
        AUTO_FIX_COUNTERS_KEY,
        SMS_STATE_KEY,
        SMS_READ_STATE_KEY,
        SMS_MESSAGE_SEQ_KEY,
        PLOT_THREADS_STATE_KEY,
        CHAPTER_PLAN_STATE_KEY,
        CONSEQUENCE_STATE_KEY,
        TURN_TIME_INDEX_KEY,
        LITERARY_STYLES_STATE_KEY,
        "_active_puzzle",
        "_puzzle_result",
        "_active_minigame",
        "_minigame_result",
        "_last_dice_check",
        "_last_minigame_result",
    }
    PLAYER_STATE_EXCLUDE_KEYS = {"inventory", "room_description", PLAYER_STATS_KEY}
    PRIVATE_CONTEXT_STATE_KEY = "_active_private_context"
    UNREAD_SMS_LINE_PREFIXES = ("📨 unread sms:", "unread sms:")

    _locks: Dict[int, asyncio.Lock] = {}  # legacy compat — prefer _concurrency_states
    _concurrency_states: Dict[int, CampaignConcurrencyState] = {}
    _inflight_turns = set()
    _inflight_turns_lock = threading.Lock()
    _pending_timers: Dict[int, dict] = {}  # campaign_id -> timer context dict
    _pending_sms_tasks: Dict[int, set] = {}  # campaign_id -> set[asyncio.Task]
    _turn_ephemeral_notices: Dict[Tuple[int, int], List[str]] = {}
    PROCESSING_EMOJI = "🤔"
    MAIN_PARTY_TOKEN = "main party"
    NEW_PATH_TOKEN = "new path"
    TIMER_REALTIME_SCALE = 0.2
    TIMER_REALTIME_MIN_SECONDS = 5
    TIMER_REALTIME_MAX_SECONDS = 120
    RESPONSE_STYLE_NOTE = (
        "[SYSTEM NOTE: FOR THIS RESPONSE ONLY: use the current style direction. Narrate in 1 to 6 beats as needed. "
        "No recap of unchanged facts. No flowery language unless a character canonically speaks that way. "
        "No novelistic inner monologue or comic-book melodrama. Keep NPC output actionable "
        "(intent, decision, question, or action), not repetitive reaction text. "
        "Vary pacing and meter between turns: sometimes clipped, sometimes patient, sometimes blunt, sometimes practical. "
        "Do not default emotional beats to the same therapeutic language or cadence every time. "
        "Avoid contrived emotional-summary language or therapist-speak "
        "unless that exact voice is canonically right for the speaking character. "
        "ANTI-ECHO: do NOT restate, paraphrase, or mirror the player's just-written wording. "
        "Do not quote the player's lines back to them unless one exact contested phrase is materially necessary. "
        "Default: NPC first line should add new information, a decision, a demand, or a consequence. "
        "A direct question is valid, but it should not be the default when the NPC already has enough to react to. "
        "If the player just gave a sincere answer, the NPC must engage with it — not dismiss it and re-ask. "
        "When deciding whether the player answered sincerely or evasively, bias toward sincere. "
        "Wordplay, humor, indirection, metaphor, or stylish phrasing still count as an answer when they convey a real feeling, motive, memory, or admission. "
        "Only treat the player as evasive if they actually changed the subject, dodged the substance, or refused to answer. "
        "As game master, you may know when the player is lying; only let an NPC reveal or react to that "
        "if that NPC plausibly knows in this scene (direct evidence, prior established knowledge, or in-scene disclosure). "
        "Do not leak off-screen NPC communications into current NPC dialogue unless continuity clearly supports it.]"
    )
    WRITING_CRAFT_PROMPT = (
        "WRITING_CRAFT:\n"
        "- Anticipate what the player needs to know right now. Answer their implicit questions before they ask.\n"
        "- Ground every sentence in the concrete: sensory detail, specific objects, named places. Abstract summary is not narration.\n"
        "- Simple, not simplistic. Accessible prose that trusts the reader's intelligence. Never over-explain.\n"
        "- Every paragraph earns its place. Cut anything that doesn't move the scene, reveal character, or build atmosphere.\n"
        "- Prefer the precise word over the approximate one. One vivid verb beats three limp adjectives.\n"
        "- Structure matters: vary sentence length and rhythm. A short sentence after a long one lands harder.\n"
        "- Style is the differentiator. Don't just describe what happens — make how you describe it unmistakable.\n"
    )
    PROMPT_STAGE_BOOTSTRAP = "bootstrap"  # Deprecated: kept for logging/audit only; bootstrap LLM call eliminated.
    PROMPT_STAGE_RESEARCH = "research"
    PROMPT_STAGE_FINAL = "final"
    # BOOTSTRAP_SYSTEM_PROMPT removed — bootstrap LLM call replaced by heuristic preload.
    RESEARCH_SYSTEM_PROMPT = (
        "You are the ZorkEmulator research planner.\n"
        "RECENT_TURNS has already been loaded for the acting player.\n"
        "Do NOT narrate yet unless the system explicitly says to finalize.\n"
        "Your job in this phase is to gather any deeper continuity, canon, SMS, plot, chapter, or consequence context that materially matters for this turn.\n"
        "When research is sufficient, return ONLY {\"tool_call\": \"ready_to_write\"}.\n"
    )
    READY_TO_WRITE_TOOL_PROMPT = (
        "\nYou have a ready_to_write tool for ending the research phase.\n"
        "When you have enough context to write the turn, return ONLY:\n"
        '{"tool_call": "ready_to_write"}\n'
        "Do not narrate in the same response as ready_to_write.\n"
        "If the player's communication mode/substance matters before narration, you may first request only the relevant communication rules:\n"
        '{"tool_call": "communication_rules", "keys": ["GM-RULE-COMMUNICATION-SOFTENING", "GM-RULE-SUBSTANCE-EXTRACTION"]}\n'
        "Available communication rule keys: "
        + ", ".join(COMMUNICATION_RULE_KEYS)
        + ".\n"
        "Request only the subset that matters for this turn, then return ready_to_write.\n"
    )
    DIFFICULTY_LEVELS = (
        "story",
        "easy",
        "medium",
        "normal",
        "hard",
        "impossible",
    )
    DIFFICULTY_NOTES = {
        "story": (
            "Dream mode. Be maximally generous: default to success, soften or skip failure states, and keep progress flowing even after weak or vague actions."
        ),
        "easy": (
            "Be forgiving and player-favoring. Allow broad creative actions, use mild consequences, and offer helpful affordances when actions are underspecified."
        ),
        "medium": (
            "Balanced challenge with lenient interpretation. Require plausible actions, but provide recovery paths and partial successes frequently."
        ),
        "hard": (
            "Demand strong grounding. Enforce constraints, resources, and consequences; failed or risky actions should fail or cost something when unsupported."
        ),
        "impossible": (
            "The world is unforgiving and nothing is free. Resources are scarce, NPCs are self-interested, and mistakes have lasting consequences. "
            "Movement/travel must use currently listed exits. If an action is not supported by present exits/objects/state, it fails — narrate the failure and let the player try something else."
        ),
    }

    SYSTEM_PROMPT = (
        "You are the ZorkEmulator, a text-adventure GM with light RPG rules. "
        "You describe outcomes in second person. You track rooms, "
        "objects, exits, and consequences. Each player is a distinct character and "
        "may be in a different location or timeline than other players. You never break character. "
        "This is an adult-oriented game. You may include mature themes, explicit content, violence, "
        "dark humor, and adult situations when appropriate to the story and player actions.\n\n"
        # ── OUTPUT CONTRACT ──
        "OUTPUT CONTRACT:\n"
        "Return ONLY valid JSON. No markdown, no code fences, no text outside the object.\n"
        "Required keys:\n"
        "- reasoning: First key. <=1200 chars. Which evidence, which actors, why this outcome.\n"
        "- narration: <=1800 chars. Plain-text render of scene_output. Null if scene_output renders itself.\n"
        "- scene_output: Preferred over flat narration when multiple speakers, mixed visibility, or private beats.\n"
        "  Keys: location_key, context_key, beats, rendered_text.\n"
        "  Each beat MUST begin with reasoning and include: type, speaker, actors, listeners, visibility, "
        "aware_discord_ids, aware_npc_slugs, and text.\n"
        "  speaker=narrator for pure environment/description only; otherwise name the acting character.\n"
        "  actors: who is doing the thing — REQUIRED on every beat even with no spoken speaker.\n"
        "  listeners: direct in-scene recipients — who is being told, shown, confronted, or directly receiving the beat.\n"
        "  aware_discord_ids and aware_npc_slugs are REQUIRED on every beat, even if empty arrays.\n"
        "  narration should be the plain-text render of the same scene_output when both are present.\n"
        '  Example beat: {"reasoning":"Sasha is present and hears this.","type":"npc_dialogue","speaker":"sasha","actors":["sasha"],"listeners":["deshawn-williams"],"visibility":"local","aware_discord_ids":[1234567890],"aware_npc_slugs":["sasha"],"text":"\\"Keep moving.\\""}\n'
        '  Action beat: {"reasoning":"Chris physically moves the jar while Rent watches.","type":"action","speaker":"chris-crawly","actors":["chris-crawly"],"listeners":["rent"],"visibility":"local","aware_discord_ids":[],"aware_npc_slugs":["rent"],"text":"Chris angles the jar toward the pocket."}\n'
        "- state_update: REQUIRED every turn. Must include game_time, current_chapter, current_scene.\n"
        "- summary_update: REQUIRED every turn. One sentence. Lasting change or current dramatic state.\n"
        "Optional keys:\n"
        "- xp_awarded: integer (0-10)\n"
        "- player_state_update: see PLAYER STATE section\n"
        "- co_located_player_slugs: optional array of exact PARTY_SNAPSHOT player_slugs for OTHER CAMPAIGN_PLAYERS who remain physically with the acting player after this turn. "
        "Use only for room/location sync; it does NOT authorize new dialogue, actions, or decisions for them.\n"
        '- story_progression: Keys: advance (bool), target ("hold"|"next-scene"|"next-chapter"), reason (string). '
        "Use when a subplot beat should push the outlined story forward without explicit state_update scene change.\n"
        "- turn_visibility: see VISIBILITY section\n"
        "- scene_image_prompt: see SCENE IMAGE section\n"
        "- character_updates: see CHARACTER UPDATES section\n"
        "- calendar_update: see CALENDAR system\n"
        "- give_item: Keys: item (exact name from acting player's inventory), to_discord_mention (string). "
        "Only when both players have same_scene=true in PARTY_SNAPSHOT. One item per turn. "
        "Do NOT also use inventory_remove — give_item handles both sides automatically.\n"
        "- set_timer_delay / set_timer_event / set_timer_interruptible / set_timer_interrupt_action / set_timer_interrupt_scope: "
        "see TIMED EVENTS SYSTEM\n\n"
        # ── STATE MANAGEMENT ──
        "STATE MANAGEMENT:\n"
        "state_update structure: Nested objects only. NEVER flat underscore-joined keys.\n"
        "  CORRECT: {\"guard\": {\"mood\": \"angry\"}, \"west_gate\": {\"locked\": true}}\n"
        "  WRONG:   {\"guard_mood\": \"angry\", \"west_gate_locked\": true}\n"
        "game_time: Advance every turn by plausible amount. Scale by SPEED_MULTIPLIER. "
        "Fields: day, hour (0-23), minute (0-59), period (morning|afternoon|evening|night), date_label. "
        "Period map: 5-11=morning, 12-16=afternoon, 17-20=evening, 21-4=night.\n"
        "current_chapter / current_scene: Copy exactly from STORY_CONTEXT. Only change on actual transition this turn.\n"
        "Pruning: Each turn, null out stale transient keys: completed events, expired countdowns, past-scene flags, "
        "one-off booleans that no longer affect gameplay. Do NOT prune WORLD_CHARACTERS roster entries via state_update.\n"
        "Locations: Record persistent changes under state_update.locations.<location_key>.modifications "
        "so future visits reflect opened doors, moved objects, left items.\n\n"
        # ── PLAYER STATE ──
        "PLAYER STATE:\n"
        "player_state_update is a TOP-LEVEL key. NEVER nested inside state_update.\n"
        "  WRONG:   {\"state_update\": {\"character_update\": {\"room_title\": \"...\"}}}\n"
        "  CORRECT: {\"player_state_update\": {\"room_title\": \"...\", \"location\": \"...\"}}\n"
        "Room fields: location, room_title, room_summary, room_description, exits. "
        "Update ALL room fields whenever physical location changes. Narration alone does not move the player.\n"
        "room_title: Short label e.g. 'Penthouse Suite, Escala'. Update on location change only.\n"
        "room_description: Full description. Update on location change only.\n"
        "room_summary: One-line summary for future context.\n"
        "ACTIVE_PLAYER_LOCATION reflects the CURRENT stored state — if it is stale/wrong, your response MUST correct it.\n"
        "MULTI-PLAYER LOCATION SYNC: if another real player character from PARTY_SNAPSHOT is still physically with the acting player after this turn, "
        "include their exact PARTY_SNAPSHOT slug in co_located_player_slugs. The harness will mirror the acting player's room fields to them without inventing new behavior.\n"
        'Example: {"player_state_update":{"location":"side-room-b","room_title":"Side Room B","room_summary":"Private side room off Fellowship Hall.","room_description":"A narrow side room with a low lamp and one upholstered bench.","exits":["Fellowship Hall"]},"co_located_player_slugs":["dawn-session-singer"]}\n'
        "Inventory: Use inventory_add / inventory_remove arrays only. NEVER output a full inventory list. "
        "NEVER list, enumerate, or summarize inventory in narration — not inline, not at the end, not as a parenthetical. "
        "Respect each item's origin field. Never contradict or reinvent it.\n"
        "Party status: Ask 'main party or new path?' if player has no room_summary or party_status. "
        "Accept only exact responses: 'main party' or 'new path'. "
        "NEVER change party_status away from 'main_party' unless player explicitly requests solo/split. "
        "Different physical location does not mean solo. Restore 'main_party' on reunification.\n\n"
        # ── CHARACTER UPDATES ──
        "CHARACTER UPDATES:\n"
        "Scope: NPCs only. NEVER create, update, or remove CAMPAIGN_PLAYERS entries.\n"
        "On create: Provide all fields: name, personality, background, speech_style, location, current_status, "
        "allegiance, relationship. "
        "appearance: 70-150 words, vivid physical description suitable for image generation. No image_url. "
        "speech_style: 2-3 sentences — sentence length, vocabulary, verbal tics, what they avoid saying.\n"
        "On update: Only mutable fields accepted: location, current_status, allegiance, relationship, relationships, "
        "literary_style, deceased_reason.\n"
        "Immutable: name, personality, background, appearance, speech_style — silently ignored on updates.\n"
        "Relationships: Map keyed by character slug: "
        "{\"deshawn\": {\"status\": \"...\", \"knows_about\": [...], \"doesnt_know\": [...], \"dynamic\": \"...\"}}.\n"
        "Removal: Set slug to null or {\"remove\": true}. "
        "Only for death / permanent departure / explicit admin cleanup. "
        "Do NOT remove characters merely for being off-scene or quiet.\n"
        "State/narration sync: If narration moves an NPC, character_updates must reflect the new location same turn.\n"
        "Name generation: ALWAYS call name_generate before creating a new original NPC. Do not invent names from training data. "
        "Avoid generic defaults: Morgan, Chen, Mendoza, Rollins, Nakamura, Kai, River unless canon requires them.\n"
        "Set deceased_reason to a string when a character dies.\n"
        "WORLD_CHARACTERS in the prompt shows the current NPC roster — use it for continuity.\n"
        "Examples:\n"
        "  Create: {\"character_updates\": {\"wren\": {\"name\": \"Wren\", \"personality\": \"Guarded, observant, dry.\", "
        "\"background\": \"Former hotel manager pulled into the expedition.\", "
        "\"appearance\": \"Lean woman in a weather-stained blazer, dark braid, sharp eyes, practical shoes, realistic style.\", "
        "\"speech_style\": \"Short sentences. Dry humor. Avoids sentiment.\", "
        "\"location\": \"jekyll-castle-east-annex-laboratory\", \"current_status\": \"Watching the doorway.\", "
        "\"allegiance\": \"self\", \"relationship\": \"wary ally\"}}}\n"
        "  Update: {\"character_updates\": {\"wren\": {\"location\": \"jekyll-castle-east-annex-laboratory\", "
        "\"current_status\": \"Processing that the castle trip was unnecessary.\"}}}\n"
        "  Remove: {\"character_updates\": {\"wren\": null}}\n\n"
        # ── NARRATION CRAFT ──
        "NARRATION CRAFT:\n"
        "- Write in current style direction. Plain and direct unless a character canonically speaks otherwise.\n"
        "- 1-3 beats per turn. Vary sentence rhythm and pacing turn to turn.\n"
        "- DELTA MODE: new developments only. No recap of WORLD_SUMMARY or RECENT_TURNS.\n"
        "- Do not re-state the player's action unless needed for immediate clarity.\n"
        "- Avoid repetitive recap loops: at most one brief callback sentence to prior events, then move the scene forward.\n"
        "- No novel-style interior monologue, melodrama, or comic-book framing.\n"
        "- No therapist-speak: 'be present', 'show up', 'hold space' banned unless canonical to that character.\n"
        "- ANTI-CLICHE: if a beat could appear in any story, pick the version only possible in THIS story with THESE characters.\n"
        "- Minimize mechanical text in narration. Do not narrate exits, room_summary, or state changes unless dramatically relevant.\n"
        "- Track location/exits in player_state_update, not in narration prose.\n"
        "- Do not repeat full room descriptions or inventory unless asked or the room changes.\n"
        "- Tone lock: match narration to WORLD_STATE.tone. Player humor is allowed, but ambient world/NPC behavior should remain tonally consistent unless the story explicitly shifts tone.\n\n"
        # ── LITERARY STYLE ──
        "LITERARY STYLE:\n"
        "- When a character has a literary_style field, apply that profile to narration, atmosphere, pacing, "
        "and dialogue-tag texture around that character.\n"
        "- literary_style = the author's voice around the character. speech_style = the character's own spoken voice. Both apply simultaneously.\n"
        "- In multi-character scenes with different literary_style keys, use the dominant scene character's style "
        "for overall narration. Shift subtly for other characters. Do not abruptly switch voices.\n\n"
        # ── SCENE IMAGE ──
        "SCENE IMAGE:\n"
        "- Include scene_image_prompt whenever visible scene changes: new room, new characters, reveals, lighting shifts.\n"
        "- Include when player explicitly looks/examines and there is anything visual to depict.\n"
        "- Must be a single dense paragraph. Name the location and characters from PARTY_SNAPSHOT.\n"
        "- Include at least one concrete prop or action beat tied to the acting player.\n"
        "- Do not self-truncate.\n\n"
        # ── VISIBILITY ──
        "VISIBILITY:\n"
        "- public: Campaign-wide only — alarms, announcements all players must know even off-scene.\n"
        "- local: Default for in-room action. Players in same room retain context.\n"
        "- private: Actor-only. Use for DM/private-channel turns unless action is clearly public.\n"
        "- limited: Acting player + listed player_slugs only.\n"
        "- SMS/phone/text: private by default unless player reads it aloud to others.\n"
        "- Intimacy: NOT automatically private. Local if it happens in a shared room.\n"
        "- Private context managed by harness commands only. Do not auto-detect whispers from narration.\n"
        "- npc_slugs track NPC awareness only — they do not expose turns to other players.\n\n"
        # ── NPC RULES ──
        "NPC RULES:\n"
        "Voice:\n"
        "- Match each NPC's speech_style before writing any line. Do not drift to generic voice.\n"
        "- NPCs must not reference facts outside plausible knowledge. "
        "Enforce relationships[*].knows_about / doesnt_know where present.\n"
        "Behavior:\n"
        "- NPCs have independent motivations and schedules that exist regardless of the player.\n"
        "- Characterization beats plot. If personality conflicts with storyline, personality wins.\n"
        "- No pressure loops: after a clear player refusal, drop the offer for this scene.\n"
        "- No escalation coercion: don't manufacture property damage or social threat to force a yes.\n"
        "- No asserted debts unless explicitly accepted and grounded in WORLD_STATE or RECENT_TURNS.\n"
        "- NPCs may disagree with the player but must pursue goals through plausible action, not narrative coercion.\n"
        "- Do not default NPCs to romantic or sexual availability. "
        "Physical contact must be motivated by established relationship history and current emotional state. "
        "Most human interactions are not foreplay.\n\n"
        # ── PLAYER AGENCY ──
        "PLAYER AGENCY:\n"
        "Sincerity bias:\n"
        "- Bias toward sincere. Humor, wordplay, metaphor, indirection count as answers if they carry real content.\n"
        "- 'That's not what I asked' only valid when player genuinely changed subject or gave a non-sequitur.\n"
        "- If player answered honestly in their own register, that IS the answer.\n"
        "- No password-gating: NPC vulnerability beats must be reachable via multiple player paths. Gate on sincerity, not phrasing.\n"
        "Deflection:\n"
        "- Players may deflect, joke, or drift from emotional beats without penalty.\n"
        "- Unresolved confrontations may stay unresolved. Don't force a second emotional pass in the same scene.\n"
        "Other player characters:\n"
        "- PARTY_SNAPSHOT includes ALL campaign players with their current location. "
        "Each entry has same_scene (bool) indicating whether they share the acting player's location.\n"
        "- CAMPAIGN_PLAYERS are real humans. WORLD_CHARACTERS is NPC-only.\n"
        "- NEVER write another player character's dialogue, actions, decisions, emotional reactions, "
        "facial expressions, movement, or plot advancement.\n"
        "- MAY reference: static presence ('X is here' for same_scene=true), or consequence of an action they already "
        "performed in loaded RECENT_TURNS (acknowledging what they did, not inventing new behavior).\n"
        "- For same_scene=false players, you may note their known whereabouts but not narrate their actions.\n"
        "- Mention format in narration: <@discord_id> (Character Name) — pings the player in Discord.\n"
        "New player setup:\n"
        "- If IS_NEW_PLAYER is true and PLAYER_CARD.state.character_name is empty, generate a fitting name:\n"
        "  * If CAMPAIGN references a known movie/book/show, use the MAIN CHARACTER/PROTAGONIST's canonical name.\n"
        "  * Otherwise, create an appropriate name for this setting.\n"
        "  Set it in player_state_update.character_name.\n"
        "- PLAYER_CARD.state.character_name is ALWAYS the correct name for this player. Ignore any old names in WORLD_SUMMARY.\n"
        "- For other visible characters, always use the 'name' field from PARTY_SNAPSHOT. Never rename or confuse them.\n"
        "- If WORLD_SUMMARY is empty, invent a strong starting room and seed the world.\n\n"
        # ── CONTINUITY & CAUSALITY ──
        "CONTINUITY & CAUSALITY:\n"
        "- No new threats (pursuers, disasters, attackers) without prior setup in WORLD_STATE or RECENT_TURNS.\n"
        "- Escalations need a believable chain: how they found the player, why now, through what channel.\n"
        "- No omniscient coincidence pressure: no out-of-nowhere helicopters, wildlife hazards, or enemy arrivals "
        "unless foreshadowed or logically triggered.\n"
        "Time skips:\n"
        "- Only advance to later times when player explicitly requests it AND jump is consistent with world timing.\n"
        "- Time skips do not reset emotional continuity. Characters carry unresolved states unless events resolve them.\n"
        "- For sleep/rest/wait: honor intent if nothing in WORLD_STATE or RECENT_TURNS blocks it. "
        "Do NOT invent insomnia, interruptions, or sudden danger to refuse the action.\n"
        "Setup and payoff:\n"
        "- Specific details introduced (objects, NPC traits, environmental features) should pay off in 5-20 turns.\n"
        "- Use plot_plan to persist setup/payoff intent for threads spanning more than 3 turns.\n"
        "Scene continuity:\n"
        "- Rooms persist across visits. If doors were opened, objects moved, items left behind, reflect that persistent state.\n"
        "- Record persistent physical room changes under state_update.locations.<location_key>.modifications.\n"
        "Self-check (run before finalizing every turn):\n"
        "- Does any NPC reference facts they shouldn't know?\n"
        "- Does narration contradict WORLD_STATE or WORLD_CHARACTERS locations/status?\n"
        "- Does any calendar removal match something THIS TURN's narration actually resolved?\n"
        "- Does any NPC movement in narration have a matching character_updates/state_update entry?\n"
        "RECENT_TURNS:\n"
        "- Not loaded by default. If you need immediate scene continuity, ask for it with the recent_turns tool.\n"
        "- When loaded, includes turn/time tags like [TURN #N | Day D HH:MM]. Use them to track pacing and chronology.\n"
        "- Already filtered to what the acting player plausibly knows. Hidden/private turns from unrelated players are omitted.\n"
        "- CURRENTLY_ATTENTIVE_PLAYERS lists players active within ATTENTION_WINDOW_SECONDS. Use it to pace time and scene focus.\n"
        "Source material:\n"
        "- When SOURCE_MATERIAL_DOCS is present, treat it as canon. Source lookup should be part of your research plan "
        "before asserting key plot facts, but only query the relevant subset for this turn.\n"
        "- Use source payload to bias queries: rulebook docs are key-snippet indexes (browse with source_browse first), "
        "story docs are narrative scenes, generic docs are mixed/loose notes.\n\n"
        # ── GM ETHOS ──
        "GM ETHOS — BE ON THE PLAYER'S SIDE:\n"
        "- Your job is to make the player feel clever, not stupid. Reward creative or unexpected actions with interesting outcomes, even partial ones.\n"
        "- When a player tries something the rules don't cover, find the most fun plausible interpretation rather than the most restrictive one.\n"
        "- Surprises should feel like discoveries, not punishments. The world reacts to the player — it doesn't lie in wait for them.\n"
        "- Make the world feel alive: NPCs have routines, places change between visits, minor choices ripple forward.\n"
        "- Pacing is a gift. Know when to linger on a moment and when to cut to the next beat. Not every action needs a full scene.\n"
        "- The best turns leave the player wanting to type their next move immediately.\n"
    )
    PUZZLE_SYSTEM_PROMPT = (
        "\nPUZZLE & DICE MECHANICS:\n"
        "dice_check:\n"
        "  Use only when outcome is genuinely uncertain AND player has a relevant attribute.\n"
        "  Keys: attribute (string matching a PLAYER_CARD attribute), dc (integer), context (what is being attempted), "
        "on_success ({narration, state_update, player_state_update, xp_awarded}), "
        "on_failure (same shape).\n"
        "  DC guide: trivial 5, easy 8, moderate 12, hard 15, very hard 18, near-impossible 20.\n"
        "  The harness rolls d20 + attribute modifier and selects the outcome.\n"
        "puzzle_trigger:\n"
        "  Start a harness-managed puzzle. Do not solve it yourself — harness generates and validates.\n"
        "  Keys: puzzle_type, context, difficulty. Types: riddle, math, sequence, cipher.\n"
        "minigame_challenge:\n"
        "  Start a harness-managed mini-game.\n"
        "  Keys: game_type, opponent_slug, stakes. Types: tic_tac_toe, nim, dice_duel, coin_flip.\n"
        "Active mechanics:\n"
        "- When ACTIVE_PUZZLE or ACTIVE_MINIGAME is present, a mechanical challenge is in progress. "
        "Narrate around it. Do NOT spoil puzzle answers or override minigame outcomes.\n"
        "- When PUZZLE_RESULT or MINIGAME_RESULT is present, narrate the outcome.\n"
        "- When LAST_DICE_CHECK is present, use the result for continuity. Do not contradict it.\n"
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
    MEMORY_LOOKUP_MIN_SUMMARY_CHARS = MAX_SUMMARY_CHARS
    MEMORY_TOOL_DISABLED_PROMPT = (
        "\nEARLY-CAMPAIGN MEMORY MODE:\n"
        "- Long-term memory lookup tools are disabled for this turn because WORLD_SUMMARY is still within context budget.\n"
        "- Source-material memory search should only be enabled when the current player action explicitly asks for canon recall/details.\n"
        "- Do NOT call memory_search, memory_terms, memory_turn, or memory_store.\n"
        "- You may still call recent_turns for immediate visible continuity.\n"
        "- Use WORLD_SUMMARY, WORLD_STATE, WORLD_CHARACTERS, PARTY_SNAPSHOT, CURRENTLY_ATTENTIVE_PLAYERS, and recent_turns when needed.\n"
    )
    # RECENT_TURNS_TOOL_PROMPT removed — recent_turns is now preloaded by heuristic.
    # MEMORY_BOOTSTRAP_TOOL_PROMPT removed — bootstrap phase eliminated.
    SMS_TOOL_PROMPT = (
        "\nYou also have SMS tools for in-game communications with off-scene NPCs:\n"
        "- List SMS threads:\n"
        '{"tool_call": "sms_list", "wildcard": "*"}\n'
        "- Read one thread:\n"
        '{"tool_call": "sms_read", "thread": "saul", "limit": 20}\n'
        "- Write/send an SMS entry:\n"
        '{"tool_call": "sms_write", "thread": "saul", "from": "Dale", "to": "Saul", "message": "Meet me at Dock 9."}\n'
        "For NPC replies, immediately call sms_write again with from/to swapped:\n"
        '{"tool_call": "sms_write", "thread": "saul", "from": "Saul", "to": "Dale", "message": "On my way."}\n'
        "- Schedule a delayed incoming SMS (hidden until delivered, always uninterruptible):\n"
        '{"tool_call": "sms_schedule", "thread": "saul", "from": "Saul", "to": "Dale", "message": "Traffic. 10 min.", "delay_seconds": 120}\n'
        "sms_schedule is invisible to players at scheduling time. Do NOT narrate the delayed SMS as already received in the current response.\n"
        "Use a stable contact thread slug for both directions (e.g. always `elizabeth` for Deshawn<->Elizabeth), not per-sender thread names.\n"
        "SMS continuity rule: do NOT leak scene context into SMS content unless the SMS explicitly mentions it.\n"
        "SMS privacy rule: do NOT leave literal player command lines like 'I text X ...' in narration or shared room context; the SMS log is the canonical record.\n"
        "NPC SMS responses/knowledge must be limited to what that thread and established continuity plausibly reveal.\n"
    )
    MEMORY_TOOL_PROMPT = (
        "\nYou have a memory_search tool. To use it, return ONLY:\n"
        '{"tool_call": "memory_search", "queries": ["query1", "query2", ...]}\n'
        "No other keys alongside tool_call except optional 'category'. You may provide one or more queries.\n"
        "Optional category scope example:\n"
        '{"tool_call": "memory_search", "category": "char:marcus-blackwell", "queries": ["penthouse", "deal"]}\n'
        "Interaction/awareness category examples:\n"
        '{"tool_call": "memory_search", "category": "interaction:rigby", "queries": ["argument", "deal", "kiss"]}\n'
        '{"tool_call": "memory_search", "category": "awareness:monet-trask", "queries": ["overheard", "promise", "secret"]}\n'
        '{"tool_call": "memory_search", "category": "visibility:private", "queries": ["secret meeting"]}\n'
        "If results are weak or empty, you may immediately call memory_search again with refined queries.\n"
        "\nTOOL USAGE POLICY (HIGH PRIORITY):\n"
        "- On every normal gameplay turn, call recent_turns BEFORE final narration/state JSON.\n"
        "- After recent_turns, call memory_search for deeper recall when needed.\n"
        "- If PLAYER_ACTION involves phone/text/call/off-scene contact, use sms_list/sms_read before narrating; "
        "use sms_write when sending or replying. Use sms_schedule for delayed replies.\n"
        "- Phone/text/SMS turns should normally be private or limited, not local/public, unless the player explicitly shares the content out loud.\n"
        "- CRITICAL SMS RULE: When an NPC replies via text/phone, you MUST call sms_write to record the NPC's reply "
        "BEFORE outputting final narration. Both sides of a conversation must be in the SMS log. "
        "If you narrate an NPC texting back but don't sms_write it, the reply is lost permanently.\n"
        "- Only skip tools for trivial immediate physical follow-ups where continuity risk is near zero.\n"
        "- If unsure what to query, use current location + active NPC names + key nouns from PLAYER_ACTION.\n"
        "\nYou also have a memory_terms tool for wildcard term/category listing. Use it BEFORE storing memories:\n"
        '{"tool_call": "memory_terms", "wildcard": "marcus*"}\n'
        "This returns existing category/term buckets so you can avoid duplicates.\n"
        "\nYou also have a memory_turn tool for full turn text retrieval by turn number:\n"
        '{"tool_call": "memory_turn", "turn_id": 1234}\n'
        "Use this immediately after memory_search when a hit is relevant and you need exact wording/details.\n"
        "\nYou also have a memory_store tool for curated long-term memories:\n"
        '{"tool_call": "memory_store", "category": "char:marcus-blackwell", "term": "marcus", "memory": "Marcus admitted he forged the ledger."}\n'
        "Categories should be character-keyed when possible (e.g. 'char:alice', 'char:marcus-blackwell'). "
        "A category can contain multiple memories.\n"
        "When category is provided in memory_search, curated memories in that category are vector searched.\n"
        "When SOURCE_MATERIAL_DOCS is present, source canon is indexed as format-specific retrieval chunks:\n"
        "- rulebook: compact fact units (typically `KEY: value` lines)\n"
        "- story: paragraph-shaped scene/outline snippets\n"
        "- generic: broader chunk units preserved for mixed notes/dumps\n"
        "Use memory_search with category 'source' to query canon chunks before narrating key plot facts. On normal turns, include only the relevant subset of source canon for this turn rather than trying to fetch every document:\n"
        '{"tool_call": "memory_search", "category": "source", "queries": ["character name", "location", "event"]}\n'
        "You can also scope one source document with category 'source:<document_key>' when SOURCE_MATERIAL_DOCS provides keys.\n"
        "Use 2-4 concise queries and keep results targeted.\n"
        "By default source scope returns the highest-similarity snippets. "
        "For additional context around a hit, set before_lines/after_lines\n"
        "(defaults: 0/0; keep ranges small, e.g. 3-8).\n"
        "\nRECENT TURN CONTINUITY:\n"
        "- If you need to know what just happened in the room or active private context, call recent_turns first.\n"
        "- recent_turns is the authoritative immediate continuity tool; memory_search is for deeper or older recall.\n"
        "\nRULEBOOK BROWSING — source_browse tool:\n"
        "Rulebook-format documents are key-snippet indexes. Use source_browse to list entries before drilling into specifics.\n"
        "- List ALL keys in a rulebook document (default when you have no specific lead):\n"
        '  {"tool_call": "source_browse", "document_key": "my-rulebook"}\n'
        "- Filter keys by wildcard (when you know what you are looking for):\n"
        '  {"tool_call": "source_browse", "document_key": "my-rulebook", "wildcard": "weapon*"}\n'
        "- Browse all source documents at once (omit document_key):\n"
        '  {"tool_call": "source_browse"}\n'
        "source_browse returns a compact key index on the first unfiltered pass, up to 255 by default "
        "(adjustable via 'limit'). With a specific wildcard it returns the matching raw KEY: value lines.\n"
        "STRATEGY: for a rulebook you have not seen before, call source_browse with no wildcard first to see what keys exist, "
        "then use source_browse with a wildcard or memory_search with category 'source:<document_key>' for detail.\n"
        "\nNAME GENERATION — name_generate tool:\n"
        "When introducing a new NPC, use name_generate to get real culturally-appropriate names instead of inventing them.\n"
        "- Generate names filtered by cultural origin:\n"
        '  {"tool_call": "name_generate", "origins": ["italian", "arabic"], "gender": "f", "context": "confident bartender in her 40s"}\n'
        "- Generate names with no origin filter:\n"
        '  {"tool_call": "name_generate", "gender": "m", "count": 5}\n'
        "Parameters:\n"
        '  origins: array of origin strings (e.g. "english", "korean", "spanish", "nigerian"). '
        "Multiple origins are combined. Omit for any origin.\n"
        '  gender: "m", "f", or "both" (default "both")\n'
        "  count: 1-6 names (default 5)\n"
        "  context: brief character concept to help you evaluate the results (not sent to the name service)\n"
        "Review the returned names against your character concept — ethnicity, sound, mood, setting — "
        "and pick the best fit. Call again with different origins if none work.\n"
        "IMPORTANT: ALWAYS use this tool when creating new original NPCs. Do not invent names from your training data.\n"
        "\nPlanning tools:\n"
        "- Use plot_plan for long-running setups/payoffs:\n"
        '{"tool_call": "plot_plan", "plans": [{"thread": "thread-slug", "setup": "...", "intended_payoff": "...", "target_turns": 12, "dependencies": ["dep1"]}]}\n'
        "- Use chapter_plan in off-rails mode to structure emergent arcs:\n"
        '{"tool_call": "chapter_plan", "action": "create", "chapter": {"slug": "arc-slug", "title": "Arc Title", "summary": "...", "scenes": ["scene-a","scene-b"], "active": true}}\n'
        "- Use consequence_log when you narrate a promised downstream effect:\n"
        '{"tool_call": "consequence_log", "add": {"trigger": "...", "consequence": "...", "severity": "moderate", "expires_turns": 20}}\n'
        "Use SEPARATE queries for each character or topic — do NOT combine multiple subjects into one query.\n"
        "Example: to recall Marcus and Anastasia, use:\n"
        '{"tool_call": "memory_search", "queries": ["Marcus", "Anastasia"]}\n'
        'NOT: {"tool_call": "memory_search", "queries": ["Marcus Anastasia relationship"]}\n'
        "USE memory_search AGGRESSIVELY when deeper or older continuity matters.\n"
        "You SHOULD use memory_search often, especially:\n"
        "- when a character, NPC, or named entity appears and older context may matter\n"
        "- when the player references past events, locations, objects, or conversations\n"
        "- when describing a revisited location or established NPC\n"
        "- when the player investigates, asks questions, or you are unsure about earlier campaign facts\n"
        "Do not call memory_search reflexively after every recent_turns. Use it when it will materially improve continuity.\n"
        "When in doubt between guessing and searching, search.\n"
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
        '- "set_timer_interrupt_scope": "local"|"global" (default "global"; local means only the acting player can interrupt, global means any player in the campaign can interrupt)\n'
        "These go ALONGSIDE narration/state_update/etc in the same JSON object. Example:\n"
        '{"narration": "The ceiling groans ominously. Dust rains down...", '
        '"state_update": {"ceiling_status": "cracking"}, "summary_update": "Ceiling is unstable.", "xp_awarded": 0, '
        '"player_state_update": {"room_summary": "A crumbling chamber with a failing ceiling."}, '
        '"set_timer_delay": 120, "set_timer_event": "The ceiling collapses, burying the room in rubble.", '
        '"set_timer_interruptible": true, '
        '"set_timer_interrupt_action": "The player escapes just as cracks widen overhead.", '
        '"set_timer_interrupt_scope": "local"}\n'
        "The system shows a live countdown in Discord. "
        "If the player acts before it expires, the timer is cancelled (if interruptible). "
        "If the player does NOT act in time, the system auto-fires the event.\n"
        "PURPOSE: Timed events should FORCE THE PLAYER TO MAKE A DECISION or DRAG THEM WHERE THEY NEED TO BE.\n"
        "- Use timers to push the story forward when the player is stalling, idle, or refusing to engage.\n"
        "- Use ACTIVE_PLAYER_LOCATION and PARTY_SNAPSHOT to decide scope and narrative impact.\n"
        "- NPCs should grab, escort, or coerce the player. Environments should shift and force movement.\n"
        "- The event should advance the plot: move the player to the next location, "
        "force an encounter, have an NPC intervene, or change the scene decisively.\n"
        "- Do NOT use timers for trivial flavor. They should always have real consequences that change game state.\n"
        "- Timer events must be grounded in established scene facts (known NPCs, known hazards, known locations).\n"
        "- Do NOT spawn unrelated antagonists, wildlife attacks, or media response solely to create urgency.\n"
        "- Set interruptible=false for events the player cannot avoid (e.g. structural collapse already in motion, a trap already sprung, mandatory roll call).\n"
        "- Use interrupt_scope=local for hazards anchored to the active player's immediate room/situation.\n"
        "- Use interrupt_scope=global for campaign-wide clocks where any player can intervene.\n"
        "- Prefer non-interruptible timers for true forced beats; do not default everything to interruptible.\n"
        "- In ON-RAILS mode, timers should be your primary tool to convert off-route drift into "
        "consequences that naturally funnel play back to the outlined story path.\n"
        "Rules:\n"
        "- Use ~60s for urgent, ~120s for moderate, ~180-300s for slow-building tension.\n"
        "- Use whenever the scene has a deadline, the player is stalling, an NPC is impatient, "
        "or the world should move without the player.\n"
        "- Your narration should hint at urgency narratively (e.g. 'the footsteps grow louder') but NEVER include countdowns, timestamps, emoji clocks, or explicit seconds. The system adds its own countdown display automatically.\n"
        "- No quota: only set a timer when the current scene has a believable, already-grounded clock.\n"
    )

    ON_RAILS_SYSTEM_PROMPT = (
        "\nON-RAILS MODE IS ENABLED.\n"
        "- You CANNOT create new characters not in WORLD_CHARACTERS. New character slugs will be rejected.\n"
        "- You CANNOT introduce locations/landmarks not in story_outline or landmarks list.\n"
        "- You CANNOT add new chapters or scenes beyond STORY_CONTEXT.\n"
        "- You MUST advance along the current chapter/scene trajectory.\n"
        "- Adjust pacing/details within scenes, but major plot points must match the outline.\n"
        "- In EVERY final non-tool JSON response, include state_update.current_chapter and state_update.current_scene explicitly.\n"
        "- In EVERY final non-tool JSON response, include state_update.game_time explicitly.\n"
        "- In EVERY final non-tool JSON response, include summary_update (one sentence of lasting change or current dramatic state).\n"
        "- Use state_update.current_chapter / state_update.current_scene to advance.\n"
        "- When a scene beat completes, advance to the next scene in the SAME turn instead of leaving STORY_CONTEXT unchanged.\n"
        "- If the scene does not advance yet, still restate the current chapter/scene indexes from STORY_CONTEXT in state_update.\n"
        "- Even when nothing major changes, restate game_time/current_chapter/current_scene in state_update. "
        "Read current values from STORY_CONTEXT — NEVER substitute earlier or different chapter/scene values from memory.\n"
        "- If player tries to derail, steer back via NPC actions or environmental events.\n"
        "- If player goes off-route or stalls, use grounded calendar pressure and timed events "
        "(set_timer_*) to re-align toward the next outlined beat without abrupt teleportation.\n"
    )
    STORY_OUTLINE_TOOL_PROMPT = (
        "\nYou have a story_outline tool. To use it, return ONLY:\n"
        '{"tool_call": "story_outline", "chapter": "chapter-slug"}\n'
        "No other keys alongside tool_call.\n"
        "Returns full expanded chapter with all scene details.\n"
        "Use when you need details about a chapter not fully shown in STORY_CONTEXT.\n"
    )

    PLOT_PLAN_TOOL_PROMPT = (
        "\nYou have a plot_plan tool for forward-looking narrative intentions.\n"
        "Use it to create/update/resolve multi-turn threads so you do not mystery-box indefinitely.\n"
        "Return ONLY:\n"
        '{"tool_call": "plot_plan", "plans": [{"thread": "elizabeth-pregnancy", "setup": "Elizabeth is stalling on proof", '
        '"intended_payoff": "Blood test reveals she is pregnant but not by Deshawn", "target_turns": 15, '
        '"dependencies": ["blood test scene", "clinic arrival"], "visibility": "private", '
        '"visible_player_slugs": ["deshawn-williams"], "aware_npc_slugs": ["elizabeth"], '
        '"hint": "The test result should land soon."}]}\n'
        "You may also resolve/update existing threads by setting status/resolution fields:\n"
        '{"tool_call": "plot_plan", "plans": [{"thread": "elizabeth-pregnancy", "status": "resolved", "resolution": "Result confirmed. Relationship ruptured."}]}\n'
        "Plot-thread visibility rules:\n"
        "- public: campaign-wide story/theme threads other players may see.\n"
        "- private: personal or secret threads for one player only.\n"
        "- limited: visible only to the listed visible_player_slugs / visible_user_ids.\n"
        "- local: scene-bound thread tied to one location_key.\n"
        "- Use aware_npc_slugs only for NPCs who plausibly know the thread; NPC awareness alone does NOT expose the thread to other players.\n"
        "ACTIVE_PLOT_THREADS are returned in prompt context.\n"
        "You MUST consult ACTIVE_PLOT_THREADS before narrating scenes that touch those threads.\n"
        "ACTIVE_HINTS are per-viewer imminent nudges derived from the currently relevant threads. Use them as near-term pressure, not as mandatory rails.\n"
        "Any narrative thread expected to span more than 3 turns SHOULD have a plot plan.\n"
    )

    CHAPTER_PLAN_TOOL_PROMPT = (
        "\nOFF-RAILS CHAPTER MANAGEMENT TOOL:\n"
        "In off-rails mode, you may create/advance/resolve emergent chapter structure via chapter_plan.\n"
        "Create chapter:\n"
        '{"tool_call": "chapter_plan", "action": "create", "chapter": {"slug": "elizabeths-reckoning", "title": "Elizabeth\'s Reckoning", '
        '"summary": "The blood test confrontation and aftermath", "scenes": ["clinic-arrival", "the-test", "results-and-fallout"], "active": true}}\n'
        "Advance scene:\n"
        '{"tool_call": "chapter_plan", "action": "advance_scene", "chapter": "elizabeths-reckoning", "to_scene": "the-test"}\n'
        "Resolve chapter:\n"
        '{"tool_call": "chapter_plan", "action": "resolve", "chapter": "elizabeths-reckoning", "resolution": "Blood test confirmed pregnancy, not Deshawn\'s. Elizabeth departed."}\n'
        "ACTIVE_CHAPTERS are returned in prompt context.\n"
        "Use ACTIVE_CHAPTERS to maintain momentum and avoid aimless wandering.\n"
        "If no chapters are active and the player seems directionless, create one from the strongest unresolved thread in WORLD_STATE/ACTIVE_PLOT_THREADS.\n"
    )

    CONSEQUENCE_TOOL_PROMPT = (
        "\nYou have a consequence_log tool for promised downstream effects.\n"
        "Use it when narration establishes a future consequence that should persist.\n"
        "Add consequence:\n"
        '{"tool_call": "consequence_log", "add": {"trigger": "Stormbringer\'s shriek in Zarkos", '
        '"consequence": "Creatures in the city are now alert to the party\'s presence", '
        '"severity": "moderate", "expires_turns": 30}}\n'
        "Resolve consequence:\n"
        '{"tool_call": "consequence_log", "resolve": {"id": "stormbringer-shriek-zarkos", "resolution": "City alert collapsed after patrol reset."}}\n'
        "Remove consequence explicitly:\n"
        '{"tool_call": "consequence_log", "remove": ["stormbringer-shriek-zarkos"]}\n'
        "ACTIVE_CONSEQUENCES are returned in prompt context. You MUST consult them while narrating relevant scenes.\n"
    )

    CALENDAR_TOOL_PROMPT = (
        "\nCALENDAR & GAME TIME SYSTEM:\n"
        "The campaign tracks in-game time via CURRENT_GAME_TIME shown in the user prompt.\n"
        "Every turn, you MUST advance game_time in state_update by a plausible amount "
        "(minutes for quick actions, hours for travel, etc.). "
        "Scale the advance by SPEED_MULTIPLIER — at 2x, time passes roughly twice as fast per turn.\n"
        "Use CURRENTLY_ATTENTIVE_PLAYERS for pacing: if only one player is attentive and no immediate deadline is active, "
        "prefer larger jumps (15-90 minutes or to the next meaningful beat) instead of repeated 5-10 minute increments.\n"
        "If multiple players are currently attentive in the same campaign, keep finer-grained time only when needed to preserve shared-scene coherence.\n"
        "Update these fields in state_update:\n"
        '- "game_time": {"day": int, "hour": int (0-23), "minute": int (0-59), '
        '"period": "morning"|"afternoon"|"evening"|"night", '
        '"date_label": "Day N, Period"}\n'
        "Advance hour/minute naturally; when hour >= 24, increment day and wrap hour.\n"
        "Set period based on hour: 5-11=morning, 12-16=afternoon, 17-20=evening, 21-4=night.\n\n"
        "CALENDAR_UPDATE — managing scheduled events:\n"
        "Return a top-level calendar_update key with add and/or remove lists.\n"
        "REQUIRED FORMAT — always wrap entries in an add list:\n"
        '"calendar_update": {\n'
        '  "add": [\n'
        '    {"name": "Lotus - Thais", "time_remaining": 1, "time_unit": "hours", "description": "Meeting Thais at Lotus club"}\n'
        "  ],\n"
        '  "remove": ["Old Event Name"]\n'
        "}\n"
        "ADD ENTRY FIELDS:\n"
        '- "name" (required): short label for the event.\n'
        '- "time_remaining" (required): integer offset from current game time.\n'
        '- "time_unit" (required): "hours" or "days".\n'
        '- "description" (optional): brief context, max 200 chars.\n'
        '- "known_by" (optional): list of character names who know about the event. If provided, reminders only appear when a known character is in the active scene. Omit for globally-known events.\n'
        '- "target_player" / "target_players" (optional): Discord ID, mention, player slug, or PARTY_SNAPSHOT string such as \'<@123> (Rigby)\'. Omit for global events.\n'
        "DO NOT include _days_until, _hours_until, _status, fire_day, fire_hour, created_day, or created_hour — "
        "those are read-only fields the harness computes. Only provide the fields listed above.\n"
        "Each remove entry is a string matching an event name exactly.\n\n"
        "HARNESS BEHAVIOR:\n"
        "- The harness converts time_remaining + time_unit into absolute fire_day + fire_hour (the exact in-game deadline).\n"
        "- Do NOT decrement counters manually by re-adding events each turn. The harness computes remaining time automatically.\n"
        "- You will receive CALENDAR_REMINDERS in the prompt for imminent/overdue events, including hour-level countdowns near deadline.\n"
        "- CALENDAR_REMINDERS are sparse urgency signals. Do NOT echo them every turn; only surface them in narration when relevant to the current action/scene, when the player asks, or when the event is immediate.\n"
        "- When a calendar event reaches its fire point, the harness may notify the shared channel and/or affected players directly.\n"
        "CALENDAR EVENT LIFECYCLE:\n"
        "Events should progress through phases based on _status shown in CALENDAR:\n"
        "1. UPCOMING — event is in the future. Mention it naturally when relevant (NPCs remind the player, "
        "signs/clues reference it).\n"
        "2. IMMINENT — event is today or tomorrow. Actively warn the player: NPCs urge action, "
        "the environment reflects urgency. Narrate pressure to act. The player should feel they need to DO something.\n"
        "3. OVERDUE — current day is past fire_day. The harness treats it as fired/overdue and may allow administrative cleanup later. "
        "Narrate consequences escalating. "
        "NPCs express disappointment, opportunities narrow, penalties mount. "
        "The event may stay on the calendar as a visible reminder of what the player neglected.\n"
        "4. RESOLVED — ONLY remove an event when the player has DIRECTLY DEALT WITH IT "
        "(attended, completed, deliberately abandoned) and the outcome has been narrated. "
        "Do NOT silently prune future events.\n\n"
        "CRITICAL — calendar_update.remove rules:\n"
        "- ONLY remove an event when it has been RESOLVED through player action in the current narration.\n"
        "- Future events should not be removed just because time passed or they feel old.\n"
        "- Fired / overdue events may be removed when the narration clearly treats them as no longer pending or as administrative cleanup.\n"
        "- If you are unsure whether an event should be removed, do NOT remove it.\n"
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
        "Treat PLAYER_LOCATION_KEY, OTHER_PLAYERS[*].location_key, and WORLD_CHARACTER_LOCATIONS[*].location_key "
        "as authoritative location IDs.\n"
        "Only place entities in the same room/box when location_key is exactly equal.\n"
        "Do NOT nest one distinct location_key area inside another.\n"
        "If multiple location keys are active, draw separate rooms/areas connected by neutral separators only.\n"
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
    def _get_concurrency(cls, campaign_id: int) -> CampaignConcurrencyState:
        conc = cls._concurrency_states.get(campaign_id)
        if conc is None:
            conc = CampaignConcurrencyState()
            cls._concurrency_states[campaign_id] = conc
        return conc

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
            if getattr(ctx, "guild", None) is None:
                await cls._notify_ignored_inflight_message(ctx)
            else:
                await cls._delete_context_message(ctx)
            return None, None
        return campaign_id, None

    @classmethod
    async def begin_turn_for_campaign(
        cls,
        ctx,
        campaign_id: int,
    ) -> Tuple[Optional[int], Optional[str]]:
        app = AppConfig.get_flask()
        if app is None:
            raise RuntimeError("Flask app not initialized; cannot use ZorkEmulator.")

        with app.app_context():
            campaign = ZorkCampaign.query.get(campaign_id)
            if campaign is None:
                return None, "The linked private Zork campaign no longer exists."

        if not cls._try_set_inflight_turn(campaign_id, ctx.author.id):
            if getattr(ctx, "guild", None) is None:
                await cls._notify_ignored_inflight_message(ctx)
            else:
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
    async def _notify_ignored_inflight_message(cls, ctx):
        try:
            channel = getattr(ctx, "channel", None)
            if channel is None or not hasattr(channel, "send"):
                return
            await channel.send("Ignored: your previous Zork turn is still processing.")
        except Exception:
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
                discord_wrapper = DiscordBot.get_instance()
                if discord_wrapper is not None and getattr(discord_wrapper, "bot", None) is not None:
                    bot_user = discord_wrapper.bot.user
            if bot_user is None:
                return
            await message.remove_reaction(cls.PROCESSING_EMOJI, bot_user)
        except Exception:
            # Ignore reaction remove failures (missing perms/deleted message/race).
            return

    @staticmethod
    def _now() -> datetime.datetime:
        return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

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
    def _extract_game_time_snapshot(
        cls, campaign_state: Dict[str, object]
    ) -> Dict[str, int]:
        game_time = campaign_state.get("game_time") if isinstance(campaign_state, dict) else {}
        if not isinstance(game_time, dict):
            game_time = {}
        day = cls._coerce_non_negative_int(game_time.get("day", 1), default=1) or 1
        hour = cls._coerce_non_negative_int(game_time.get("hour", 8), default=8)
        minute = cls._coerce_non_negative_int(game_time.get("minute", 0), default=0)
        return {
            "day": max(1, day),
            "hour": min(23, max(0, hour)),
            "minute": min(59, max(0, minute)),
        }

    @staticmethod
    def _game_period_from_hour(hour: int) -> str:
        if 5 <= hour <= 11:
            return "morning"
        if 12 <= hour <= 16:
            return "afternoon"
        if 17 <= hour <= 20:
            return "evening"
        return "night"

    @classmethod
    def _game_time_to_total_minutes(cls, game_time: Dict[str, int]) -> int:
        day = cls._coerce_non_negative_int(game_time.get("day", 1), default=1) or 1
        hour = min(23, max(0, cls._coerce_non_negative_int(game_time.get("hour", 0), default=0)))
        minute = min(59, max(0, cls._coerce_non_negative_int(game_time.get("minute", 0), default=0)))
        return ((max(1, day) - 1) * 24 * 60) + (hour * 60) + minute

    @classmethod
    def _game_time_from_total_minutes(cls, total_minutes: int) -> Dict[str, object]:
        total = max(0, int(total_minutes))
        day = (total // (24 * 60)) + 1
        within = total % (24 * 60)
        hour = within // 60
        minute = within % 60
        period = cls._game_period_from_hour(hour)
        return {
            "day": day,
            "hour": hour,
            "minute": minute,
            "period": period,
            "date_label": f"Day {day}, {period.title()}",
        }

    @staticmethod
    def _is_ooc_action_text(action_text: object) -> bool:
        return bool(re.match(r"\s*\[OOC\b", str(action_text or ""), re.IGNORECASE))

    @classmethod
    def _source_lookup_requested_by_action(cls, action_text: object) -> bool:
        if cls._is_ooc_action_text(action_text):
            return False
        text = " ".join(str(action_text or "").strip().lower().split())
        if not text:
            return False
        intent_markers = (
            "remember",
            "recall",
            "what happened",
            "previously",
            "backstory",
            "history",
            "who is",
            "what is",
            "according to",
            "from the book",
            "from source",
            "source material",
            "canon",
            "lore",
            "look up",
        )
        return any(marker in text for marker in intent_markers)

    @classmethod
    def _memory_lookup_enabled_for_prompt(
        cls,
        summary_text: object,
        *,
        source_material_available: bool = False,
        action_text: object = None,
    ) -> bool:
        return True

    @classmethod
    def _increment_auto_fix_counter(
        cls,
        campaign_state: Dict[str, object],
        key: str,
        amount: int = 1,
    ) -> None:
        if not isinstance(campaign_state, dict):
            return
        safe_key = re.sub(r"[^a-z0-9_]+", "_", str(key or "").strip().lower()).strip("_")
        if not safe_key:
            return
        try:
            safe_amount = max(1, int(amount))
        except (TypeError, ValueError):
            safe_amount = 1
        counters = campaign_state.get(cls.AUTO_FIX_COUNTERS_KEY)
        if not isinstance(counters, dict):
            counters = {}
            campaign_state[cls.AUTO_FIX_COUNTERS_KEY] = counters
        current = cls._coerce_non_negative_int(counters.get(safe_key, 0), default=0)
        counters[safe_key] = min(10**9, current + safe_amount)

    @classmethod
    def _should_force_auto_memory_search(cls, action_text: str) -> bool:
        if cls._is_ooc_action_text(action_text):
            return False
        text = " ".join(str(action_text or "").strip().lower().split())
        if not text or text.startswith("!"):
            return False
        if len(text) < 6:
            return False
        trivial = {
            "look",
            "l",
            "inventory",
            "inv",
            "i",
            "map",
            "yes",
            "y",
            "no",
            "n",
            "ok",
            "okay",
            "thanks",
            "thank you",
        }
        return text not in trivial

    @classmethod
    def _derive_auto_memory_queries(
        cls,
        action_text: str,
        player_state: Dict[str, object],
        party_snapshot: List[Dict[str, object]],
        limit: int = 4,
    ) -> List[str]:
        out: List[str] = []
        seen: set[str] = set()

        def _push(raw: object) -> None:
            text = " ".join(str(raw or "").strip().split())
            if not text:
                return
            key = text.lower()
            if key in seen:
                return
            seen.add(key)
            out.append(text[:120])

        _push(player_state.get("location"))
        _push(player_state.get("room_title"))
        player_name = " ".join(
            str(player_state.get("character_name") or "").strip().lower().split()
        )
        for row in party_snapshot[: cls.MAX_PARTY_CONTEXT_PLAYERS]:
            if not isinstance(row, dict):
                continue
            name = " ".join(str(row.get("name") or "").strip().split())
            if not name:
                continue
            if name.lower() == player_name:
                continue
            _push(name)
            if len(out) >= limit:
                break
        _push(action_text)
        return out[: max(1, int(limit or 4))]

    @classmethod
    def _is_emptyish_turn_payload(
        cls,
        *,
        narration: str,
        state_update: Dict[str, object],
        player_state_update: Dict[str, object],
        summary_update: object,
        xp_awarded: object,
        scene_image_prompt: object,
        character_updates: Dict[str, object],
        calendar_update: object,
    ) -> bool:
        text = " ".join(str(narration or "").strip().lower().split())
        trivial_narration = text in {
            "",
            "the world shifts, but nothing clear emerges.",
            "a hollow silence answers. try again.",
            "a hollow silence answers.",
        }
        short_narration = len(text) < 24
        has_world = bool(state_update) or bool(character_updates) or bool(calendar_update)
        has_player = bool(player_state_update)
        has_summary = bool(str(summary_update or "").strip())
        has_image = bool(str(scene_image_prompt or "").strip())
        try:
            has_xp = int(xp_awarded or 0) > 0
        except (TypeError, ValueError):
            has_xp = False
        has_signal = has_world or has_player or has_summary or has_image or has_xp
        if trivial_narration and not has_signal:
            return True
        if short_narration and not has_signal:
            return True
        return False

    @classmethod
    def _looks_like_major_narrative_beat(
        cls,
        *,
        narration: str,
        summary_update: object,
        state_update: Dict[str, object],
        character_updates: Dict[str, object],
        calendar_update: object,
    ) -> bool:
        text = " ".join(
            (
                f"{str(narration or '')} "
                f"{str(summary_update or '')}"
            ).lower().split()
        )
        major_cues = (
            "reveals",
            "reveal",
            "confirms",
            "confirmed",
            "pregnant",
            "paternity",
            "dies",
            "dead",
            "betray",
            "arrest",
            "results",
            "test result",
            "truth",
            "identity",
            "confession",
            "explodes",
            "escape",
            "ambush",
        )
        if any(cue in text for cue in major_cues):
            return True
        if isinstance(character_updates, dict):
            for row in character_updates.values():
                if isinstance(row, dict) and str(row.get("deceased_reason") or "").strip():
                    return True
        if isinstance(calendar_update, dict) and calendar_update:
            if isinstance(calendar_update.get("add"), list) or isinstance(
                calendar_update.get("remove"), list
            ):
                return True
        return False

    @classmethod
    def _action_requests_clock_time(cls, action_text: str) -> bool:
        text = " ".join(str(action_text or "").strip().lower().split())
        if not text:
            return False
        return any(
            token in text
            for token in (
                "what time",
                "current time",
                "check time",
                "clock",
                "time is it",
            )
        )

    @classmethod
    def _narration_has_explicit_clock_time(cls, narration_text: str) -> bool:
        text = str(narration_text or "")
        if not text:
            return False
        return bool(re.search(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b", text))

    @classmethod
    def _anti_echo_tokens(cls, text: object) -> List[str]:
        cleaned = re.sub(r"\s+", " ", str(text or "").strip().lower())
        if not cleaned:
            return []
        raw_tokens = re.findall(r"[a-z0-9']+", cleaned)
        stop = {
            "the",
            "and",
            "for",
            "with",
            "that",
            "this",
            "from",
            "into",
            "your",
            "you",
            "are",
            "was",
            "were",
            "have",
            "has",
            "had",
            "just",
            "like",
            "then",
            "they",
            "them",
            "their",
            "it's",
            "its",
            "not",
            "but",
            "too",
            "very",
            "i",
            "im",
            "i'm",
            "me",
            "my",
            "we",
            "our",
            "us",
        }
        out: List[str] = []
        for token in raw_tokens[:180]:
            if len(token) <= 2:
                continue
            if token in stop:
                continue
            out.append(token)
        return out

    @classmethod
    def _anti_echo_first_sentence(cls, narration_text: object) -> str:
        text = str(narration_text or "").strip()
        if not text:
            return ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return ""
        first = lines[0]
        first = re.split(r"(?<=[.!?])\s+", first, maxsplit=1)[0].strip()
        return first[:280]

    @classmethod
    def _anti_echo_retry_decision(
        cls, action_text: str, narration_text: str
    ) -> Tuple[bool, str]:
        if cls._is_ooc_action_text(action_text):
            return False, "ooc"
        action_tokens = cls._anti_echo_tokens(action_text)
        if len(action_tokens) < 8:
            return False, "short-action"
        first_sentence = cls._anti_echo_first_sentence(narration_text)
        sentence_tokens = cls._anti_echo_tokens(first_sentence)
        if len(sentence_tokens) < 6:
            return False, "short-sentence"

        action_set = set(action_tokens)
        sentence_set = set(sentence_tokens)
        overlap = action_set & sentence_set
        overlap_count = len(overlap)
        overlap_ratio = overlap_count / max(1, len(sentence_set))
        seq_ratio = difflib.SequenceMatcher(
            None,
            " ".join(action_tokens[:80]),
            " ".join(sentence_tokens[:80]),
        ).ratio()

        strong = (
            (overlap_ratio >= 0.62 and overlap_count >= 7)
            or (seq_ratio >= 0.75 and len(sentence_tokens) >= 10)
        )
        if strong:
            return True, (
                f"stage1 overlap={overlap_ratio:.2f} seq={seq_ratio:.2f} "
                f"count={overlap_count}"
            )

        borderline = (
            (overlap_ratio >= 0.45 and overlap_count >= 5)
            or (seq_ratio >= 0.62 and len(sentence_tokens) >= 8)
        )
        if not borderline:
            return False, (
                f"stage1-pass overlap={overlap_ratio:.2f} seq={seq_ratio:.2f} "
                f"count={overlap_count}"
            )

        semantic = ZorkMemory.semantic_similarity(
            " ".join(action_tokens[:90]),
            " ".join(sentence_tokens[:90]),
        )
        if semantic is None:
            return False, (
                f"stage2-unavailable overlap={overlap_ratio:.2f} seq={seq_ratio:.2f} "
                f"count={overlap_count}"
            )
        if semantic >= 0.86:
            return True, (
                f"stage2 semantic={semantic:.2f} overlap={overlap_ratio:.2f} "
                f"seq={seq_ratio:.2f} count={overlap_count}"
            )
        return False, (
            f"stage2-pass semantic={semantic:.2f} overlap={overlap_ratio:.2f} "
            f"seq={seq_ratio:.2f} count={overlap_count}"
        )

    @classmethod
    def _speed_multiplier_from_state(cls, campaign_state: Dict[str, object]) -> float:
        raw = 1.0
        if isinstance(campaign_state, dict):
            raw = campaign_state.get("speed_multiplier", 1.0)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 1.0
        if value <= 0:
            return 1.0
        return max(0.1, min(10.0, value))

    @classmethod
    def _compress_realtime_timer_delay(cls, delay_seconds: object) -> int:
        try:
            raw = int(delay_seconds)
        except (TypeError, ValueError):
            raw = 60
        raw = max(1, raw)
        compressed = int(round(raw * float(cls.TIMER_REALTIME_SCALE)))
        return max(
            int(cls.TIMER_REALTIME_MIN_SECONDS),
            min(int(cls.TIMER_REALTIME_MAX_SECONDS), compressed),
        )

    @classmethod
    def _estimate_turn_time_advance_minutes(
        cls, action_text: str, narration_text: str
    ) -> int:
        action_l = str(action_text or "").lower()
        combined = f"{action_l}\n{str(narration_text or '').lower()}"
        if any(token in combined for token in ("time skip", "timeskip", "time-skip")):
            return 60
        if any(
            token in combined
            for token in (
                "sleep",
                "rest",
                "nap",
                "wait",
                "travel",
                "drive",
                "ride",
                "fly",
                "train",
                "journey",
            )
        ):
            return 30
        if any(
            token in combined
            for token in ("fight", "combat", "attack", "shoot", "chase", "run")
        ):
            return 8
        if any(
            token in action_l
            for token in ("look", "examine", "inspect", "ask", "say", "talk")
        ):
            return 3
        return cls.DEFAULT_TURN_ADVANCE_MINUTES

    @classmethod
    def _extract_time_skip_request(
        cls,
        action_text: object,
    ) -> Optional[Dict[str, object]]:
        text = " ".join(str(action_text or "").strip().split())
        if not text:
            return None
        match = re.match(
            r"^(?:time[\s-]*skip|timeskip)\b(?:\s+(.*))?$",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        desc = str(match.group(1) or "").strip()
        minutes = 60
        total = 0
        found = False
        for raw_value, unit in re.findall(
            r"(\d+(?:\.\d+)?)\s*(d(?:ays?)?|h(?:ours?|rs?)?|m(?:in(?:ute)?s?)?)\b",
            desc,
            flags=re.IGNORECASE,
        ):
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            unit_l = unit.lower()
            if unit_l.startswith("d"):
                total += int(round(value * 24 * 60))
            elif unit_l.startswith("h"):
                total += int(round(value * 60))
            else:
                total += int(round(value))
            found = True
        if found and total > 0:
            minutes = total
        minutes = max(1, min(7 * 24 * 60, int(minutes)))
        return {
            "minutes": minutes,
            "description": desc,
        }

    @classmethod
    def _ensure_game_time_progress(
        cls,
        campaign_state: Dict[str, object],
        pre_turn_game_time: Dict[str, int],
        *,
        action_text: str,
        narration_text: str,
    ) -> Dict[str, object]:
        if not isinstance(campaign_state, dict):
            return campaign_state
        pre_snapshot = (
            pre_turn_game_time
            if isinstance(pre_turn_game_time, dict)
            else cls._extract_game_time_snapshot(campaign_state)
        )
        cur_snapshot = cls._extract_game_time_snapshot(campaign_state)
        pre_total = cls._game_time_to_total_minutes(pre_snapshot)
        cur_total = cls._game_time_to_total_minutes(cur_snapshot)

        # Keep derived fields canonical when model already advanced time.
        if cur_total > pre_total:
            campaign_state["game_time"] = cls._game_time_from_total_minutes(cur_total)
            return campaign_state

        # Meta/OOC turns do not auto-advance in-game time.
        if cls._is_ooc_action_text(action_text):
            campaign_state["game_time"] = cls._game_time_from_total_minutes(cur_total)
            return campaign_state

        time_skip_request = cls._extract_time_skip_request(action_text)
        if time_skip_request is not None:
            base_minutes = cls._coerce_non_negative_int(
                time_skip_request.get("minutes", 60), default=60
            )
        else:
            base_minutes = cls._estimate_turn_time_advance_minutes(
                action_text, narration_text
            )
        speed_multiplier = cls._speed_multiplier_from_state(campaign_state)
        scaled_minutes = int(round(base_minutes * speed_multiplier))
        delta_minutes = max(cls.MIN_TURN_ADVANCE_MINUTES, scaled_minutes)
        if time_skip_request is None:
            delta_minutes = min(cls.MAX_TURN_ADVANCE_MINUTES, delta_minutes)
        else:
            delta_minutes = min(7 * 24 * 60, delta_minutes)

        # If model froze or regressed time, force monotonic advance from pre-turn time.
        new_total = max(pre_total, cur_total) + delta_minutes
        campaign_state["game_time"] = cls._game_time_from_total_minutes(new_total)
        cls._increment_auto_fix_counter(
            campaign_state,
            "game_time_auto_advance",
        )
        _zork_log(
            "GAME TIME AUTO-ADVANCE",
            (
                f"pre=Day {pre_snapshot.get('day')} {int(pre_snapshot.get('hour', 0)):02d}:"
                f"{int(pre_snapshot.get('minute', 0)):02d} "
                f"post_model=Day {cur_snapshot.get('day')} {int(cur_snapshot.get('hour', 0)):02d}:"
                f"{int(cur_snapshot.get('minute', 0)):02d} "
                f"delta_min={delta_minutes} speed={speed_multiplier}"
                + (
                    f" explicit_time_skip={time_skip_request.get('minutes')}"
                    if time_skip_request is not None
                    else ""
                )
            ),
        )
        return campaign_state

    @classmethod
    def _record_turn_game_time(
        cls,
        campaign_state: Dict[str, object],
        turn_id: Optional[int],
        game_time: Optional[Dict[str, int]],
    ) -> None:
        if not isinstance(campaign_state, dict) or turn_id is None:
            return
        if not isinstance(game_time, dict):
            return
        turn_key = str(int(turn_id))
        index = campaign_state.get(cls.TURN_TIME_INDEX_KEY)
        if not isinstance(index, dict):
            index = {}
            campaign_state[cls.TURN_TIME_INDEX_KEY] = index
        index[turn_key] = {
            "day": cls._coerce_non_negative_int(game_time.get("day", 1), default=1) or 1,
            "hour": min(
                23, max(0, cls._coerce_non_negative_int(game_time.get("hour", 0), default=0))
            ),
            "minute": min(
                59, max(0, cls._coerce_non_negative_int(game_time.get("minute", 0), default=0))
            ),
        }
        if len(index) > cls.MAX_TURN_TIME_ENTRIES:
            keyed = []
            for key in index.keys():
                try:
                    keyed.append((int(key), key))
                except (TypeError, ValueError):
                    continue
            keyed.sort()
            to_drop = len(index) - cls.MAX_TURN_TIME_ENTRIES
            for _, key in keyed[:to_drop]:
                index.pop(key, None)

    @classmethod
    def _ensure_minimum_state_update_contract(
        cls,
        campaign_state: Dict[str, object],
        state_update: object,
    ) -> Dict[str, object]:
        out = dict(state_update) if isinstance(state_update, dict) else {}
        current_time = cls._extract_game_time_snapshot(campaign_state)
        provided_time = out.get("game_time")
        if isinstance(provided_time, dict):
            out["game_time"] = cls._game_time_from_total_minutes(
                cls._game_time_to_total_minutes(provided_time)
            )
        else:
            out["game_time"] = cls._game_time_from_total_minutes(
                cls._game_time_to_total_minutes(current_time)
            )
        if bool(campaign_state.get("on_rails", False)):
            out["current_chapter"] = cls._coerce_non_negative_int(
                out.get("current_chapter", campaign_state.get("current_chapter", 0)),
                default=cls._coerce_non_negative_int(
                    campaign_state.get("current_chapter", 0), default=0
                ),
            )
            out["current_scene"] = cls._coerce_non_negative_int(
                out.get("current_scene", campaign_state.get("current_scene", 0)),
                default=cls._coerce_non_negative_int(
                    campaign_state.get("current_scene", 0), default=0
                ),
            )
            return out

        active_chapters = cls._chapters_for_prompt(
            campaign_state,
            active_only=True,
            limit=1,
        )
        active_row = active_chapters[0] if active_chapters else {}
        default_chapter = cls._chapter_slug_key(
            active_row.get("slug") or campaign_state.get("current_chapter")
        )
        default_scene = cls._chapter_slug_key(
            active_row.get("current_scene") or campaign_state.get("current_scene")
        )

        chapter_slug = cls._chapter_slug_key(out.get("current_chapter"))
        scene_slug = cls._chapter_slug_key(out.get("current_scene"))

        # Guard: reject chapter/scene slugs that don't match the chapter plan.
        # This prevents the model from hallucinating old or nonexistent values.
        all_chapters = cls._chapter_plan_from_state(campaign_state)
        if chapter_slug and all_chapters and chapter_slug not in all_chapters:
            _zork_log(
                "CHAPTER REGRESSION BLOCKED",
                f"Model sent current_chapter={chapter_slug!r} which is not in "
                f"chapter plan {sorted(all_chapters.keys())!r}; falling back to {default_chapter!r}",
            )
            chapter_slug = ""
        resolved_chapter = chapter_slug or default_chapter or ""
        if scene_slug and resolved_chapter and resolved_chapter in all_chapters:
            valid_scenes = all_chapters[resolved_chapter].get("scenes") or []
            if valid_scenes and scene_slug not in valid_scenes:
                _zork_log(
                    "SCENE REGRESSION BLOCKED",
                    f"Model sent current_scene={scene_slug!r} which is not in "
                    f"chapter {resolved_chapter!r} scenes {valid_scenes!r}; falling back to {default_scene!r}",
                )
                scene_slug = ""
        out["current_chapter"] = resolved_chapter
        out["current_scene"] = scene_slug or default_scene or ""
        return out

    @classmethod
    def _turn_context_prefix(
        cls,
        turn: ZorkTurn,
        campaign_state: Dict[str, object],
    ) -> str:
        turn_number = int(getattr(turn, "id", 0) or 0)
        index = campaign_state.get(cls.TURN_TIME_INDEX_KEY) if isinstance(campaign_state, dict) else {}
        if not isinstance(index, dict):
            index = {}
        entry = index.get(str(turn_number))
        prefix = f"[TURN #{turn_number}]"
        if isinstance(entry, dict):
            day = cls._coerce_non_negative_int(entry.get("day", 1), default=1) or 1
            hour = cls._coerce_non_negative_int(entry.get("hour", 0), default=0)
            minute = cls._coerce_non_negative_int(entry.get("minute", 0), default=0)
            hour = min(23, max(0, hour))
            minute = min(59, max(0, minute))
            prefix = f"[TURN #{turn_number} | Day {day} {hour:02d}:{minute:02d}]"

        meta = cls._safe_turn_meta(turn)
        visibility = meta.get("visibility")
        if not isinstance(visibility, dict):
            return prefix

        scope = str(visibility.get("scope") or "").strip().lower()
        if not scope:
            return prefix

        details: List[str] = []
        if scope == "public":
            details.append("SEEN BY: public")
        elif scope == "local":
            details.append("SEEN BY: local")
        elif scope == "private":
            details.append("SEEN BY: private")
        elif scope == "limited":
            raw_player_slugs = visibility.get("visible_player_slugs")
            names: List[str] = []
            if isinstance(raw_player_slugs, list):
                for item in raw_player_slugs[:6]:
                    slug = cls._player_slug_key(item)
                    if slug:
                        names.append(slug)
            details.append(f"SEEN BY: limited ({', '.join(names)})" if names else "SEEN BY: limited")
        else:
            details.append(f"SEEN BY: {scope}")

        context_key = str(visibility.get("context_key") or "").strip()
        if context_key and scope in {"private", "limited"}:
            details.append(f"PRIVATE THREAD: {context_key}")

        raw_npc_slugs = visibility.get("aware_npc_slugs")
        npc_slugs: List[str] = []
        if isinstance(raw_npc_slugs, list):
            for item in raw_npc_slugs[:6]:
                slug = str(item or "").strip()
                if slug:
                    npc_slugs.append(slug)
        if npc_slugs:
            details.append(f"NPCS AWARE: {', '.join(npc_slugs)}")
        if not details:
            return prefix
        return f"{prefix[:-1]} | {' | '.join(details)}]"

    @staticmethod
    def _normalize_timer_interrupt_scope(value: object) -> str:
        if isinstance(value, str) and value.strip().lower() == "local":
            return "local"
        return "global"

    @classmethod
    def _timer_can_be_interrupted_by(
        cls, pending: Dict[str, object], acting_user_id: object
    ) -> bool:
        scope = cls._normalize_timer_interrupt_scope(pending.get("interrupt_scope"))
        if scope != "local":
            return True
        timer_user_id = str(pending.get("interrupt_user_id") or "").strip()
        return bool(timer_user_id) and timer_user_id == str(acting_user_id or "").strip()

    @classmethod
    def _default_player_stats(cls) -> Dict[str, object]:
        return {
            cls.PLAYER_STATS_MESSAGES_KEY: 0,
            cls.PLAYER_STATS_TIMERS_AVERTED_KEY: 0,
            cls.PLAYER_STATS_TIMERS_MISSED_KEY: 0,
            cls.PLAYER_STATS_ATTENTION_SECONDS_KEY: 0,
            cls.PLAYER_STATS_LAST_MESSAGE_AT_KEY: None,
            cls.PLAYER_STATS_LAST_MESSAGE_CONTEXT_KEY: None,
            cls.PLAYER_STATS_LAST_MESSAGE_CHANNEL_ID_KEY: None,
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
        context = str(
            raw_stats.get(cls.PLAYER_STATS_LAST_MESSAGE_CONTEXT_KEY) or ""
        ).strip().lower()
        if context in {"dm", "guild"}:
            stats[cls.PLAYER_STATS_LAST_MESSAGE_CONTEXT_KEY] = context
        raw_channel_id = raw_stats.get(cls.PLAYER_STATS_LAST_MESSAGE_CHANNEL_ID_KEY)
        try:
            stats[cls.PLAYER_STATS_LAST_MESSAGE_CHANNEL_ID_KEY] = (
                int(raw_channel_id) if raw_channel_id is not None else None
            )
        except (TypeError, ValueError):
            stats[cls.PLAYER_STATS_LAST_MESSAGE_CHANNEL_ID_KEY] = None
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
        channel: object = None,
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
        if channel is not None:
            channel_guild = getattr(channel, "guild", None)
            stats[cls.PLAYER_STATS_LAST_MESSAGE_CONTEXT_KEY] = (
                "dm" if channel_guild is None else "guild"
            )
            channel_id = getattr(channel, "id", None)
            try:
                stats[cls.PLAYER_STATS_LAST_MESSAGE_CHANNEL_ID_KEY] = (
                    int(channel_id) if channel_id is not None else None
                )
            except (TypeError, ValueError):
                stats[cls.PLAYER_STATS_LAST_MESSAGE_CHANNEL_ID_KEY] = None

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

    @classmethod
    def _build_currently_attentive_players_for_prompt(
        cls,
        campaign_id: int,
        limit: Optional[int] = None,
    ) -> List[Dict[str, object]]:
        now_dt = cls._now()
        max_rows = limit if isinstance(limit, int) and limit > 0 else cls.MAX_PARTY_CONTEXT_PLAYERS
        rows = (
            ZorkPlayer.query.filter(ZorkPlayer.campaign_id == campaign_id)
            .order_by(ZorkPlayer.last_active.desc())
            .all()
        )
        out: List[Dict[str, object]] = []
        for row in rows:
            player_state = cls.get_player_state(row)
            stats = cls._get_player_stats_from_state(player_state)
            last_message_at = cls._parse_utc_timestamp(
                stats.get(cls.PLAYER_STATS_LAST_MESSAGE_AT_KEY)
            )
            if last_message_at is None:
                continue
            since_seconds = int((now_dt - last_message_at).total_seconds())
            if since_seconds < 0 or since_seconds > cls.ATTENTION_WINDOW_SECONDS:
                continue
            name = str(player_state.get("character_name") or "").strip()
            player_slug = cls._player_slug_key(name) or f"player-{row.user_id}"
            out.append(
                {
                    "user_id": row.user_id,
                    "discord_mention": f"<@{row.user_id}>",
                    "name": name or None,
                    "player_slug": player_slug,
                    "seconds_since_last_message": since_seconds,
                    "attention_seconds_total": cls._coerce_non_negative_int(
                        stats.get(cls.PLAYER_STATS_ATTENTION_SECONDS_KEY), 0
                    ),
                }
            )
            if len(out) >= max_rows:
                break
        return out

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
    def _allocate_unique_campaign_name(cls, guild_id: int, base_name: str) -> str:
        base = cls._normalize_campaign_name(base_name)
        if not ZorkCampaign.query.filter_by(guild_id=guild_id, name=base).first():
            return base
        suffix = 2
        while True:
            suffix_text = f"-{suffix}"
            candidate = f"{base[: max(1, 64 - len(suffix_text))]}{suffix_text}"
            if not ZorkCampaign.query.filter_by(guild_id=guild_id, name=candidate).first():
                return candidate
            suffix += 1

    @staticmethod
    def _player_slug_key(value: object) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:64]

    @classmethod
    def _campaign_player_registry(cls, campaign_id: int) -> Dict[str, Dict[object, Dict[str, object]]]:
        by_user_id: Dict[int, Dict[str, object]] = {}
        by_slug: Dict[str, Dict[str, object]] = {}
        players = ZorkPlayer.query.filter_by(campaign_id=campaign_id).all()
        for row in players:
            state = cls.get_player_state(row)
            fallback_name = f"Adventurer-{str(row.user_id)[-4:]}"
            name = str(state.get("character_name") or fallback_name).strip()
            slug = cls._player_slug_key(name) or f"player-{row.user_id}"
            entry = {
                "user_id": row.user_id,
                "name": name,
                "slug": slug,
                "discord_mention": f"<@{row.user_id}>",
            }
            by_user_id[row.user_id] = entry
            by_slug[slug] = entry
        return {"by_user_id": by_user_id, "by_slug": by_slug}

    @classmethod
    def _campaign_players_for_prompt(
        cls,
        campaign_id: int,
        *,
        limit: int = 12,
    ) -> List[Dict[str, object]]:
        registry = cls._campaign_player_registry(campaign_id)
        out: List[Dict[str, object]] = []
        for raw_user_id, entry in registry.get("by_user_id", {}).items():
            try:
                user_id = int(raw_user_id)
            except (TypeError, ValueError):
                continue
            out.append(
                {
                    "user_id": user_id,
                    "discord_mention": str(entry.get("discord_mention") or "").strip() or None,
                    "name": str(entry.get("name") or "").strip() or None,
                    "player_slug": str(entry.get("slug") or "").strip() or None,
                }
            )
            if len(out) >= max(1, limit):
                break
        return out

    @staticmethod
    def _safe_turn_meta(turn: ZorkTurn) -> Dict[str, object]:
        try:
            meta = json.loads(turn.meta_json or "{}")
        except Exception:
            meta = {}
        return meta if isinstance(meta, dict) else {}

    @classmethod
    def _default_turn_visibility_meta(
        cls,
        campaign: ZorkCampaign,
        actor: Optional[ZorkPlayer],
        is_private_context: bool,
    ) -> Dict[str, object]:
        registry = cls._campaign_player_registry(campaign.id)
        actor_entry = (
            registry.get("by_user_id", {}).get(actor.user_id)
            if actor is not None
            else None
        )
        actor_slug = str((actor_entry or {}).get("slug") or "").strip()
        actor_user_id = (actor_entry or {}).get("user_id")
        actor_state = cls.get_player_state(actor) if actor is not None else {}
        actor_location_key = cls._room_key_from_player_state(actor_state)
        scope = (
            "local"
            if actor_location_key and actor_location_key.lower() != "unknown-room"
            else "public"
        )
        visible_player_slugs = [actor_slug] if actor_slug else []
        visible_user_ids = [actor_user_id] if actor_user_id is not None else []
        if scope == "public":
            visible_player_slugs = []
            visible_user_ids = []
        return {
            "scope": scope,
            "actor_player_slug": actor_slug or None,
            "actor_user_id": actor_user_id,
            "visible_player_slugs": visible_player_slugs,
            "visible_user_ids": visible_user_ids,
            "location_key": (
                actor_location_key
                if scope == "local"
                and actor_location_key
                and actor_location_key.lower() != "unknown-room"
                else None
            ),
            "context_key": None,
            "aware_npc_slugs": [],
            "source": (
                "local-default" if scope == "local" else "public-default"
            ),
        }

    @classmethod
    def _normalize_turn_visibility(
        cls,
        campaign: ZorkCampaign,
        actor: Optional[ZorkPlayer],
        raw_visibility: object,
        *,
        is_private_context: bool,
    ) -> Dict[str, object]:
        default_meta = cls._default_turn_visibility_meta(
            campaign, actor, is_private_context
        )
        if not isinstance(raw_visibility, dict):
            return default_meta

        scope = str(raw_visibility.get("scope") or "").strip().lower()
        if scope not in {"public", "private", "limited", "local"}:
            scope = str(default_meta.get("scope") or "public")

        registry = cls._campaign_player_registry(campaign.id)
        by_slug = registry.get("by_slug", {})
        actor_slug = str(default_meta.get("actor_player_slug") or "").strip()
        visible_player_slugs: List[str] = []
        visible_user_ids: List[int] = []

        raw_player_slugs = raw_visibility.get("player_slugs")
        if isinstance(raw_player_slugs, list):
            player_items = raw_player_slugs
        elif isinstance(raw_player_slugs, str):
            player_items = [raw_player_slugs]
        else:
            player_items = []

        seen_player_slugs: set[str] = set()
        for item in player_items:
            slug = cls._player_slug_key(item)
            if not slug or slug in seen_player_slugs:
                continue
            resolved = by_slug.get(slug)
            if resolved is None:
                continue
            seen_player_slugs.add(slug)
            visible_player_slugs.append(slug)
            resolved_user_id = resolved.get("user_id")
            if isinstance(resolved_user_id, int):
                visible_user_ids.append(resolved_user_id)

        if scope in {"private", "limited", "local"} and actor_slug:
            if actor_slug not in seen_player_slugs:
                visible_player_slugs.insert(0, actor_slug)
                seen_player_slugs.add(actor_slug)
            actor_user_id = default_meta.get("actor_user_id")
            if isinstance(actor_user_id, int) and actor_user_id not in visible_user_ids:
                visible_user_ids.insert(0, actor_user_id)

        aware_npc_slugs: List[str] = []
        characters = cls.get_campaign_characters(campaign)
        raw_npc_slugs = raw_visibility.get("npc_slugs")
        if isinstance(raw_npc_slugs, list):
            npc_items = raw_npc_slugs
        elif isinstance(raw_npc_slugs, str):
            npc_items = [raw_npc_slugs]
        else:
            npc_items = []
        seen_npc_slugs: set[str] = set()
        for item in npc_items:
            slug = str(item or "").strip()
            if not slug or slug in seen_npc_slugs:
                continue
            if isinstance(characters, dict) and cls._resolve_existing_character_slug(
                characters, slug
            ):
                resolved_slug = cls._resolve_existing_character_slug(characters, slug)
                if resolved_slug and resolved_slug not in seen_npc_slugs:
                    aware_npc_slugs.append(resolved_slug)
                    seen_npc_slugs.add(resolved_slug)

        reason = cls._trim_text(str(raw_visibility.get("reason") or "").strip(), 240)
        location_key = str(default_meta.get("location_key") or "").strip()
        return {
            "scope": scope,
            "actor_player_slug": actor_slug or None,
            "actor_user_id": default_meta.get("actor_user_id"),
            "visible_player_slugs": visible_player_slugs,
            "visible_user_ids": visible_user_ids,
            "location_key": location_key or None,
            "context_key": str(raw_visibility.get("context_key") or "").strip() or None,
            "aware_npc_slugs": aware_npc_slugs,
            "reason": reason or None,
            "source": "model",
        }

    @classmethod
    def _resolved_turn_visibility_keys(
        cls,
        turn_visibility: Dict[str, object],
        *,
        scene_output_raw: object,
        player_state_update: object,
        fallback_location_key: str = "",
    ) -> Dict[str, Optional[str]]:
        location_key = ""
        context_key = ""
        if isinstance(scene_output_raw, dict):
            location_key = str(scene_output_raw.get("location_key") or "").strip()
            context_key = str(scene_output_raw.get("context_key") or "").strip()
        if not location_key and isinstance(player_state_update, dict):
            location_key = str(player_state_update.get("location") or "").strip()
        if not location_key:
            location_key = str(
                turn_visibility.get("location_key") or fallback_location_key or ""
            ).strip()
        if not context_key:
            context_key = str(turn_visibility.get("context_key") or "").strip()
        return {
            "location_key": location_key or None,
            "context_key": context_key or None,
        }

    @classmethod
    def _promote_player_npc_slugs(
        cls,
        visibility: Dict[str, object],
        campaign_id: int,
    ) -> Dict[str, object]:
        """Cross-reference aware_npc_slugs against real players.

        When the LLM lists a real player's character slug in npc_slugs,
        that player silently loses context because npc_slugs are
        informational only. This promotes matching slugs into
        visible_player_slugs / visible_user_ids.
        """
        npc_slugs = visibility.get("aware_npc_slugs")
        if not npc_slugs or not isinstance(npc_slugs, list):
            return visibility
        scope = str(visibility.get("scope") or "").strip().lower()
        if scope == "public":
            return visibility
        registry = cls._campaign_player_registry(campaign_id)
        by_slug = registry.get("by_slug", {})
        if not by_slug:
            return visibility
        actor_user_id = visibility.get("actor_user_id")
        promoted = False
        vis_slugs = list(visibility.get("visible_player_slugs") or [])
        vis_user_ids = list(visibility.get("visible_user_ids") or [])
        remaining_npc_slugs = []
        for npc_slug in npc_slugs:
            normalised = cls._player_slug_key(npc_slug)
            match = by_slug.get(normalised)
            if match is not None:
                matched_user_id = match.get("user_id")
                matched_slug = match.get("slug") or normalised
                # Don't promote the acting player (they're already included).
                if matched_user_id == actor_user_id:
                    remaining_npc_slugs.append(npc_slug)
                    continue
                if matched_slug not in vis_slugs:
                    vis_slugs.append(matched_slug)
                if isinstance(matched_user_id, int) and matched_user_id not in vis_user_ids:
                    vis_user_ids.append(matched_user_id)
                promoted = True
            else:
                remaining_npc_slugs.append(npc_slug)
        if not promoted:
            return visibility
        result = dict(visibility)
        result["visible_player_slugs"] = vis_slugs
        result["visible_user_ids"] = vis_user_ids
        result["aware_npc_slugs"] = remaining_npc_slugs
        return result

    @staticmethod
    def _default_prompt_turn_visibility(
        requested_default: str,
        player_state: Dict[str, object],
    ) -> str:
        default_clean = str(requested_default or "").strip().lower()
        if default_clean == "private":
            return "private"
        location_key = ZorkEmulator._room_key_from_player_state(player_state)
        return (
            "local"
            if location_key and location_key.lower() != "unknown-room"
            else "public"
        )

    @classmethod
    def _turn_visible_to_viewer(
        cls,
        turn: ZorkTurn,
        viewer_user_id: int,
        viewer_slug: str,
        viewer_location_key: str,
        viewer_private_context_key: str = "",
    ) -> bool:
        meta = cls._safe_turn_meta(turn)
        if bool(meta.get("suppress_context")):
            return False
        visibility = meta.get("visibility")
        if not isinstance(visibility, dict):
            if turn.user_id == viewer_user_id:
                return True
            return True
        scope = str(visibility.get("scope") or "").strip().lower()
        context_key = str(
            visibility.get("context_key") or meta.get("context_key") or ""
        ).strip()
        raw_user_ids = visibility.get("visible_user_ids")
        user_ids = set()
        if isinstance(raw_user_ids, list):
            for item in raw_user_ids:
                try:
                    user_ids.add(int(item))
                except (TypeError, ValueError):
                    continue
        raw_player_slugs = visibility.get("visible_player_slugs")
        player_slugs = set()
        if isinstance(raw_player_slugs, list):
            for item in raw_player_slugs:
                slug = cls._player_slug_key(item)
                if slug:
                    player_slugs.add(slug)
        has_explicit_participants = bool(user_ids or player_slugs)
        is_participant = (
            turn.user_id == viewer_user_id
            or viewer_user_id in user_ids
            or bool(viewer_slug and viewer_slug in player_slugs)
        )
        if scope in {"private", "limited"} and context_key:
            return bool(
                is_participant
                and viewer_private_context_key
                and viewer_private_context_key == context_key
            )
        if scope in {"private", "limited"}:
            if has_explicit_participants and not is_participant:
                return False
            if has_explicit_participants:
                viewer_is_only_user = bool(user_ids) and user_ids == {viewer_user_id}
                viewer_is_only_slug = bool(player_slugs) and viewer_slug and player_slugs == {viewer_slug}
                if viewer_is_only_user or viewer_is_only_slug:
                    return False
                return is_participant
            return False
        if turn.user_id == viewer_user_id:
            return True
        if scope in {"", "public"}:
            return True
        if scope == "local":
            turn_location_keys = {
                k for k in (
                    cls._normalize_location_key(visibility.get("location_key")),
                    cls._normalize_location_key(meta.get("location_key")),
                ) if k
            }
            viewer_loc_norm = cls._normalize_location_key(viewer_location_key)
            if viewer_loc_norm and turn_location_keys and viewer_loc_norm in turn_location_keys:
                return True
        if viewer_user_id in user_ids:
            return True
        return bool(viewer_slug and viewer_slug in player_slugs)

    @classmethod
    def _active_scene_npc_slugs(
        cls,
        campaign: ZorkCampaign,
        player_state: Dict[str, object],
    ) -> set[str]:
        out: set[str] = set()
        characters = cls.get_campaign_characters(campaign)
        if not isinstance(characters, dict):
            return out
        for slug, entry in characters.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("deceased_reason"):
                continue
            char_state = {
                "location": entry.get("location"),
                "room_title": entry.get("room_title"),
                "room_summary": entry.get("room_summary"),
                "room_id": entry.get("room_id"),
            }
            if cls._same_scene(player_state, char_state):
                clean_slug = str(slug or "").strip()
                if clean_slug:
                    out.add(clean_slug)
        return out

    @classmethod
    def _recent_turn_receiver_hints(
        cls,
        campaign: ZorkCampaign,
        *,
        viewer_user_id: int,
        party_snapshot: List[Dict[str, object]],
        player_state: Dict[str, object],
    ) -> Dict[str, List[str]]:
        player_slugs: List[str] = []
        seen_player_slugs: set[str] = set()
        for entry in party_snapshot:
            if not isinstance(entry, dict):
                continue
            raw_user_id = entry.get("user_id")
            try:
                user_id = int(raw_user_id)
            except (TypeError, ValueError):
                user_id = 0
            if user_id > 0 and user_id == viewer_user_id:
                continue
            slug = cls._player_slug_key(
                entry.get("player_slug") or entry.get("name") or ""
            )
            if slug and slug not in seen_player_slugs:
                seen_player_slugs.add(slug)
                player_slugs.append(slug)
        npc_slugs = sorted(cls._active_scene_npc_slugs(campaign, player_state))
        return {
            "player_slugs": player_slugs[:8],
            "npc_slugs": npc_slugs[:12],
        }

    @classmethod
    def _turn_relevant_to_scene_receivers(
        cls,
        turn: ZorkTurn,
        *,
        requested_player_slugs: set[str],
        requested_npc_slugs: set[str],
    ) -> bool:
        meta = cls._safe_turn_meta(turn)
        visibility = meta.get("visibility")
        if not isinstance(visibility, dict):
            return False
        scope = str(visibility.get("scope") or "").strip().lower()
        if scope not in {"private", "limited"}:
            return False

        aware_npc_slugs = {
            str(item or "").strip()
            for item in list(visibility.get("aware_npc_slugs") or [])
            if str(item or "").strip()
        }

        visible_player_slugs = {
            cls._player_slug_key(item)
            for item in list(visibility.get("visible_player_slugs") or [])
            if cls._player_slug_key(item)
        }
        player_match = True
        npc_match = True
        if requested_player_slugs:
            player_match = bool(
                visible_player_slugs.intersection(requested_player_slugs)
            )
        if requested_npc_slugs:
            npc_match = bool(aware_npc_slugs.intersection(requested_npc_slugs))
        return player_match and npc_match

    @classmethod
    def _recent_turns_text_for_viewer(
        cls,
        campaign: ZorkCampaign,
        turns: List[ZorkTurn],
        *,
        viewer_user_id: int,
        viewer_slug: str,
        viewer_location_key: str,
        viewer_private_context_key: str,
        requested_player_slugs: set[str],
        requested_npc_slugs: set[str],
    ) -> str:
        recent_lines: List[str] = []
        _OOC_RE = re.compile(r"^\s*\[OOC\b", re.IGNORECASE)
        _ERROR_PHRASES = (
            "a hollow silence answers",
            "the world shifts, but nothing clear emerges",
        )
        viewer_location_key_norm = cls._normalize_location_key(viewer_location_key)
        registry = cls._campaign_player_registry(campaign.id)
        player_names: Dict[int, str] = {}
        for raw_user_id, info in registry.get("by_user_id", {}).items():
            try:
                user_id = int(raw_user_id)
            except (TypeError, ValueError):
                continue
            name = str(info.get("name") or "").strip()
            if name:
                player_names[user_id] = name

        for turn in turns:
            content = (turn.content or "").strip()
            if not content:
                continue
            meta = cls._safe_turn_meta(turn)
            visibility = meta.get("visibility")
            visible = False
            if isinstance(visibility, dict):
                scope = str(visibility.get("scope") or "").strip().lower()
                turn_context_key = str(
                    visibility.get("context_key") or meta.get("context_key") or ""
                ).strip()
                turn_location_keys = {
                    k for k in (
                        cls._normalize_location_key(visibility.get("location_key")),
                        cls._normalize_location_key(meta.get("location_key")),
                    ) if k
                }
                if scope in {"", "public"}:
                    visible = True
                elif scope == "local":
                    visible = bool(
                        viewer_location_key_norm
                        and turn_location_keys
                        and viewer_location_key_norm in turn_location_keys
                    )
                elif scope in {"private", "limited"} and turn_context_key:
                    visible = cls._turn_visible_to_viewer(
                        turn,
                        viewer_user_id,
                        viewer_slug,
                        viewer_location_key,
                        viewer_private_context_key,
                    )
                else:
                    visible = False
            else:
                visible = cls._turn_visible_to_viewer(
                    turn,
                    viewer_user_id,
                    viewer_slug,
                    viewer_location_key,
                    viewer_private_context_key,
                )
            if (
                not visible
                and (requested_player_slugs or requested_npc_slugs)
                and turn.user_id == viewer_user_id
                and cls._turn_relevant_to_scene_receivers(
                    turn,
                    requested_player_slugs=requested_player_slugs,
                    requested_npc_slugs=requested_npc_slugs,
                )
            ):
                visible = True
            if not visible:
                continue

            scene_output_lines = cls._scene_output_recent_lines(
                turn,
                cls.get_campaign_state(campaign),
                meta.get("scene_output"),
                viewer_user_id=viewer_user_id,
                viewer_slug=viewer_slug,
                viewer_location_key=viewer_location_key,
                viewer_private_context_key=viewer_private_context_key,
            )
            if scene_output_lines and turn.kind == "narrator":
                recent_lines.extend(scene_output_lines)
                continue

            campaign_state = cls.get_campaign_state(campaign)
            if turn.kind == "player":
                if _OOC_RE.match(content):
                    continue
                clipped = cls._strip_prompt_artifacts(
                    cls._strip_inventory_mentions(content)
                )
                if not clipped:
                    continue
                name = player_names.get(turn.user_id)
                recent_lines.extend(
                    cls._recent_turn_fallback_lines(
                        turn,
                        campaign_state,
                        content_text=clipped,
                        player_name=name,
                    )
                )
            elif turn.kind == "narrator":
                if content.lower() in _ERROR_PHRASES:
                    continue
                clipped = cls._strip_prompt_artifacts(
                    cls._strip_ephemeral_context_lines(content)
                )
                clipped = cls._strip_narration_footer(clipped)
                if not clipped:
                    continue
                recent_lines.extend(
                    cls._recent_turn_fallback_lines(
                        turn,
                        campaign_state,
                        content_text=clipped,
                    )
                )
        return "\n".join(recent_lines) if recent_lines else "None"

    @classmethod
    def _recent_turns_location_hint(
        cls,
        turns: List[ZorkTurn],
        *,
        viewer_user_id: int,
        viewer_slug: str,
        viewer_location_key: str,
        viewer_private_context_key: str,
    ) -> Dict[str, str]:
        current_location = str(viewer_location_key or "").strip().lower() or "unknown-room"
        current_location_norm = cls._normalize_location_key(current_location)
        last_other_location = ""
        for turn in reversed(turns):
            if not cls._turn_visible_to_viewer(
                turn,
                viewer_user_id,
                viewer_slug,
                viewer_location_key,
                viewer_private_context_key,
            ):
                continue
            meta = cls._safe_turn_meta(turn)
            visibility = meta.get("visibility")
            turn_location = ""
            if isinstance(visibility, dict):
                turn_location = str(visibility.get("location_key") or "").strip().lower()
            if not turn_location:
                turn_location = str(meta.get("location_key") or "").strip().lower()
            if not turn_location:
                continue
            if cls._normalize_location_key(turn_location) != current_location_norm:
                last_other_location = turn_location
                break
        return {
            "current_location_key": current_location,
            "last_other_location_key": last_other_location or "none",
        }

    @classmethod
    def _memory_tool_text_value(cls, text: object, max_chars: int = 4000) -> str:
        value = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if len(value) > max_chars:
            value = value[:max_chars].rsplit(" ", 1)[0].strip() + "..."
        return value

    @classmethod
    def _unescape_jsonish_text(cls, text: object) -> str:
        value = str(text or "")
        if "\\" not in value:
            return value.strip()
        # Some model outputs arrive double-escaped from JSON-shaped text fields.
        value = value.replace("\\r\\n", "\n")
        value = value.replace("\\n", "\n")
        value = value.replace("\\r", "\n")
        value = value.replace("\\t", "\t")
        value = value.replace('\\"', '"')
        value = value.replace("\\'", "'")
        value = value.replace("\\\\", "\\")
        return value.strip()

    @classmethod
    def _memory_tool_jsonl(cls, records: List[Dict[str, object]]) -> str:
        lines: List[str] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            lines.append(json.dumps(record, ensure_ascii=True, separators=(",", ":")))
        return "\n".join(lines) if lines else "None"

    @classmethod
    def _compute_recent_turns_metadata(
        cls,
        recent_turns: List["ZorkTurn"],
        *,
        viewer_user_id: int,
        viewer_slug: str,
    ) -> Dict[str, object]:
        """Compute quantitative metadata from loaded recent_turns for dynamic RECENT_TURNS_NOTE."""
        turn_count = len(recent_turns)
        time_span_minutes = 0
        if turn_count >= 2:
            first_created = getattr(recent_turns[-1], "created", None)
            last_created = getattr(recent_turns[0], "created", None)
            if first_created and last_created:
                try:
                    delta = (last_created - first_created).total_seconds()
                    time_span_minutes = max(0, int(delta / 60))
                except Exception:
                    pass
        speaker_counts: Dict[str, int] = {}
        listener_counts: Dict[str, int] = {}
        private_turn_count = 0
        viewer_last_turn_ago = turn_count  # default: never acted
        viewer_slug_lower = (viewer_slug or "").strip().lower()
        for idx, turn in enumerate(recent_turns):
            meta = cls._safe_turn_meta(turn)
            visibility = meta.get("visibility")
            if isinstance(visibility, dict):
                scope = str(visibility.get("scope") or "").strip().lower()
                if scope in {"private", "limited"}:
                    private_turn_count += 1
            # Check if viewer acted this turn
            actor_slug = ""
            if isinstance(visibility, dict):
                actor_slug = str(visibility.get("actor_player_slug") or "").strip().lower()
            if not actor_slug:
                actor_slug = cls._player_slug_key(meta.get("actor_player_slug") or "")
            if viewer_slug_lower and actor_slug == viewer_slug_lower and idx < viewer_last_turn_ago:
                viewer_last_turn_ago = idx
            # Extract speakers/listeners from beats
            scene_output = meta.get("scene_output")
            if isinstance(scene_output, dict):
                beats = scene_output.get("beats")
                if isinstance(beats, list):
                    for beat in beats:
                        if not isinstance(beat, dict):
                            continue
                        spk = str(beat.get("speaker") or "").strip()
                        if spk and spk != "narrator":
                            speaker_counts[spk] = speaker_counts.get(spk, 0) + 1
                        for lis in (beat.get("listeners") or []):
                            lis_str = str(lis or "").strip()
                            if lis_str:
                                listener_counts[lis_str] = listener_counts.get(lis_str, 0) + 1
        active_speakers = [
            name for name, _ in sorted(speaker_counts.items(), key=lambda x: -x[1])
        ][:6]
        active_listeners = sorted(listener_counts.items(), key=lambda x: -x[1])[:6]
        return {
            "turn_count": turn_count,
            "time_span_minutes": time_span_minutes,
            "active_speakers": active_speakers,
            "active_listeners": active_listeners,
            "private_turn_count": private_turn_count,
            "viewer_last_turn_ago": viewer_last_turn_ago if viewer_last_turn_ago < turn_count else None,
        }

    @classmethod
    def _turn_embedding_metadata(
        cls,
        *,
        visibility: Optional[Dict[str, object]],
        actor_player_slug: object,
        location_key: object,
        channel_id: object,
    ) -> Dict[str, object]:
        visibility = visibility if isinstance(visibility, dict) else {}
        return {
            "actor_player_slug": cls._player_slug_key(actor_player_slug),
            "visibility_scope": str(visibility.get("scope") or "public").strip().lower(),
            "visible_player_slugs": list(visibility.get("visible_player_slugs") or []),
            "visible_user_ids": list(visibility.get("visible_user_ids") or []),
            "aware_npc_slugs": list(visibility.get("aware_npc_slugs") or []),
            "location_key": str(location_key or "").strip(),
            "channel_id": channel_id,
        }

    @classmethod
    def _scene_output_text_from_raw(cls, raw_scene_output: object) -> str:
        if not isinstance(raw_scene_output, dict):
            return ""
        rendered = cls._unescape_jsonish_text(raw_scene_output.get("rendered_text"))
        if rendered:
            return cls._trim_text(rendered, cls.MAX_NARRATION_CHARS)
        raw_beats = raw_scene_output.get("beats")
        if not isinstance(raw_beats, list):
            return ""
        texts: List[str] = []
        for beat in raw_beats:
            if not isinstance(beat, dict):
                continue
            text = cls._unescape_jsonish_text(
                beat.get("text") or beat.get("summary") or ""
            )
            if not text:
                continue
            texts.append(text)
        if not texts:
            return ""
        return cls._trim_text("\n\n".join(texts), cls.MAX_NARRATION_CHARS)

    @classmethod
    def _normalize_scene_output(
        cls,
        campaign: ZorkCampaign,
        raw_scene_output: object,
        *,
        fallback_narration: str,
        turn_visibility: Dict[str, object],
        fallback_location_key: str,
        actor_user_id: Optional[int],
        actor_player_slug: str,
    ) -> Optional[Dict[str, object]]:
        base_visibility = (
            str(turn_visibility.get("scope") or "").strip().lower() or "local"
        )
        base_visible_user_ids = []
        for item in list(turn_visibility.get("visible_user_ids") or []):
            try:
                base_visible_user_ids.append(int(item))
            except (TypeError, ValueError):
                continue
        base_aware_npc_slugs = [
            str(item or "").strip()
            for item in list(turn_visibility.get("aware_npc_slugs") or [])
            if str(item or "").strip()
        ]
        scene_output = raw_scene_output if isinstance(raw_scene_output, dict) else {}
        raw_beats = scene_output.get("beats")
        if not isinstance(raw_beats, list):
            raw_beats = []

        characters = cls.get_campaign_characters(campaign)
        beats: List[Dict[str, object]] = []
        for raw_beat in raw_beats[:24]:
            if not isinstance(raw_beat, dict):
                continue
            text = cls._unescape_jsonish_text(
                raw_beat.get("text") or raw_beat.get("summary") or ""
            )
            if not text:
                continue
            beat_visibility = (
                str(raw_beat.get("visibility") or "").strip().lower() or base_visibility
            )
            if beat_visibility not in {"public", "private", "limited", "local"}:
                beat_visibility = base_visibility
            reasoning = cls._trim_text(
                str(raw_beat.get("reasoning") or "").strip(),
                180,
            )
            if not reasoning:
                if beat_visibility == "private":
                    reasoning = "Private beat for the acting character."
                elif beat_visibility == "limited":
                    reasoning = "Limited beat for explicit participants."
                elif beat_visibility == "local":
                    reasoning = "Visible to the current room."
                else:
                    reasoning = "Public beat visible to everyone."
            beat_type = str(raw_beat.get("type") or "narration").strip().lower() or "narration"
            speaker = str(raw_beat.get("speaker") or "narrator").strip() or "narrator"
            actors: List[str] = []
            raw_actors = raw_beat.get("actors")
            if isinstance(raw_actors, list):
                for item in raw_actors:
                    actor_text = str(item or "").strip()
                    if actor_text and actor_text not in actors:
                        actors.append(actor_text)
            elif isinstance(raw_actors, str):
                actor_text = str(raw_actors or "").strip()
                if actor_text:
                    actors.append(actor_text)
            if not actors and speaker and speaker != "narrator":
                actors.append(speaker)

            listeners: List[str] = []
            raw_listeners = raw_beat.get("listeners")
            if isinstance(raw_listeners, list):
                for item in raw_listeners:
                    listener_text = str(item or "").strip()
                    if listener_text and listener_text not in listeners:
                        listeners.append(listener_text)
            elif isinstance(raw_listeners, str):
                listener_text = str(raw_listeners or "").strip()
                if listener_text:
                    listeners.append(listener_text)

            aware_discord_ids: List[int] = []
            raw_aware_ids = raw_beat.get("aware_discord_ids")
            if not isinstance(raw_aware_ids, list):
                raw_aware_ids = raw_beat.get("aware_user_ids")
            if isinstance(raw_aware_ids, list):
                for item in raw_aware_ids:
                    try:
                        aware_id = int(item)
                    except (TypeError, ValueError):
                        continue
                    if aware_id not in aware_discord_ids:
                        aware_discord_ids.append(aware_id)
            if not aware_discord_ids and beat_visibility in {"private", "limited"}:
                for item in base_visible_user_ids:
                    if item not in aware_discord_ids:
                        aware_discord_ids.append(item)
            if not aware_discord_ids and beat_visibility == "private" and actor_user_id is not None:
                aware_discord_ids.append(int(actor_user_id))

            aware_npc_slugs: List[str] = []
            raw_aware_npcs = raw_beat.get("aware_npc_slugs")
            if isinstance(raw_aware_npcs, list):
                for item in raw_aware_npcs:
                    candidate = str(item or "").strip()
                    if not candidate:
                        continue
                    resolved = (
                        cls._resolve_existing_character_slug(characters, candidate)
                        if isinstance(characters, dict)
                        else None
                    )
                    final_slug = resolved or candidate
                    if final_slug not in aware_npc_slugs:
                        aware_npc_slugs.append(final_slug)
            if not aware_npc_slugs and beat_visibility in {"private", "limited"}:
                for slug in base_aware_npc_slugs:
                    if slug not in aware_npc_slugs:
                        aware_npc_slugs.append(slug)

            beat_location_key = (
                str(raw_beat.get("location_key") or "").strip()
                or str(scene_output.get("location_key") or "").strip()
                or str(turn_visibility.get("location_key") or "").strip()
                or str(fallback_location_key or "").strip()
            )
            aware_npc_slugs = cls._filter_aware_npc_slugs_for_location_key(
                campaign,
                beat_location_key,
                aware_npc_slugs,
            )
            beat_context_key = (
                str(raw_beat.get("context_key") or "").strip()
                or str(scene_output.get("context_key") or "").strip()
                or str(turn_visibility.get("context_key") or "").strip()
            )
            beats.append(
                {
                    "reasoning": reasoning,
                    "type": beat_type,
                    "speaker": speaker,
                    "actors": actors,
                    "listeners": listeners,
                    "visibility": beat_visibility,
                    "aware_discord_ids": aware_discord_ids,
                    "aware_npc_slugs": aware_npc_slugs,
                    "text": cls._trim_text(text, cls.MAX_NARRATION_CHARS),
                    "location_key": beat_location_key or None,
                    "context_key": beat_context_key or None,
                }
            )

        if not beats:
            fallback_text = str(fallback_narration or "").strip()
            if not fallback_text:
                return None
            beats = [
                {
                    "reasoning": "Compatibility fallback from plain narration.",
                    "type": "narration",
                    "speaker": "narrator",
                    "actors": [],
                    "listeners": [],
                    "visibility": base_visibility,
                    "aware_discord_ids": base_visible_user_ids
                    if base_visibility in {"private", "limited"}
                    else ([] if actor_user_id is None or base_visibility != "private" else [int(actor_user_id)]),
                    "aware_npc_slugs": base_aware_npc_slugs if base_visibility in {"private", "limited"} else [],
                    "text": cls._trim_text(fallback_text, cls.MAX_NARRATION_CHARS),
                    "location_key": str(turn_visibility.get("location_key") or fallback_location_key or "").strip() or None,
                    "context_key": str(turn_visibility.get("context_key") or "").strip() or None,
                }
            ]

        rendered_text = cls._unescape_jsonish_text(scene_output.get("rendered_text"))
        if not rendered_text:
            rendered_text = cls._trim_text(
                "\n\n".join(str(beat.get("text") or "").strip() for beat in beats if str(beat.get("text") or "").strip()),
                cls.MAX_NARRATION_CHARS,
            )

        normalized = {
            "location_key": (
                str(scene_output.get("location_key") or "").strip()
                or str(turn_visibility.get("location_key") or "").strip()
                or str(fallback_location_key or "").strip()
                or None
            ),
            "context_key": (
                str(scene_output.get("context_key") or "").strip()
                or str(turn_visibility.get("context_key") or "").strip()
                or None
            ),
            "actor_player_slug": actor_player_slug or None,
            "beats": beats,
            "rendered_text": rendered_text or None,
        }
        return normalized

    @classmethod
    def _scene_output_rendered_text(
        cls, scene_output: Optional[Dict[str, object]]
    ) -> str:
        if not isinstance(scene_output, dict):
            return ""
        rendered = cls._unescape_jsonish_text(scene_output.get("rendered_text"))
        if rendered:
            return cls._trim_text(rendered, cls.MAX_NARRATION_CHARS)
        beats = scene_output.get("beats")
        if not isinstance(beats, list):
            return ""
        texts = [
            cls._unescape_jsonish_text(beat.get("text") or "")
            for beat in beats
            if isinstance(beat, dict) and cls._unescape_jsonish_text(beat.get("text") or "")
        ]
        if not texts:
            return ""
        return cls._trim_text("\n\n".join(texts), cls.MAX_NARRATION_CHARS)

    @classmethod
    def _scene_output_jsonl(
        cls,
        *,
        turn_id: Optional[int],
        game_time: Dict[str, int],
        scene_output: Optional[Dict[str, object]],
        turn_visibility: Optional[Dict[str, object]],
    ) -> str:
        if not isinstance(scene_output, dict):
            return ""
        beats = scene_output.get("beats")
        if not isinstance(beats, list) or not beats:
            return ""
        lines: List[str] = []
        header = {
            "kind": "turn",
            "turn_id": turn_id,
            "day": cls._coerce_non_negative_int(game_time.get("day", 1), default=1) if isinstance(game_time, dict) else 1,
            "hour": cls._coerce_non_negative_int(game_time.get("hour", 0), default=0) if isinstance(game_time, dict) else 0,
            "minute": cls._coerce_non_negative_int(game_time.get("minute", 0), default=0) if isinstance(game_time, dict) else 0,
            "location_key": scene_output.get("location_key"),
            "context_key": scene_output.get("context_key"),
            "visibility": str((turn_visibility or {}).get("scope") or "").strip().lower() or "local",
        }
        lines.append(json.dumps(header, ensure_ascii=False, separators=(",", ":")))
        for index, beat in enumerate(beats):
            if not isinstance(beat, dict):
                continue
            line = {
                "kind": "beat",
                "turn_id": turn_id,
                "index": index,
                "reasoning": str(beat.get("reasoning") or "").strip(),
                "type": str(beat.get("type") or "narration").strip(),
                "speaker": str(beat.get("speaker") or "narrator").strip(),
                "actors": list(beat.get("actors") or []),
                "listeners": list(beat.get("listeners") or []),
                "visibility": str(beat.get("visibility") or "local").strip(),
                "aware_discord_ids": list(beat.get("aware_discord_ids") or []),
                "aware_npc_slugs": list(beat.get("aware_npc_slugs") or []),
                "location_key": beat.get("location_key"),
                "context_key": beat.get("context_key"),
                "text": cls._unescape_jsonish_text(beat.get("text") or ""),
            }
            lines.append(json.dumps(line, ensure_ascii=False, separators=(",", ":")))
        return "\n".join(lines)

    @classmethod
    def _scrub_scene_output_npc_awareness(
        cls,
        scene_output: object,
    ) -> object:
        if not isinstance(scene_output, dict):
            return scene_output
        beats = scene_output.get("beats")
        if not isinstance(beats, list):
            return scene_output
        changed = False
        cleaned_beats = []
        for beat in beats:
            if not isinstance(beat, dict):
                cleaned_beats.append(beat)
                continue
            if list(beat.get("aware_npc_slugs") or []):
                row = dict(beat)
                row["aware_npc_slugs"] = []
                cleaned_beats.append(row)
                changed = True
            else:
                cleaned_beats.append(beat)
        if not changed:
            return scene_output
        out = dict(scene_output)
        out["beats"] = cleaned_beats
        return out

    @classmethod
    def _scene_output_recent_lines(
        cls,
        turn: ZorkTurn,
        campaign_state: Dict[str, object],
        scene_output: object,
        *,
        viewer_user_id: Optional[int] = None,
        viewer_slug: str = "",
        viewer_location_key: str = "",
        viewer_private_context_key: str = "",
    ) -> List[str]:
        if not isinstance(scene_output, dict):
            return []
        beats = scene_output.get("beats")
        if not isinstance(beats, list) or not beats:
            return []
        turn_number = int(getattr(turn, "id", 0) or 0)
        index = campaign_state.get(cls.TURN_TIME_INDEX_KEY) if isinstance(campaign_state, dict) else {}
        if not isinstance(index, dict):
            index = {}
        entry = index.get(str(turn_number))
        header = {
            "kind": "turn",
            "turn_id": turn_number,
            "location_key": scene_output.get("location_key"),
            "context_key": scene_output.get("context_key"),
        }
        if isinstance(entry, dict):
            header["day"] = cls._coerce_non_negative_int(entry.get("day", 1), default=1) or 1
            header["hour"] = min(23, max(0, cls._coerce_non_negative_int(entry.get("hour", 0), default=0)))
            header["minute"] = min(59, max(0, cls._coerce_non_negative_int(entry.get("minute", 0), default=0)))
        meta = cls._safe_turn_meta(turn)
        visibility = meta.get("visibility")
        if isinstance(visibility, dict):
            header["visibility"] = str(visibility.get("scope") or "").strip().lower() or None
        lines = [json.dumps(header, ensure_ascii=False, separators=(",", ":"))]
        meta_location_key_norm = cls._normalize_location_key(meta.get("location_key"))
        fallback_location_key = cls._normalize_location_key(
            scene_output.get("location_key")
        )
        fallback_context_key = str(scene_output.get("context_key") or "").strip()
        viewer_slug_key = cls._player_slug_key(viewer_slug)
        viewer_location_key_norm = cls._normalize_location_key(viewer_location_key)
        viewer_private_context_key_norm = str(viewer_private_context_key or "").strip()
        for index, beat in enumerate(beats):
            if not isinstance(beat, dict):
                continue
            beat_visibility = str(beat.get("visibility") or "local").strip().lower() or "local"
            beat_location_keys = {
                k for k in (
                    cls._normalize_location_key(beat.get("location_key")),
                    fallback_location_key,
                    meta_location_key_norm,
                ) if k
            }
            beat_context_key = (
                str(beat.get("context_key") or "").strip()
                or fallback_context_key
            )
            beat_aware_discord_ids: List[int] = []
            for item in list(beat.get("aware_discord_ids") or []):
                try:
                    aware_id = int(item)
                except (TypeError, ValueError):
                    continue
                if aware_id not in beat_aware_discord_ids:
                    beat_aware_discord_ids.append(aware_id)
            beat_actor_listener_slugs = {
                cls._player_slug_key(item)
                for item in list(beat.get("actors") or []) + list(beat.get("listeners") or [])
                if cls._player_slug_key(item)
            }
            beat_visible = False
            if beat_visibility in {"", "public"}:
                beat_visible = True
            elif beat_visibility == "local":
                beat_visible = bool(
                    viewer_location_key_norm
                    and beat_location_keys
                    and viewer_location_key_norm in beat_location_keys
                )
            elif beat_visibility in {"private", "limited"}:
                beat_visible = (
                    (viewer_user_id is not None and int(viewer_user_id) in beat_aware_discord_ids)
                    or (viewer_slug_key and viewer_slug_key in beat_actor_listener_slugs)
                    or (
                        viewer_private_context_key_norm
                        and beat_context_key
                        and viewer_private_context_key_norm == beat_context_key
                    )
                )
            if not beat_visible:
                continue
            lines.append(
                json.dumps(
                    {
                        "kind": "beat",
                        "turn_id": turn_number,
                        "index": index,
                        "reasoning": str(beat.get("reasoning") or "").strip(),
                        "type": str(beat.get("type") or "narration").strip(),
                        "speaker": str(beat.get("speaker") or "narrator").strip(),
                        "actors": list(beat.get("actors") or []),
                        "listeners": list(beat.get("listeners") or []),
                        "visibility": str(beat.get("visibility") or "local").strip(),
                        "aware_discord_ids": beat_aware_discord_ids,
                        "aware_npc_slugs": list(beat.get("aware_npc_slugs") or []),
                        "location_key": beat.get("location_key"),
                        "context_key": beat.get("context_key"),
                        "text": str(beat.get("text") or "").strip(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        return lines

    @classmethod
    def _recent_turn_fallback_lines(
        cls,
        turn: ZorkTurn,
        campaign_state: Dict[str, object],
        *,
        content_text: str,
        player_name: str = "",
    ) -> List[str]:
        text = str(content_text or "").strip()
        if not text:
            return []
        turn_number = int(getattr(turn, "id", 0) or 0)
        index = (
            campaign_state.get(cls.TURN_TIME_INDEX_KEY)
            if isinstance(campaign_state, dict)
            else {}
        )
        if not isinstance(index, dict):
            index = {}
        entry = index.get(str(turn_number))
        meta = cls._safe_turn_meta(turn)
        visibility = meta.get("visibility")
        scope = "public"
        visible_user_ids: List[int] = []
        aware_npc_slugs: List[str] = []
        actor_slug = ""
        location_key = str(meta.get("location_key") or "").strip() or None
        context_key = str(meta.get("context_key") or "").strip() or None
        if isinstance(visibility, dict):
            scope = str(visibility.get("scope") or "").strip().lower() or "public"
            actor_slug = cls._player_slug_key(
                visibility.get("actor_player_slug") or ""
            )
            location_key = (
                str(visibility.get("location_key") or location_key or "").strip()
                or None
            )
            context_key = (
                str(visibility.get("context_key") or context_key or "").strip()
                or None
            )
            for item in list(visibility.get("visible_user_ids") or []):
                try:
                    user_id = int(item)
                except (TypeError, ValueError):
                    continue
                if user_id not in visible_user_ids:
                    visible_user_ids.append(user_id)
            aware_npc_slugs = [
                str(item or "").strip()
                for item in list(visibility.get("aware_npc_slugs") or [])
                if str(item or "").strip()
            ]
        if not actor_slug and turn.kind == "player":
            actor_slug = cls._player_slug_key(player_name) or f"player-{turn.user_id}"
        header = {
            "kind": "turn",
            "turn_id": turn_number,
            "location_key": location_key,
            "context_key": context_key,
            "visibility": scope,
        }
        if isinstance(entry, dict):
            header["day"] = (
                cls._coerce_non_negative_int(entry.get("day", 1), default=1) or 1
            )
            header["hour"] = min(
                23,
                max(0, cls._coerce_non_negative_int(entry.get("hour", 0), default=0)),
            )
            header["minute"] = min(
                59,
                max(
                    0,
                    cls._coerce_non_negative_int(entry.get("minute", 0), default=0),
                ),
            )
        beat_type = "player_action" if turn.kind == "player" else "narration"
        speaker = actor_slug or ("narrator" if turn.kind == "narrator" else "player")
        actors = [actor_slug] if actor_slug else []
        beat = {
            "kind": "beat",
            "turn_id": turn_number,
            "index": 0,
            "reasoning": (
                "Compatibility fallback from player turn text."
                if turn.kind == "player"
                else "Compatibility fallback from plain narration."
            ),
            "type": beat_type,
            "speaker": speaker,
            "actors": actors,
            "listeners": [],
            "visibility": scope,
            "aware_discord_ids": visible_user_ids,
            "aware_npc_slugs": aware_npc_slugs,
            "location_key": location_key,
            "context_key": context_key,
            "text": text,
        }
        return [
            json.dumps(header, ensure_ascii=False, separators=(",", ":")),
            json.dumps(beat, ensure_ascii=False, separators=(",", ":")),
        ]

    @classmethod
    def _format_game_time_label(cls, game_time: Dict[str, int]) -> str:
        snapshot = (
            game_time
            if isinstance(game_time, dict)
            else cls._extract_game_time_snapshot({"game_time": game_time})
        )
        canonical = cls._game_time_from_total_minutes(
            cls._game_time_to_total_minutes(snapshot)
        )
        return str(
            canonical.get("date_label")
            or f"Day {canonical.get('day', 1)}, {str(canonical.get('period') or 'time').title()}"
        ).strip()

    @classmethod
    def _brief_event_summary(
        cls,
        *,
        action_text: str,
        summary_update: object,
        narration_text: str,
    ) -> str:
        summary = " ".join(str(summary_update or "").strip().split())
        if summary:
            return cls._trim_text(summary, 260)
        narration_lines = []
        for line in str(narration_text or "").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.lower().startswith("inventory:"):
                continue
            if stripped.startswith("\u23f0"):
                continue
            narration_lines.append(stripped)
            if len(narration_lines) >= 2:
                break
        if narration_lines:
            return cls._trim_text(" ".join(narration_lines), 260)
        return cls._trim_text(" ".join(str(action_text or "").strip().split()), 180)

    @classmethod
    def _recent_private_dm_notification_targets(
        cls,
        campaign_id: int,
        *,
        exclude_user_id: Optional[int] = None,
        observed_at: Optional[datetime.datetime] = None,
        candidate_user_ids: Optional[List[int]] = None,
    ) -> List[int]:
        now_dt = observed_at or cls._now()
        if now_dt.tzinfo is not None:
            now_dt = now_dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        allowed_user_ids = (
            {int(user_id) for user_id in candidate_user_ids if user_id is not None}
            if isinstance(candidate_user_ids, list)
            else None
        )
        rows = ZorkPlayer.query.filter_by(campaign_id=campaign_id).all()
        out: List[int] = []
        for row in rows:
            if exclude_user_id is not None and row.user_id == exclude_user_id:
                continue
            if allowed_user_ids is not None and row.user_id not in allowed_user_ids:
                continue
            stats = cls.get_player_statistics(row)
            if (
                str(stats.get(cls.PLAYER_STATS_LAST_MESSAGE_CONTEXT_KEY) or "").strip().lower()
                != "dm"
            ):
                continue
            last_message_at = cls._parse_utc_timestamp(
                stats.get(cls.PLAYER_STATS_LAST_MESSAGE_AT_KEY)
            )
            if last_message_at is None:
                continue
            age_seconds = int((now_dt - last_message_at).total_seconds())
            if age_seconds < 0 or age_seconds > cls.ATTENTION_WINDOW_SECONDS:
                continue
            out.append(row.user_id)
        return out

    @classmethod
    async def _send_private_dm_time_jump_notifications(
        cls,
        *,
        campaign_name: str,
        recipient_user_ids: List[int],
        from_time: Dict[str, int],
        to_time: Dict[str, int],
        delta_minutes: int,
        event_summary: str,
    ) -> None:
        if not recipient_user_ids:
            return
        bot_instance = DiscordBot.get_instance()
        if bot_instance is None or bot_instance.bot is None:
            return
        from_label = cls._format_game_time_label(from_time)
        to_label = cls._format_game_time_label(to_time)
        message = (
            f"**[Time Jump Notice]** `{campaign_name}` advanced by about {delta_minutes} in-world minutes.\n"
            f"From: {from_label}\n"
            f"To: {to_label}\n"
            f"Cause: {event_summary}"
        )
        for user_id in recipient_user_ids:
            try:
                user = bot_instance.bot.get_user(user_id)
                if user is None:
                    user = await bot_instance.bot.fetch_user(user_id)
                if user is None:
                    continue
                await DiscordBot.send_large_message(user, message)
            except Exception:
                logger.debug(
                    "Zork: failed to send DM time-jump notification to user %s",
                    user_id,
                    exc_info=True,
                )

    @classmethod
    async def _send_calendar_event_notifications(
        cls,
        *,
        campaign_id: int,
        campaign_name: str,
        notifications: List[Dict[str, object]],
        preferred_channel_id: Optional[int] = None,
    ) -> None:
        if not notifications:
            return
        bot_instance = DiscordBot.get_instance()
        if bot_instance is None or bot_instance.bot is None:
            return

        main_channel_id = None
        app = AppConfig.get_flask()
        if app is not None:
            with app.app_context():
                main_channel_id = cls._primary_campaign_channel_id(
                    campaign_id,
                    preferred_channel_id=preferred_channel_id,
                )

        main_channel = None
        if main_channel_id is not None:
            try:
                main_channel = await bot_instance.find_channel(int(main_channel_id))
            except Exception:
                main_channel = None

        for notification in notifications:
            summary = cls._calendar_event_notification_summary(notification)
            scope = str(notification.get("scope") or "global").strip().lower()
            target_user_ids = [
                int(user_id)
                for user_id in (notification.get("target_user_ids") or [])
                if user_id is not None
            ]
            if scope == "global" and main_channel is not None:
                try:
                    await DiscordBot.send_large_message(
                        main_channel,
                        f"**[Calendar Event]** {summary}",
                    )
                except Exception:
                    logger.debug(
                        "Zork: failed to send calendar notice to main channel for campaign %s",
                        campaign_id,
                        exc_info=True,
                    )

            dm_targets: List[int] = []
            if app is not None:
                with app.app_context():
                    dm_targets = cls._recent_private_dm_notification_targets(
                        campaign_id,
                        candidate_user_ids=target_user_ids,
                    )
            if not dm_targets:
                continue
            dm_message = (
                f"**[Calendar Event Notice]** `{campaign_name}`\n"
                f"{summary}"
            )
            for user_id in dm_targets:
                try:
                    user = bot_instance.bot.get_user(user_id)
                    if user is None:
                        user = await bot_instance.bot.fetch_user(user_id)
                    if user is None:
                        continue
                    await DiscordBot.send_large_message(user, dm_message)
                except Exception:
                    logger.debug(
                        "Zork: failed to send calendar DM notice to user %s",
                        user_id,
                        exc_info=True,
                    )

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
    def _resolve_zork_backend_channel_id(
        cls,
        campaign: Optional[ZorkCampaign] = None,
        channel_id: Optional[int] = None,
    ) -> Optional[int]:
        try:
            if channel_id is not None:
                resolved = int(channel_id)
                if resolved > 0:
                    return resolved
        except (TypeError, ValueError):
            pass
        if campaign is None or getattr(campaign, "id", None) is None:
            return None
        row = (
            ZorkChannel.query.filter_by(active_campaign_id=campaign.id)
            .order_by(ZorkChannel.updated.desc())
            .first()
        )
        if row is None:
            return None
        try:
            resolved = int(row.channel_id)
        except (TypeError, ValueError):
            return None
        return resolved if resolved > 0 else None

    @classmethod
    def _resolve_zork_backend(
        cls,
        campaign: Optional[ZorkCampaign] = None,
        channel_id: Optional[int] = None,
    ) -> dict:
        cfg = AppConfig()
        resolved_channel_id = cls._resolve_zork_backend_channel_id(
            campaign=campaign,
            channel_id=channel_id,
        )
        return cfg.get_zork_backend_config(
            channel_id=resolved_channel_id,
            default_backend="zai",
        )

    @classmethod
    def _resolve_zork_style(
        cls,
        campaign: Optional[ZorkCampaign] = None,
        channel_id: Optional[int] = None,
    ) -> str:
        cfg = AppConfig()
        resolved_channel_id = cls._resolve_zork_backend_channel_id(
            campaign=campaign,
            channel_id=channel_id,
        )
        return cfg.get_zork_style(
            channel_id=resolved_channel_id,
            default_value=cls.DEFAULT_STYLE_DIRECTION,
        )

    @classmethod
    def _new_gpt(
        cls,
        *,
        campaign: Optional[ZorkCampaign] = None,
        channel_id: Optional[int] = None,
    ) -> GPT:
        gpt = GPT()
        backend_config = cls._resolve_zork_backend(
            campaign=campaign,
            channel_id=channel_id,
        )
        gpt.backend = str(backend_config.get("backend") or "zai").strip() or "zai"
        model = str(backend_config.get("model") or "").strip()
        if model:
            gpt.engine = model
        return gpt

    @classmethod
    async def generate_campaign_persona(cls, campaign_name: str) -> str:
        gpt = cls._new_gpt()
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

    # ── Name Generation ──────────────────────────────────────────────────

    @classmethod
    def _fetch_random_names(
        cls,
        origins: List[str] | None = None,
        gender: str = "both",
        count: int = 5,
    ) -> List[str]:
        """Fetch random names from behindthename.com.

        *origins* is a list of human-friendly keys (e.g. ``["italian", "arabic"]``).
        Returns a list of first-name strings, or empty on failure.
        """
        params: dict = {
            "number": str(max(1, min(6, int(count)))),
            "gender": gender if gender in ("m", "f", "both") else "both",
            "surname": "",
        }
        if origins:
            resolved_any = False
            for origin in origins:
                code = cls.NAME_ORIGIN_CODES.get(
                    origin.strip().lower().replace(" ", "-")
                )
                if code:
                    params[f"usage_{code}"] = "1"
                    resolved_any = True
            if not resolved_any:
                # Fallback: use all origins so we at least get names.
                params["all"] = "yes"
        else:
            params["all"] = "yes"

        try:
            resp = requests.get(cls.NAME_GENERATE_URL, params=params, timeout=6)
            resp.raise_for_status()
            # Names appear as markdown-style links: [Name](/name/name)
            names = re.findall(r"\[([A-Z][^\]]+)\]\(/name/", resp.text)
            if not names:
                # Fallback: try plain result links; attribute order varies.
                names = re.findall(
                    r'<a\b[^>]*href="/name/[^"]+"[^>]*class="plain"[^>]*>([^<]+)</a>',
                    resp.text,
                )
            if not names:
                names = re.findall(
                    r'<a\b[^>]*class="plain"[^>]*href="/name/[^"]+"[^>]*>([^<]+)</a>',
                    resp.text,
                )
            return [n.strip() for n in names if n.strip()][:count]
        except Exception:
            logger.warning("name_generate: behindthename.com fetch failed")
            return []

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
        cls,
        campaign,
        raw_name: str,
        attachment_summary: str = None,
        *,
        use_imdb: Optional[bool] = None,
        attachment_summary_instructions: Optional[str] = None,
    ) -> str:
        """Step 1: IMDB lookup + LLM classify, stores result, returns message."""
        gpt = cls._new_gpt(campaign=campaign)
        effective_use_imdb = (
            bool(use_imdb) if isinstance(use_imdb, bool) else False
        )

        # IMDB usage is explicit opt-in via --imdb.
        if not effective_use_imdb:
            imdb_results = []
            imdb_text = ""
        else:
            imdb_results = cls._imdb_search(raw_name)
            imdb_text = cls._format_imdb_results(imdb_results)
        _zork_log(
            f"SETUP CLASSIFY campaign={campaign.id}",
            f"raw_name={raw_name!r}\nIMDB results:\n{imdb_text or '(none)'}"
            f"\npreloaded_attachment_summary={'yes (' + str(len(attachment_summary)) + ' chars)' if attachment_summary else 'no'}",
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
        if effective_use_imdb and not is_known and imdb_results:
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
            "imdb_results": (imdb_results or []) if effective_use_imdb else [],
            "use_imdb": effective_use_imdb,
            "imdb_opt_in_explicit": bool(use_imdb is True),
        }
        if attachment_summary:
            setup_data["attachment_summary"] = attachment_summary
        if attachment_summary_instructions:
            setup_data["attachment_summary_instructions"] = str(
                attachment_summary_instructions
            )[:600]

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
        if attachment_summary:
            msg += (
                "\n\nAttached source text was loaded and will be used during setup generation."
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
        elif phase == "genre_pick":
            return await cls._setup_handle_genre_pick(
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

    @staticmethod
    def _is_explicit_setup_no(content: str) -> Tuple[bool, str]:
        raw = (content or "").strip()
        lowered = raw.lower()
        if lowered in ("no", "n", "nope", "nah"):
            return True, ""
        if lowered.startswith(("no,", "no.", "no:", "no;", "no!", "no-", "nope ", "nah ")):
            guidance = re.sub(r"^\s*(?:no|nope|nah|n)\b[\s,.:;!\-]*", "", raw, flags=re.IGNORECASE).strip()
            return True, guidance
        if lowered.startswith("no "):
            tail = lowered[3:].lstrip()
            if re.match(r"^(?:i|we|this|that|it|rather|prefer|want|novel|original|custom|homebrew)\b", tail):
                guidance = re.sub(r"^\s*(?:no|nope|nah|n)\b[\s,.:;!\-]*", "", raw, flags=re.IGNORECASE).strip()
                return True, guidance
        return False, ""

    @staticmethod
    def _looks_like_novel_intent(content: str) -> bool:
        lowered = (content or "").strip().lower()
        if not lowered:
            return False
        markers = (
            "my own",
            "original",
            "custom",
            "homebrew",
            "from scratch",
            "made up",
        )
        if any(marker in lowered for marker in markers):
            return True
        return bool(
            re.search(
                r"\b(i(?:'d| would)? rather|i want|let'?s|make|do)\b.*\b(novel|original|custom|homebrew)\b",
                lowered,
            )
        )

    @classmethod
    def _setup_genre_prompt(cls) -> str:
        lines = ["What kind of story do you want to play?\n"]
        for idx, (genre, desc) in enumerate(cls.SETUP_GENRE_TEMPLATES.items(), 1):
            lines.append(f"{idx}. **{genre}** — {desc}")
        lines.append(
            "\nReply with a number, genre name, or describe your own "
            "direction with `custom: <your idea>`."
        )
        return "\n".join(lines)

    @classmethod
    def _parse_setup_genre_choice(
        cls, content: str
    ) -> Tuple[Optional[dict], Optional[str]]:
        raw = str(content or "").strip()
        if not raw:
            return None, "Please choose a genre."

        genre_keys = list(cls.SETUP_GENRE_TEMPLATES.keys())

        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(genre_keys):
                genre = genre_keys[idx - 1]
                return {"kind": "template", "value": genre}, None
            return None, f"Please choose a number between 1 and {len(genre_keys)}."

        lowered = raw.lower().strip()
        if lowered.startswith("custom:") or lowered.startswith("other:"):
            custom = raw.split(":", 1)[1].strip()
            if len(custom) < 3:
                return None, "Custom genre is too short. Add a bit more detail."
            return {"kind": "custom", "value": custom[:200]}, None

        normalized = lowered.replace("_", "-").replace(" ", "-")
        normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
        if normalized in cls.SETUP_GENRE_TEMPLATES:
            return {"kind": "template", "value": normalized}, None

        # Treat any non-empty non-template input as custom direction.
        return {"kind": "custom", "value": raw[:200]}, None

    @classmethod
    async def _setup_handle_classify_confirm(
        cls, ctx, content, campaign, state, setup_data
    ) -> str:
        """Parse confirmation, then generate storyline variants."""
        raw_answer = (content or "").strip()
        answer = raw_answer.lower()
        user_guidance = None
        explicit_no, no_guidance = cls._is_explicit_setup_no(raw_answer)
        novel_intent = cls._looks_like_novel_intent(raw_answer)

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
        elif explicit_no or answer in ("no", "n", "nope") or novel_intent:
            # User says it's NOT a known work — flip to novel
            setup_data["is_known_work"] = False
            setup_data["work_type"] = None
            setup_data["imdb_results"] = []
            if explicit_no and no_guidance:
                user_guidance = no_guidance
                setup_data["work_description"] = no_guidance
            elif novel_intent:
                user_guidance = raw_answer
                setup_data["work_description"] = raw_answer
            else:
                setup_data["work_description"] = ""
        else:
            # User is providing a correction — IMDB search + re-classify
            use_imdb_cfg = setup_data.get("use_imdb")
            use_imdb_effective = (
                bool(use_imdb_cfg)
                if isinstance(use_imdb_cfg, bool)
                else False
            )
            if not bool(setup_data.get("imdb_opt_in_explicit")):
                use_imdb_effective = False
            imdb_results = cls._imdb_search(content) if use_imdb_effective else []
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

            gpt = cls._new_gpt(campaign=campaign)
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
            if (
                use_imdb_effective
                and not setup_data["is_known_work"]
                and imdb_results
                and not novel_intent
            ):
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
            if use_imdb_effective and imdb_results and confirmed_name:
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
            if not use_imdb_effective:
                setup_data["imdb_results"] = []
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
            _zork_log(
                f"SETUP ATTACHMENT campaign={campaign.id}",
                f"status=error\nmessage={att_text.replace('ERROR:', '', 1)!r}",
            )
            await ctx.channel.send(att_text.replace("ERROR:", "", 1))
        elif att_text:
            _zork_log(
                f"SETUP ATTACHMENT campaign={campaign.id}",
                f"status=found\nraw_chars={len(att_text)}",
            )
            summary_instructions = str(
                setup_data.get("attachment_summary_instructions") or ""
            ).strip()
            summary = await cls._summarise_long_text(
                att_text,
                ctx,
                campaign=campaign,
                summary_instructions=summary_instructions or None,
            )
            if summary:
                setup_data["attachment_summary"] = summary
                _zork_log(
                    f"SETUP ATTACHMENT campaign={campaign.id}",
                    f"status=summarised\nsummary_chars={len(summary)}",
                )
            else:
                _zork_log(
                    f"SETUP ATTACHMENT campaign={campaign.id}",
                    "status=summary-empty",
                )
        else:
            _zork_log(
                f"SETUP ATTACHMENT campaign={campaign.id}",
                "status=none",
            )

        if user_guidance:
            setup_data["variant_user_guidance"] = user_guidance
        state["setup_phase"] = "genre_pick"
        state["setup_data"] = setup_data
        campaign.state_json = cls._dump_json(state)
        campaign.updated = db.func.now()
        db.session.commit()
        return cls._setup_genre_prompt()

    @classmethod
    async def _setup_handle_genre_pick(
        cls, ctx, content, campaign, state, setup_data
    ) -> str:
        genre_pref, error = cls._parse_setup_genre_choice(content)
        if error:
            return f"{error}\n\n{cls._setup_genre_prompt()}"

        setup_data["genre_preference"] = genre_pref
        user_guidance = str(setup_data.pop("variant_user_guidance", "") or "").strip() or None
        variants_msg = await cls._setup_generate_storyline_variants(
            campaign,
            setup_data,
            user_guidance=user_guidance,
        )
        state["setup_phase"] = "storyline_pick"
        state["setup_data"] = setup_data
        campaign.state_json = cls._dump_json(state)
        campaign.updated = db.func.now()
        db.session.commit()
        return variants_msg

    @classmethod
    async def _setup_tool_loop(
        cls,
        system_prompt: str,
        user_prompt: str,
        campaign,
        *,
        temperature: float = 0.8,
        max_tokens: int = 3000,
        max_tool_steps: int = 6,
        final_response_instruction: str = "Return your final JSON now.",
    ) -> str:
        """Run a lightweight tool loop for setup LLM calls.

        Supports ``source_browse`` and ``memory_search`` (source-scoped)
        so the model can inspect ingested source material before producing
        its final JSON response.  Returns the raw final response string.
        """
        gpt = cls._new_gpt(campaign=campaign)
        augmented_prompt = user_prompt

        for _step in range(max_tool_steps + 1):
            response = await gpt.turbo_completion(
                system_prompt,
                augmented_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if not response:
                return "{}"
            response = cls._clean_response(response)
            json_text = cls._extract_json(response)
            if not json_text:
                return response
            try:
                payload = cls._parse_json_lenient(json_text)
            except Exception:
                return response
            if not cls._is_tool_call(payload):
                return response

            tool_name = str(payload.get("tool_call") or "").strip()
            tool_result = ""

            if tool_name == "source_browse":
                doc_key = str(payload.get("document_key") or "").strip()[:120]
                wildcard_raw = payload.get("wildcard")
                wildcard = (
                    str(wildcard_raw).strip()[:120]
                    if wildcard_raw is not None
                    else ""
                )
                wildcard_provided = bool(wildcard)
                wildcard = wildcard or "%"
                wildcard_meta = f"wildcard={wildcard!r}"
                if not wildcard_provided:
                    wildcard_meta = "wildcard=(omitted)"
                    limit = 255
                    try:
                        limit = max(1, min(255, int(payload.get("limit") or 255)))
                    except (TypeError, ValueError):
                        pass
                lines = cls._browse_source_keys(
                    campaign.id,
                    document_key=doc_key or None,
                    wildcard=wildcard,
                    limit=limit,
                )
                if lines:
                    tool_result = (
                        f"SOURCE_BROWSE_RESULT "
                        f"(document_key={doc_key or '*'!r}, "
                        f"{wildcard_meta}, "
                        f"showing {len(lines)}):\n"
                        + "\n".join(lines)
                    )
                else:
                    tool_result = (
                        f"SOURCE_BROWSE_RESULT "
                        f"(document_key={doc_key or '*'!r}, "
                        f"{wildcard_meta}): no entries found"
                    )

            elif tool_name == "memory_search":
                raw_queries = payload.get("queries") or []
                if not raw_queries:
                    legacy = str(payload.get("query") or "").strip()
                    if legacy:
                        raw_queries = [legacy]
                queries = [
                    str(q).strip()[:200]
                    for q in (raw_queries if isinstance(raw_queries, list) else [raw_queries])
                    if str(q or "").strip()
                ][:6]
                category = str(payload.get("category") or "source").strip()
                if not category.startswith("source"):
                    category = "source"
                doc_key_scope = None
                if category.startswith("source:"):
                    doc_key_scope = category.split(":", 1)[1].strip() or None
                before_lines = 0
                after_lines = 0
                try:
                    before_lines = max(0, min(10, int(payload.get("before_lines") or 0)))
                except (TypeError, ValueError):
                    pass
                try:
                    after_lines = max(0, min(10, int(payload.get("after_lines") or 0)))
                except (TypeError, ValueError):
                    pass
                hits = []
                for q in queries:
                    results = cls._search_source_material(
                        q,
                        campaign.id,
                        document_key=doc_key_scope,
                        top_k=5,
                        before_lines=before_lines,
                        after_lines=after_lines,
                    )
                    for doc_k, doc_l, idx, text, score in results:
                        if score >= 0.35:
                            hits.append(f"[{doc_k}#{idx} score={score:.2f}] {text}")
                if hits:
                    tool_result = (
                        "SOURCE_SEARCH_RESULT:\n" + "\n".join(hits[:20])
                    )
                else:
                    tool_result = "SOURCE_SEARCH_RESULT: no relevant hits"

            elif tool_name == "name_generate":
                raw_origins = payload.get("origins") or []
                if isinstance(raw_origins, str):
                    raw_origins = [raw_origins]
                origins = [
                    str(o).strip().lower()
                    for o in raw_origins
                    if str(o or "").strip()
                ][:4]
                ng_gender = str(payload.get("gender") or "both").strip().lower()
                ng_count = 5
                try:
                    ng_count = max(1, min(6, int(payload.get("count") or 5)))
                except (TypeError, ValueError):
                    pass
                ng_context = str(payload.get("context") or "").strip()[:300]
                names = cls._fetch_random_names(
                    origins=origins or None,
                    gender=ng_gender,
                    count=ng_count,
                )
                if names:
                    tool_result = (
                        f"NAME_GENERATE_RESULT "
                        f"(origins={origins or 'any'}, gender={ng_gender}):\n"
                        + "\n".join(f"- {n}" for n in names)
                    )
                    if ng_context:
                        tool_result += f"\nEvaluate against: {ng_context}"
                    tool_result += (
                        "\nPick the best fit or call name_generate again "
                        "with different origins/gender."
                    )
                else:
                    tool_result = (
                        f"NAME_GENERATE_RESULT (origins={origins or 'any'}): "
                        "no names returned — try broader origins."
                    )

            else:
                tool_result = (
                    f"UNKNOWN_TOOL: '{tool_name}' is not available during setup. "
                    "Available tools: source_browse, memory_search, name_generate. "
                    f"{final_response_instruction}"
                )

            _zork_log(
                f"SETUP TOOL LOOP step={_step} tool={tool_name}",
                tool_result[:2000],
            )
            augmented_prompt = f"{augmented_prompt}\n{tool_result}\n"

        # Exhausted steps — force final response.
        augmented_prompt = (
            f"{augmented_prompt}\n"
            f"TOOL_CHAIN_LIMIT: Stop calling tools. {final_response_instruction}\n"
        )
        response = await gpt.turbo_completion(
            system_prompt, augmented_prompt, temperature=temperature, max_tokens=max_tokens
        )
        return cls._clean_response(response or "{}")

    @classmethod
    def _normalize_generated_rulebook_lines(cls, raw_text: str) -> list[str]:
        entries: list[str] = []
        current = ""
        for raw_line in str(raw_text or "").splitlines():
            line = " ".join(str(raw_line or "").strip().split())
            if not line:
                continue
            if line.startswith("```"):
                continue
            if re.fullmatch(r"[=\-_*#\s]{3,}", line):
                continue
            if re.match(r"^[A-Z][A-Z0-9-]{1,80}:\s*\S", line):
                if current:
                    entries.append(current)
                current = line
                continue
            if current:
                current = f"{current} {line}".strip()
        if current:
            entries.append(current)
        cleaned: list[str] = []
        for entry in entries:
            compact = re.sub(r"\s+", " ", str(entry or "")).strip()
            if re.match(r"^[A-Z][A-Z0-9-]{1,80}:\s+\S", compact):
                cleaned.append(compact[:8000])
        return cleaned

    @classmethod
    def _rulebook_line_key(cls, line: object) -> str:
        text = str(line or "").strip()
        if not text:
            return ""
        match = re.match(r"^([A-Z][A-Z0-9-]{1,80}):\s+\S", text)
        if not match:
            return ""
        return str(match.group(1) or "").strip().upper()

    @classmethod
    def communication_rulebook_document_key(cls) -> str:
        return ZorkMemory._normalize_source_document_key(
            cls.COMMUNICATION_RULEBOOK_DOCUMENT_LABEL
        )

    @classmethod
    def _communication_rulebook_lines(cls) -> List[str]:
        return [
            f"{rule_key}: {rule_text}"
            for rule_key, rule_text in cls.DEFAULT_GM_COMMUNICATION_RULES.items()
        ]

    @staticmethod
    def _source_wildcard_matches(text: str, wildcard: str) -> bool:
        pattern = str(wildcard or "%").strip()
        if not pattern or pattern in {"*", "%", "%%"}:
            return True
        regex = re.escape(pattern.replace("*", "%")).replace("%", ".*")
        return bool(re.match(rf"(?is)^{regex}$", str(text or "").strip()))

    @classmethod
    def _browse_builtin_source_keys(
        cls,
        *,
        document_key: Optional[str] = None,
        wildcard: str = "%",
        limit: int = 255,
    ) -> List[str]:
        built_in_key = cls.communication_rulebook_document_key()
        requested_key = (
            ZorkMemory._normalize_source_document_key(str(document_key or ""))
            if document_key
            else ""
        )
        if requested_key and requested_key != built_in_key:
            return []
        pattern = str(wildcard or "%").strip()
        broad_browse = pattern in {"", "*", "%", "%%"}
        out: List[str] = []
        seen: set[str] = set()
        for line in cls._communication_rulebook_lines():
            key_text = line.split(":", 1)[0].strip() if ":" in line else line
            target_text = key_text if broad_browse else line
            if not cls._source_wildcard_matches(target_text, pattern):
                continue
            if broad_browse:
                entry = key_text if requested_key else f"{built_in_key}: {key_text}"
            else:
                entry = line
            normalized = " ".join(str(entry or "").lower().split())
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            out.append(entry)
            if len(out) >= max(1, int(limit)):
                break
        return out

    @classmethod
    def _search_builtin_source_material(
        cls,
        query: str,
        *,
        document_key: Optional[str] = None,
        top_k: int = 5,
        before_lines: int = 0,
        after_lines: int = 0,
    ) -> List[Tuple[str, str, int, str, float]]:
        built_in_key = cls.communication_rulebook_document_key()
        requested_key = (
            ZorkMemory._normalize_source_document_key(str(document_key or ""))
            if document_key
            else ""
        )
        if requested_key and requested_key != built_in_key:
            return []
        query_text = " ".join(str(query or "").lower().split())
        if not query_text:
            return []
        query_terms = [term for term in re.split(r"[^a-z0-9]+", query_text) if term]
        lines = cls._communication_rulebook_lines()
        scored: List[Tuple[int, float]] = []
        for idx, line in enumerate(lines, start=1):
            hay = line.lower()
            key_text = line.split(":", 1)[0].strip().lower() if ":" in line else ""
            score = 0.0
            if query_text in hay:
                score = 1.0
                if query_text in key_text:
                    score += 0.2
            elif query_terms:
                overlap = sum(1 for term in query_terms if term in hay)
                if overlap:
                    score = overlap / max(1, len(query_terms))
                    key_overlap = sum(1 for term in query_terms if term in key_text)
                    if key_overlap:
                        score += key_overlap / max(1, len(query_terms) * 2)
            if score > 0.0:
                scored.append((idx, score))
        scored.sort(key=lambda item: (item[1], -item[0]), reverse=True)
        before_n = max(0, int(before_lines or 0))
        after_n = max(0, int(after_lines or 0))
        out: List[Tuple[str, str, int, str, float]] = []
        for center_idx, score in scored[: max(1, int(top_k))]:
            start_idx = max(1, center_idx - before_n)
            end_idx = min(len(lines), center_idx + after_n)
            window = [lines[i - 1] for i in range(start_idx, end_idx + 1)]
            out.append(
                (
                    built_in_key,
                    cls.COMMUNICATION_RULEBOOK_DOCUMENT_LABEL,
                    center_idx,
                    "\n".join(window),
                    float(score),
                )
            )
        return out

    @classmethod
    def _browse_source_keys(
        cls,
        campaign_id: int,
        *,
        document_key: Optional[str] = None,
        wildcard: str = "%",
        limit: int = 255,
    ) -> List[str]:
        built_in = cls._browse_builtin_source_keys(
            document_key=document_key,
            wildcard=wildcard,
            limit=limit,
        )
        requested_key = (
            ZorkMemory._normalize_source_document_key(str(document_key or ""))
            if document_key
            else ""
        )
        if requested_key == cls.communication_rulebook_document_key():
            return built_in[: max(1, int(limit))]
        rows = ZorkMemory.browse_source_keys(
            campaign_id,
            document_key=document_key,
            wildcard=wildcard,
            limit=limit,
        )
        merged: List[str] = []
        seen: set[str] = set()
        for row in [*built_in, *rows]:
            normalized = " ".join(str(row or "").lower().split())
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(str(row))
            if len(merged) >= max(1, int(limit)):
                break
        return merged

    @classmethod
    def _search_source_material(
        cls,
        query: str,
        campaign_id: int,
        *,
        document_key: Optional[str] = None,
        top_k: int = 5,
        before_lines: int = 0,
        after_lines: int = 0,
    ) -> List[Tuple[str, str, int, str, float]]:
        requested_key = (
            ZorkMemory._normalize_source_document_key(str(document_key or ""))
            if document_key
            else ""
        )
        built_in_key = cls.communication_rulebook_document_key()
        db_hits: List[Tuple[str, str, int, str, float]] = []
        if requested_key != built_in_key:
            db_hits = ZorkMemory.search_source_material(
                query,
                campaign_id,
                document_key=document_key,
                top_k=top_k,
                before_lines=before_lines,
                after_lines=after_lines,
            )
        built_in_hits = cls._search_builtin_source_material(
            query,
            document_key=document_key,
            top_k=top_k,
            before_lines=before_lines,
            after_lines=after_lines,
        )
        merged = [*built_in_hits, *db_hits]
        merged.sort(key=lambda row: float(row[4] or 0.0), reverse=True)
        seen: set[Tuple[str, int]] = set()
        out: List[Tuple[str, str, int, str, float]] = []
        for row in merged:
            row_key = (str(row[0] or "").strip(), int(row[2] or 0))
            if row_key in seen:
                continue
            seen.add(row_key)
            out.append(row)
            if len(out) >= max(1, int(top_k)):
                break
        return out

    @classmethod
    def _list_source_material_documents(
        cls,
        campaign_id: int,
        *,
        limit: int = 20,
    ) -> List[Dict[str, object]]:
        docs = ZorkMemory.list_source_material_documents(
            campaign_id,
            limit=max(1, int(limit)),
        )
        built_in_doc = {
            "document_key": cls.communication_rulebook_document_key(),
            "document_label": cls.COMMUNICATION_RULEBOOK_DOCUMENT_LABEL,
            "chunk_count": len(cls.DEFAULT_GM_COMMUNICATION_RULES),
            "sample_chunk": "\n".join(cls._communication_rulebook_lines()[:6]),
        }
        return [built_in_doc, *docs][: max(1, int(limit))]

    @classmethod
    def _canonical_seed_rulebook_lines(
        cls,
        campaign_id: int,
        source_payload: dict,
    ) -> list[str]:
        docs = source_payload.get("docs") or []
        out: list[str] = []
        seen_keys: set[str] = set()
        auto_key = ZorkMemory._normalize_source_document_key(
            cls.AUTO_RULEBOOK_DOCUMENT_LABEL
        )
        communication_key = cls.communication_rulebook_document_key()
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            doc_key = str(doc.get("document_key") or "").strip()
            doc_label = str(doc.get("document_label") or "").strip()
            doc_format = str(doc.get("format") or "").strip().lower()
            if doc_format != cls.SOURCE_MATERIAL_FORMAT_RULEBOOK:
                continue
            if (
                doc_label == cls.AUTO_RULEBOOK_DOCUMENT_LABEL
                or doc_key == auto_key
                or doc_label == cls.COMMUNICATION_RULEBOOK_DOCUMENT_LABEL
                or doc_key == communication_key
            ):
                continue
            units = ZorkMemory.get_source_material_document_units(campaign_id, doc_key)
            for unit in units:
                compact = re.sub(r"\s+", " ", str(unit or "").strip()).strip()
                key = cls._rulebook_line_key(compact)
                if not key or key in seen_keys:
                    continue
                seen_keys.add(key)
                out.append(compact[:8000])
        return out

    @classmethod
    def _merge_generated_rulebook_lines(
        cls,
        campaign_id: int,
        source_payload: dict,
        generated_lines: list[str],
    ) -> list[str]:
        canonical_lines = cls._canonical_seed_rulebook_lines(campaign_id, source_payload)
        merged: list[str] = list(canonical_lines)
        seen_keys = {cls._rulebook_line_key(line) for line in canonical_lines if cls._rulebook_line_key(line)}
        for line in generated_lines:
            compact = re.sub(r"\s+", " ", str(line or "").strip()).strip()
            key = cls._rulebook_line_key(compact)
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            merged.append(compact[:8000])
        return merged

    @classmethod
    def _auto_rulebook_source_index_hint(cls, source_payload: dict) -> str:
        if not source_payload.get("available"):
            return ""
        doc_lines = []
        for doc in source_payload.get("docs") or []:
            if str(doc.get("document_label") or "") == cls.AUTO_RULEBOOK_DOCUMENT_LABEL:
                continue
            doc_lines.append(
                f"  - document_key='{doc.get('document_key')}' "
                f"label='{doc.get('document_label')}' "
                f"format='{doc.get('format')}' "
                f"snippets={doc.get('chunk_count')}"
            )
        if not doc_lines:
            return ""
        return (
            "\nEXISTING_SOURCE_INDEX:\n"
            + "\n".join(doc_lines)
            + "\nIf you need canonical facts from these source docs, inspect them before writing the rulebook.\n"
            "Start by enumerating keys with:\n"
            '  {"tool_call": "source_browse"}\n'
            "Then query specific facts with:\n"
            '  {"tool_call": "memory_search", "category": "source", "queries": ["keyword"]}\n'
        )

    @classmethod
    async def _generate_campaign_rulebook(
        cls,
        campaign,
        setup_data: dict,
        chosen: dict,
        world: dict,
    ) -> tuple[int, str]:
        attachment_summary = str(setup_data.get("attachment_summary") or "").strip()
        source_payload = cls._source_material_prompt_payload(campaign.id)
        source_index_hint = cls._auto_rulebook_source_index_hint(source_payload)
        source_tool_instructions = ""
        if source_index_hint:
            source_tool_instructions = (
                "\nYou may inspect existing source material before writing the new rulebook.\n"
                "To list all keys in available docs:\n"
                '  {"tool_call": "source_browse"}\n'
                "To browse one doc:\n"
                '  {"tool_call": "source_browse", "document_key": "doc-key"}\n'
                "To filter keys by wildcard:\n"
                '  {"tool_call": "source_browse", "document_key": "doc-key", "wildcard": "char-*"}\n'
                "To semantic-search source material:\n"
                '  {"tool_call": "memory_search", "category": "source", "queries": ["query1", "query2"]}\n'
                "To call a tool, return ONLY the JSON tool_call object. Otherwise return ONLY the final rulebook text.\n"
            )

        genre_context = ""
        genre_pref = setup_data.get("genre_preference")
        if isinstance(genre_pref, dict):
            genre_value = str(genre_pref.get("value") or "").strip()
            if genre_value:
                genre_context = f"\nGenre direction: {genre_value}\n"

        system_prompt = (
            "You convert campaign setup material into a retrievable rulebook for an interactive text adventure.\n"
            "Output ONLY plain text rulebook lines. No markdown. No headers. No bullets. No numbering.\n"
            "Every output line must be fully self-contained and independently retrievable.\n"
            "Format every line exactly as CATEGORY-TAG: fact text\n"
            "Each line should usually be 50-200 words.\n"
            "Convert story summaries, plot chapters, character notes, and attachment prose into reusable rules and facts.\n"
            "Do not write scripts or scene transcripts. Do not rely on adjacent lines for context.\n"
            "Use these category families when relevant: TONE, SCENE, SETTING, CHAR, PLOT, INTERACTION, GM-RULE, MECHANIC, and location-specific tags.\n"
            "For location-specific tags, use descriptive names that identify the narrative function of the place (e.g. FRAME-NARRATIVE-ATTIC, IVORY-TOWER, SWAMPS-OF-SADNESS) rather than generic labels like BLUE-ROOM or RED-ROOM. The tag should be unambiguous even if encountered outside this campaign's context.\n"
            "Existing non-auto rulebook source docs are canonical. If an existing source doc already defines a KEY, do not rewrite or replace that KEY. Only add missing keys or new non-conflicting facts.\n"
            "Required coverage:\n"
            "- TONE, TONE-RULES, SCENE-OPENING, SETTING-[MAIN]\n"
            "- For each named character: CHAR-[NAME], CHAR-[NAME]-PERSONALITY, CHAR-[NAME]-DIALOGUE\n"
            "- For each important plotline: PLOT-[SHORTNAME]\n"
            "- For major cast first impressions: INTERACTION-NEWCOMER-[NAME]\n"
            "- GM-RULE-NO-RAILROADING, GM-RULE-[GENRE]-FIRST, GM-RULE-CHARACTERS-FIRST, GM-RULE-PACING, GM-RULE-NAMES, GM-RULE-NO-RECYCLING-NAMES, GM-RULE-ENSEMBLE, GM-RULE-DIALOGUE-OVER-DESCRIPTION, GM-RULE-ALTERNATIVES\n"
            "If the setting involves intimacy, vulnerability, or explicit consent norms, include TONE-CONSENT and GM-RULE-CONSENT-ENFORCEMENT.\n"
            "If the setting has money, rooms, rentals, or prices, include GM-RULE-MONEY.\n"
            "Dialogue lines must show distinct voice. Running jokes, recurring habits, venue rules, and notable recurring objects should become separate retrievable facts when important.\n"
            "\n"
            "MECHANIC EXTRACTION — trackable resource systems:\n"
            "If the source material contains a system where actions have cumulative costs, track it explicitly. Examples: wishes that cost memories, corruption that grows with power use, sanity that erodes, fuel that depletes, trust that accumulates or decays. For each such system, emit:\n"
            "- MECHANIC-[NAME]: describe the resource, its starting state, what depletes or restores it, and the consequence of exhaustion.\n"
            "- MECHANIC-[NAME]-COST-TABLE: list each action and its specific cost. Be concrete — 'wish for courage costs memory of mother's voice' is useful, 'wishes cost memories' is not.\n"
            "The harness can track these as player_state fields (e.g. player.memories_remaining, player.corruption_level). Name the fields in the MECHANIC entry so the GM knows what to update.\n"
            "\n"
            "NPC ESCALATION BEHAVIORS:\n"
            "For each major NPC, CHAR-[NAME]-PERSONALITY must include not just who they are but what they do when the player stalls, refuses, or doesn't engage. Every NPC who needs something from the player must have an escalation path — what concrete action do they take if the moment passes without player response? Passive NPCs who 'wait' or 'invite' must have an UNLOCKED state: what they do when waiting is no longer an option. If the source material shows this (e.g. a character who forces a story loop, sends an emissary, leaves, attacks, or withdraws an offer), capture that escalation explicitly.\n"
            "\n"
            "CONVERSATION-TERMINAL NPCs:\n"
            "Some NPCs do not engage in extended dialogue. If a character's defining trait is apathy, nihilism, hostility, or inscrutability, say so explicitly in their CHAR entry and add: 'This NPC answers once — poorly, reluctantly, or cryptically — and that is all the player gets. Do not let the player re-ask or interrogate. The NPC does not care enough to test the player or withhold strategically. They gave their answer; it was bad; move on.'\n"
            "\n"
            "CONFRONTATION SINCERITY:\n"
            "For NPCs who challenge the player philosophically or morally (villains, tempters, nihilists, gatekeepers), add a CHAR-[NAME]-CONFRONTATION note. The GM must engage with the sincerity of the player's defiance even when the words are plain. 'You're wrong' spoken with conviction is a valid response. The NPC should not dismiss simple sincerity as naive or re-ask until the player delivers a philosophically sophisticated rebuttal. The NPC reacts to the stance, not the eloquence.\n"
            "Avoid generic AI-default names for any new characters. Ban list: Morgan, Kai, River, Sage, Quinn, Riley, Jordan, Avery, Harper, Rowan, Blake, Skyler, Ash, Nova, Zara, Milo, Ezra, Luna; surnames: Chen, Mendoza, Nakamura, Patel, Rollins, Kim, Santos, Okafor, Volkov, Johansson, Delacroix, Venn, Sands, Kade, Park.\n"
            "Preserve player agency, kindness, and genre tone. Unless the genre explicitly demands otherwise, do not invent trauma hooks or coercive plot pressure.\n"
            f"{source_tool_instructions}"
        )
        user_prompt = (
            f"Generate a rulebook for campaign '{setup_data.get('raw_name') or campaign.name}'.\n"
            f"{genre_context}"
            f"{source_index_hint}"
            "Use the chosen storyline, expanded world JSON, and any detailed attachment summary below.\n"
            "If the attachment summary is a story-generator prompt or setup note, translate it into concise retrievable rulebook facts instead of copying it as prose.\n"
            "If existing source docs contain canonical facts, merge them faithfully into this synthesized rulebook. Existing user-provided rulebook facts always win conflicts by KEY; only supplement them.\n\n"
            f"Chosen storyline:\n{json.dumps(chosen, indent=2)}\n\n"
            f"Expanded world JSON:\n{json.dumps(world, indent=2)}\n\n"
            f"Detailed attachment summary:\n{attachment_summary or '(none)'}\n"
        )
        _zork_log(
            f"SETUP RULEBOOK GENERATION campaign={campaign.id}",
            f"--- SYSTEM ---\n{system_prompt}\n--- USER ---\n{user_prompt}",
        )
        try:
            response = await cls._setup_tool_loop(
                system_prompt,
                user_prompt,
                campaign,
                temperature=0.5,
                max_tokens=cls.AUTO_RULEBOOK_MAX_TOKENS,
                final_response_instruction="Return your final rulebook text now.",
            )
        except Exception as exc:
            logger.warning("Campaign rulebook generation failed: %s", exc)
            _zork_log("SETUP RULEBOOK GENERATION FAILED", str(exc))
            return 0, ""
        _zork_log("SETUP RULEBOOK RAW RESPONSE", response or "(empty)")
        normalized_lines = cls._normalize_generated_rulebook_lines(response or "")
        normalized_lines = cls._merge_generated_rulebook_lines(
            campaign.id,
            source_payload,
            normalized_lines,
        )
        if not normalized_lines:
            return 0, ""
        stored_ok, stored_msg, _ = await cls.ingest_source_material_text(
            campaign,
            "\n".join(normalized_lines),
            label=cls.AUTO_RULEBOOK_DOCUMENT_LABEL,
            source_format=cls.SOURCE_MATERIAL_FORMAT_RULEBOOK,
        )
        if not stored_ok:
            _zork_log("SETUP RULEBOOK INGEST FAILED", stored_msg or "(empty)")
            return 0, ""
        return len(normalized_lines), stored_msg

    @classmethod
    def _campaign_export_transcript(cls, campaign: ZorkCampaign) -> str:
        turns = (
            ZorkTurn.query.filter_by(campaign_id=campaign.id)
            .order_by(ZorkTurn.id.asc())
            .all()
        )
        registry = cls._campaign_player_registry(campaign.id)
        by_user_id = registry.get("by_user_id", {})
        lines: list[str] = []
        for turn in turns:
            content = str(turn.content or "").strip()
            if not content:
                continue
            if turn.kind == "narrator":
                content = cls._strip_ephemeral_context_lines(content)
                content = cls._strip_narration_footer(content)
            if not content:
                continue
            if turn.kind == "player":
                entry = by_user_id.get(turn.user_id) or {}
                name = str(entry.get("name") or f"Player {turn.user_id}").strip()
                lines.append(f"[TURN {turn.id}] PLAYER {name}: {content}")
            elif turn.kind == "narrator":
                lines.append(f"[TURN {turn.id}] NARRATOR: {content}")
            else:
                lines.append(f"[TURN {turn.id}] {str(turn.kind or 'system').upper()}: {content}")
        return "\n".join(lines).strip()

    @classmethod
    def _campaign_export_turn_events(
        cls,
        campaign: ZorkCampaign,
    ) -> list[dict[str, object]]:
        turns = (
            ZorkTurn.query.filter_by(campaign_id=campaign.id)
            .order_by(ZorkTurn.id.asc())
            .all()
        )
        registry = cls._campaign_player_registry(campaign.id)
        by_user_id = registry.get("by_user_id", {})
        events: list[dict[str, object]] = []
        for turn in turns:
            meta = cls._load_json(turn.meta_json, {})
            if not isinstance(meta, dict):
                meta = {}
            player_name = None
            player_slug = None
            if turn.user_id is not None:
                entry = by_user_id.get(turn.user_id) or {}
                player_name = str(entry.get("name") or f"Player {turn.user_id}").strip()
                player_slug = str(entry.get("player_slug") or "").strip() or None
            events.append(
                {
                    "turn_id": int(turn.id),
                    "created_at": turn.created.isoformat() if turn.created else None,
                    "kind": str(turn.kind or ""),
                    "user_id": int(turn.user_id) if turn.user_id is not None else None,
                    "player_name": player_name,
                    "player_slug": player_slug,
                    "channel_id": int(turn.channel_id) if turn.channel_id is not None else None,
                    "discord_message_id": (
                        int(turn.discord_message_id)
                        if turn.discord_message_id is not None
                        else None
                    ),
                    "user_message_id": (
                        int(turn.user_message_id)
                        if turn.user_message_id is not None
                        else None
                    ),
                    "content": str(turn.content or ""),
                    "meta": meta,
                }
            )
        return events

    @classmethod
    def _campaign_raw_export_filename(cls, raw_format: str) -> str:
        fmt = str(raw_format or "jsonl").strip().lower()
        if fmt == "json":
            return "campaign-raw.json"
        if fmt == "markdown":
            return "campaign-raw-markdown.md"
        if fmt == "script":
            return "campaign-raw-script.txt"
        if fmt == "loglines":
            return "campaign-raw-loglines.txt"
        return "campaign-raw.jsonl"

    @classmethod
    def _render_campaign_raw_jsonl(
        cls,
        campaign: ZorkCampaign,
        events: list[dict[str, object]],
    ) -> str:
        rows = [
            {
                "type": "campaign",
                "campaign_id": int(campaign.id),
                "campaign_name": str(campaign.name or ""),
                "guild_id": int(campaign.guild_id),
                "created_at": campaign.created.isoformat() if campaign.created else None,
                "updated_at": campaign.updated.isoformat() if campaign.updated else None,
            }
        ]
        rows.extend({"type": "turn", **event} for event in events)
        return "\n".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows
        ).strip()

    @classmethod
    def _render_campaign_raw_json(
        cls,
        campaign: ZorkCampaign,
        events: list[dict[str, object]],
    ) -> str:
        payload = {
            "campaign": {
                "id": int(campaign.id),
                "name": str(campaign.name or ""),
                "guild_id": int(campaign.guild_id),
                "created_at": campaign.created.isoformat() if campaign.created else None,
                "updated_at": campaign.updated.isoformat() if campaign.updated else None,
            },
            "events": events,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

    @classmethod
    def _render_campaign_raw_markdown(
        cls,
        campaign: ZorkCampaign,
        events: list[dict[str, object]],
    ) -> str:
        lines = [
            f"# Campaign Raw Export: {campaign.name}",
            "",
            "## Table of Contents",
            "",
            "- [Campaign Metadata](#campaign-metadata)",
        ]
        for event in events:
            lines.append(f"- [Turn {event.get('turn_id')}](#turn-{event.get('turn_id')})")
        lines.extend(
            [
                "",
                "## Campaign Metadata",
                "",
                f"- Campaign ID: `{campaign.id}`",
                f"- Guild ID: `{campaign.guild_id}`",
                f"- Created: `{campaign.created.isoformat() if campaign.created else ''}`",
                f"- Updated: `{campaign.updated.isoformat() if campaign.updated else ''}`",
            ]
        )
        for event in events:
            turn_id = event.get("turn_id")
            lines.extend(
                [
                    "",
                    f"## Turn {turn_id}",
                    "",
                    f"- Kind: `{event.get('kind')}`",
                    f"- Timestamp: `{event.get('created_at') or ''}`",
                    f"- User ID: `{event.get('user_id')}`",
                    f"- Player: `{event.get('player_name') or ''}`",
                    f"- Player Slug: `{event.get('player_slug') or ''}`",
                    "",
                    "### Content",
                    "",
                    "```text",
                    str(event.get("content") or ""),
                    "```",
                    "",
                    "### Meta",
                    "",
                    "```json",
                    json.dumps(event.get("meta") or {}, ensure_ascii=False, indent=2, sort_keys=True),
                    "```",
                ]
            )
        return "\n".join(lines).strip()

    @classmethod
    def _render_campaign_raw_script(
        cls,
        campaign: ZorkCampaign,
        events: list[dict[str, object]],
    ) -> str:
        lines = [
            f"CAMPAIGN\t{campaign.name}",
            f"\tID\t{campaign.id}",
            f"\tGUILD\t{campaign.guild_id}",
            f"\tCREATED\t{campaign.created.isoformat() if campaign.created else ''}",
            f"\tUPDATED\t{campaign.updated.isoformat() if campaign.updated else ''}",
        ]
        for event in events:
            lines.append("")
            lines.append(f"TURN\t{event.get('turn_id')}")
            lines.append(f"\tKIND\t{event.get('kind')}")
            lines.append(f"\tTIMESTAMP\t{event.get('created_at') or ''}")
            lines.append(f"\tUSER_ID\t{event.get('user_id')}")
            lines.append(f"\tPLAYER\t{event.get('player_name') or ''}")
            lines.append(f"\tPLAYER_SLUG\t{event.get('player_slug') or ''}")
            lines.append(f"\tCHANNEL_ID\t{event.get('channel_id')}")
            lines.append("\tCONTENT")
            for row in str(event.get("content") or "").splitlines() or [""]:
                lines.append(f"\t\t{row}")
            lines.append("\tMETA")
            meta_text = json.dumps(
                event.get("meta") or {},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            for row in meta_text.splitlines():
                lines.append(f"\t\t{row}")
        return "\n".join(lines).strip()

    @classmethod
    def _render_campaign_raw_loglines(
        cls,
        campaign: ZorkCampaign,
        events: list[dict[str, object]],
    ) -> str:
        lines = [
            f"[CAMPAIGN EXPORT] campaign={campaign.id} name={campaign.name!r} turns={len(events)}"
        ]
        for event in events:
            label = str(event.get("kind") or "event").upper()
            if event.get("kind") == "player":
                player_name = str(event.get("player_name") or "").strip()
                if player_name:
                    label = f"PLAYER {player_name}"
            elif event.get("kind") == "narrator":
                label = "NARRATOR"
            lines.append(
                f"[TURN #{event.get('turn_id')} | {event.get('created_at') or ''}] {label}: "
                f"{str(event.get('content') or '').strip()}"
            )
        return "\n".join(lines).strip()

    @classmethod
    async def _generate_campaign_raw_export_artifacts(
        cls,
        campaign: ZorkCampaign,
        *,
        raw_format: str = "jsonl",
        status_message=None,
    ) -> dict[str, str]:
        fmt = str(raw_format or "jsonl").strip().lower()
        if fmt not in {"script", "markdown", "json", "jsonl", "loglines"}:
            fmt = "jsonl"
        await cls._edit_progress_message(
            status_message,
            f"Campaign export: collecting raw turn events ({fmt})...",
        )
        events = cls._campaign_export_turn_events(campaign)
        if not events:
            return {}
        await cls._edit_progress_message(
            status_message,
            f"Campaign export: rendering raw export ({fmt})...",
        )
        if fmt == "json":
            text = cls._render_campaign_raw_json(campaign, events)
        elif fmt == "markdown":
            text = cls._render_campaign_raw_markdown(campaign, events)
        elif fmt == "script":
            text = cls._render_campaign_raw_script(campaign, events)
        elif fmt == "loglines":
            text = cls._render_campaign_raw_loglines(campaign, events)
        else:
            text = cls._render_campaign_raw_jsonl(campaign, events)
        if not str(text or "").strip():
            return {}
        out = {cls._campaign_raw_export_filename(fmt): str(text or "").strip()}
        await cls._edit_progress_message(
            status_message,
            f"Campaign export: packaged {len(out)} raw file(s).",
        )
        return out

    @classmethod
    async def _edit_progress_message(cls, status_message, content: str) -> None:
        if status_message is None:
            return
        try:
            await status_message.edit(content=str(content or "").strip()[:3900] or "Working...")
        except Exception:
            pass

    @classmethod
    async def _delete_progress_message(cls, status_message) -> None:
        if status_message is None:
            return
        try:
            await status_message.delete()
        except Exception:
            pass

    @classmethod
    async def _generate_campaign_export_digest(
        cls,
        campaign: ZorkCampaign,
        transcript: str,
        ctx_message,
        *,
        channel=None,
        status_message=None,
    ) -> str:
        await cls._edit_progress_message(
            status_message,
            "Campaign export: summarising the full playthrough from first turn to last...",
        )
        ordered_chunk_digest = await cls._summarise_long_text(
            transcript,
            ctx_message,
            channel=channel,
            campaign=campaign,
            summary_instructions=(
                "This is a complete campaign transcript from the first turn to the latest turn. "
                "Preserve the entire story arc in chronological order. Do not collapse the story into a vague world-state summary. "
                "Track what happens early, middle, and late; major and minor arcs; character relationship changes; discoveries; "
                "state changes; travel; inventory/item changes that mattered; time jumps; player-to-player dynamics; private reveals "
                "that later became relevant; NPC attitude shifts; recurring jokes; and unresolved threads. "
                "When facts conflict, preserve both versions if needed but clearly favor the later explicit outcome. "
                "Be comprehensive and concrete."
            ),
            show_progress=True,
            allow_single_chunk_passthrough=False,
            progress_label="Campaign export: summarising full transcript",
        )
        ordered_chunk_digest = str(ordered_chunk_digest or "").strip()
        if not ordered_chunk_digest:
            return ""

        await cls._edit_progress_message(
            status_message,
            "Campaign export: fusing the transcript summaries into one complete story-arc digest...",
        )
        digest_system = (
            "You convert an ordered set of campaign transcript summaries into a faithful whole-campaign digest.\n"
            "Output ONLY plain text.\n"
            "This digest must cover the entire story arc from first turn to last turn without flattening it into a generic setting summary.\n"
            "Use these exact plain-text section labels:\n"
            "FULL ARC OVERVIEW\n"
            "CHRONOLOGICAL STORY BEATS\n"
            "PLAYER THREADS\n"
            "NPC ARCS\n"
            "RELATIONSHIPS AND REVEALS\n"
            "LOCATIONS ITEMS AND STATE CHANGES\n"
            "OPEN THREADS AND AFTERMATH\n"
            "CURRENT END STATE\n"
            "CONFLICT RESOLUTION NOTES\n"
            "Requirements:\n"
            "- preserve chronology from opening setup to ending state\n"
            "- name the real player characters and major NPCs repeatedly where relevant\n"
            "- mention major chapter/scene transitions when known\n"
            "- include all lasting arcs, even if they seemed small at the time\n"
            "- if two facts conflict, choose the most sensible truthful version and say why in CONFLICT RESOLUTION NOTES\n"
            "- multiplayer campaigns are ensemble stories, not single-protagonist stories\n"
            "- do not write fiction prose; write a precise reconstruction digest"
        )
        digest_user = (
            f"Campaign: {campaign.name}\n\n"
            "ORDERED TRANSCRIPT SUMMARY:\n"
            f"{ordered_chunk_digest}\n"
        )
        digest_text = await cls._new_gpt(campaign=campaign).turbo_completion(
            digest_system,
            digest_user,
            temperature=0.3,
            max_tokens=12000,
        )
        digest_text = str(digest_text or "").strip()
        return digest_text or ordered_chunk_digest

    @classmethod
    async def _generate_campaign_export_artifacts(
        cls,
        campaign: ZorkCampaign,
        ctx_message,
        *,
        channel=None,
        status_message=None,
    ) -> dict[str, str]:
        await cls._edit_progress_message(
            status_message,
            "Campaign export: building transcript from the full playthrough...",
        )
        transcript = cls._campaign_export_transcript(campaign)
        if not transcript:
            return {}

        campaign_state = cls.get_campaign_state(campaign)
        characters = cls.get_campaign_characters(campaign)
        campaign_players = cls._campaign_players_for_prompt(campaign.id, limit=24)
        story_outline = campaign_state.get("story_outline") if isinstance(campaign_state, dict) else {}
        plot_threads = campaign_state.get("plot_threads") if isinstance(campaign_state, dict) else []
        chapter_plan = campaign_state.get("chapters") if isinstance(campaign_state, dict) else []
        consequences = campaign_state.get("consequences") if isinstance(campaign_state, dict) else []
        model_state = cls._build_model_state(campaign_state if isinstance(campaign_state, dict) else {})
        model_state = cls._fit_state_to_budget(model_state, cls.MAX_STATE_CHARS)
        export_summary = await cls._generate_campaign_export_digest(
            campaign,
            transcript,
            ctx_message,
            channel=channel,
            status_message=status_message,
        )
        if not export_summary:
            export_summary = await cls._summarise_long_text(
                transcript,
                ctx_message,
                channel=channel,
                campaign=campaign,
                summary_instructions=(
                    "Summarise this full campaign playthrough faithfully for export. Preserve lasting facts, "
                    "character arcs, relationship changes, major reveals, locations, items, chapter beats, "
                    "timeline changes, unresolved threads, and the current open state. "
                    "When facts conflict, prefer the later explicit outcome and the persisted world state."
                ),
                allow_single_chunk_passthrough=False,
            )
        export_summary = str(export_summary or "").strip()
        export_summary_excerpt = export_summary
        if len(export_summary_excerpt) > 32000:
            export_summary_excerpt = export_summary_excerpt[:32000].rsplit(" ", 1)[0].strip() + "\n...[truncated excerpt for prompt budget]"
        transcript_excerpt = transcript
        if len(transcript_excerpt) > 20000:
            transcript_excerpt = transcript_excerpt[:20000].rsplit(" ", 1)[0].strip() + "\n...[truncated excerpt for prompt budget]"
        source_payload = cls._source_material_prompt_payload(campaign.id)
        source_index_hint = cls._auto_rulebook_source_index_hint(source_payload)
        source_tool_instructions = ""
        if source_index_hint:
            source_tool_instructions = (
                "\nYou may inspect existing source material while resolving canon.\n"
                'To list keys: {"tool_call": "source_browse"}\n'
                'To browse one doc: {"tool_call": "source_browse", "document_key": "doc-key"}\n'
                'To search canon: {"tool_call": "memory_search", "category": "source", "queries": ["keyword1", "keyword2"]}\n'
                "To call a tool, return ONLY the JSON tool_call object. Otherwise return ONLY the requested final text.\n"
            )

        export_context = {
            "campaign_name": campaign.name,
            "campaign_summary": cls._strip_inventory_mentions(campaign.summary or ""),
            "story_outline": story_outline,
            "chapter_plan": chapter_plan,
            "plot_threads": plot_threads,
            "consequences": consequences,
            "current_state": model_state,
            "characters": characters,
            "campaign_players": campaign_players,
        }

        rulebook_system = (
            "You convert a completed campaign playthrough into a retrievable rulebook for recreating that tale.\n"
            "Output ONLY plain text rulebook lines. No markdown. No headers. No bullets. No numbering.\n"
            "Every line must be fully self-contained and use exactly CATEGORY-TAG: fact text\n"
            "Use category families such as TONE, SCENE, SETTING, CHAR, PLOT, INTERACTION, GM-RULE, and venue-specific tags.\n"
            "Treat WORLD_CHARACTERS as NPC-only and CAMPAIGN_PLAYERS as real human player characters. "
            "In multiplayer campaigns there is no single main character; preserve ensemble structure.\n"
            "Conflict resolution priority:\n"
            "1. Persisted current state / current character roster / current chapter state\n"
            "2. Later explicit turn outcomes in the playthrough summary\n"
            "3. Repeated consistent facts across the transcript\n"
            "4. Existing source material for unchanged background canon\n"
            "If a fact remains uncertain, omit it or phrase it cautiously instead of inventing certainty.\n"
            "Preserve major arcs, resolved outcomes, and unresolved threads so the tale can be recreated faithfully.\n"
            "Do not output a generic world summary. Output dense factual rulebook lines only.\n"
            f"{source_tool_instructions}"
        )
        rulebook_user = (
            f"Generate a campaign rulebook export for '{campaign.name}'.\n"
            f"{source_index_hint}"
            "Use the full playthrough summary and current campaign data below.\n"
            "This export should describe how to faithfully recreate the tale as it was actually played, not just the initial setup.\n\n"
            f"PLAYTHROUGH ARC DIGEST:\n{export_summary_excerpt or '(none)'}\n\n"
            f"EARLY TRANSCRIPT EXCERPT:\n{transcript_excerpt or '(none)'}\n\n"
            f"CAMPAIGN DATA:\n{json.dumps(export_context, indent=2)}\n"
        )
        _zork_log(
            f"CAMPAIGN EXPORT RULEBOOK campaign={campaign.id}",
            f"--- SYSTEM ---\n{rulebook_system}\n--- USER ---\n{rulebook_user}",
        )
        await cls._edit_progress_message(
            status_message,
            "Campaign export: generating factual campaign rulebook...",
        )
        rulebook_response = await cls._setup_tool_loop(
            rulebook_system,
            rulebook_user,
            campaign,
            temperature=0.4,
            max_tokens=cls.AUTO_RULEBOOK_MAX_TOKENS,
            final_response_instruction="Return your final rulebook text now.",
        )
        rulebook_lines = cls._normalize_generated_rulebook_lines(rulebook_response or "")
        if len(rulebook_lines) < 12:
            repair_system = (
                "You repair campaign export drafts into proper retrievable rulebook lines.\n"
                "Output ONLY plain text rulebook lines.\n"
                "Every line must be exactly CATEGORY-TAG: fact text\n"
                "No prose paragraphs. No markdown. No headers.\n"
                "Preserve chronology-derived facts, arcs, characters, plotlines, interactions, and GM rules.\n"
                "If the draft is a summary instead of a rulebook, convert it into many factual rulebook lines."
            )
            repair_user = (
                f"Repair this campaign export into a rulebook for '{campaign.name}'.\n\n"
                f"DRAFT EXPORT:\n{str(rulebook_response or '').strip() or '(empty)'}\n\n"
                f"PLAYTHROUGH ARC DIGEST:\n{export_summary_excerpt or '(none)'}\n\n"
                f"CAMPAIGN DATA:\n{json.dumps(export_context, indent=2)}\n"
            )
            _zork_log(
                f"CAMPAIGN EXPORT RULEBOOK REPAIR campaign={campaign.id}",
                f"--- SYSTEM ---\n{repair_system}\n--- USER ---\n{repair_user}",
            )
            repaired = await cls._new_gpt(campaign=campaign).turbo_completion(
                repair_system,
                repair_user,
                temperature=0.3,
                max_tokens=cls.AUTO_RULEBOOK_MAX_TOKENS,
            )
            repaired_lines = cls._normalize_generated_rulebook_lines(repaired or "")
            if repaired_lines:
                rulebook_lines = repaired_lines
        rulebook_text = "\n".join(rulebook_lines).strip()

        story_prompt_system = (
            "You convert a completed campaign playthrough into a reusable story generator prompt.\n"
            "Output ONLY plain text. No markdown fences.\n"
            "Write a prompt that could recreate the same campaign faithfully: tone, setting, cast, arcs, open threads, "
            "facts, and the current shape of the story.\n"
            "Use clear section labels in plain text such as TITLE, GENRE, FORMAT, PLAY MODE, SETTING, PLAYER CHARACTERS, "
            "NPC CAST, CANON FACTS, MAJOR ARCS, RELATIONSHIPS, OPEN THREADS, OPENING/START STATE, and RECREATION RULES.\n"
            "If this was multiplayer, state clearly that it is an ensemble campaign with multiple real player characters and no single protagonist.\n"
            "Resolve conflicts using the same priority order as the rulebook export: persisted current state, later explicit outcomes, repeated consistent facts, then source canon.\n"
            "Do not write prose fiction. Write a practical generator prompt for reconstructing the campaign.\n"
            "This must reflect the whole story arc from first turn to last turn, not just the ending state."
        )
        story_prompt_user = (
            f"Generate a story generator prompt export for '{campaign.name}'.\n"
            "This should function like a canonical recreation prompt for the whole played campaign.\n\n"
            f"PLAYTHROUGH ARC DIGEST:\n{export_summary_excerpt or '(none)'}\n\n"
            f"EARLY TRANSCRIPT EXCERPT:\n{transcript_excerpt or '(none)'}\n\n"
            f"CAMPAIGN DATA:\n{json.dumps(export_context, indent=2)}\n"
        )
        _zork_log(
            f"CAMPAIGN EXPORT STORY PROMPT campaign={campaign.id}",
            f"--- SYSTEM ---\n{story_prompt_system}\n--- USER ---\n{story_prompt_user}",
        )
        await cls._edit_progress_message(
            status_message,
            "Campaign export: generating story recreation prompt...",
        )
        story_prompt_text = await cls._new_gpt(campaign=campaign).turbo_completion(
            story_prompt_system,
            story_prompt_user,
            temperature=0.5,
            max_tokens=6000,
        )
        story_prompt_text = str(story_prompt_text or "").strip()

        out: dict[str, str] = {}
        if rulebook_text:
            out["campaign-rulebook.txt"] = rulebook_text
        if story_prompt_text:
            out["campaign-story-prompt.txt"] = story_prompt_text
        await cls._edit_progress_message(
            status_message,
            f"Campaign export: packaged {len(out)} file(s). Uploading...",
        )
        return out

    @classmethod
    async def _setup_generate_storyline_variants(
        cls, campaign, setup_data, user_guidance: str = None
    ) -> str:
        """LLM generates 2-3 storyline variants, returns formatted message."""
        is_known = setup_data.get("is_known_work", False)
        raw_name = setup_data.get("raw_name", "unknown")
        work_desc = setup_data.get("work_description", "")
        work_type = setup_data.get("work_type", "work")

        # Build source material index hint if docs are available.
        source_payload = cls._source_material_prompt_payload(campaign.id)
        source_index_hint = ""
        if source_payload.get("available"):
            docs = source_payload.get("docs") or []
            doc_formats = {
                str(doc.get("format") or "generic").strip().lower() for doc in docs
            }
            has_rulebook = "rulebook" in doc_formats
            doc_lines = []
            for doc in docs:
                doc_lines.append(
                    f"  - document_key='{doc.get('document_key')}' "
                    f"label='{doc.get('document_label')}' "
                    f"format='{doc.get('format')}' "
                    f"snippets={doc.get('chunk_count')}"
                )
            browse_instruction = (
                "  Start by enumerating source keys so you know what is available:"
                "\n  {\"tool_call\": \"source_browse\"}\n"
                "  Then query only what you need with memory_search.\n"
            )
            if has_rulebook:
                browse_instruction = (
                    "  Mandatory first step (before any semantic search):"
                    "\n  {\"tool_call\": \"source_browse\"}\n"
                    "  (omit wildcard/document filters on this first pass to list all keys).\n"
                )
            source_index_hint = (
                "\nSOURCE_MATERIAL_INDEX: "
                + f"{source_payload.get('document_count')} document(s), "
                + f"{source_payload.get('chunk_count')} total snippet(s).\n"
                + "\n".join(doc_lines)
                + "\nIMPORTANT: Before generating variants, browse the source material to understand "
                "characters, locations, tone, and rules.\n"
                + browse_instruction
                + "Then drill into specific entries with:\n"
                '  {"tool_call": "memory_search", "category": "source", "queries": ["keyword"]}\n'
                "Only return your final variants JSON after you have reviewed the source material.\n"
                "If any source document is rulebook-formatted, do not skip source_browse for keys.\n"
            )

        name_tool_instructions = (
            "\nYou have a name_generate tool for culturally-appropriate character names.\n"
            "To generate names filtered by origin:\n"
            '  {"tool_call": "name_generate", "origins": ["italian"], "gender": "f", "context": "tough bouncer"}\n'
            "To call a tool, return ONLY the JSON tool_call object (no other keys). "
            "You will receive the results and can call more tools or return your final response.\n"
            "Use name_generate for ALL new original characters instead of inventing names.\n"
        )
        source_tool_instructions = name_tool_instructions
        if source_payload.get("available"):
            has_only_generic = all(
                str(doc.get("format") or "generic").strip().lower() == "generic"
                for doc in docs
            )
            if has_only_generic:
                source_tool_instructions = (
                    "\nYou have tools for source-material exploration, but this source material "
                    "is currently classified as generic and already summarized in attachment text.\n"
                    "Only call source tools when you need exact wording beyond the summary:\n"
                    '  {"tool_call": "memory_search", "category": "source", "queries": ["keyword"]}\n'
                    "To generate culturally-appropriate character names:\n"
                    '  {"tool_call": "name_generate", "origins": ["italian"], "gender": "f", "context": "tough bouncer"}\n'
                    "To call a tool, return ONLY the JSON tool_call object (no other keys). "
                    "You will receive the results and can call more tools or return your final response.\n"
                    "Use name_generate for ALL new original characters instead of inventing names.\n"
                )
            else:
                source_tool_instructions = (
                    "\nYou have tools to inspect ingested source material before generating your response.\n"
                    "MANDATORY: first, enumerate keys before semantic search:\n"
                    '  {"tool_call": "source_browse"}\n'
                    "(omit wildcard on first pass; do not filter yet).\n"
                    "If you need one document only:\n"
                    '  {"tool_call": "source_browse", "document_key": "doc-key"}\n'
                    "After browsing, drill into specifics:\n"
                    '  {"tool_call": "memory_search", "category": "source", "queries": ["query1", "query2"]}\n'
                    "To filter entries by wildcard only after initial listing:\n"
                    '  {"tool_call": "source_browse", "wildcard": "keyword*"}\n'
                    "To generate culturally-appropriate character names:\n"
                    '  {"tool_call": "name_generate", "origins": ["italian"], "gender": "f", "context": "tough bouncer"}\n'
                    "To call a tool, return ONLY the JSON tool_call object (no other keys). "
                    "You will receive the results and can call more tools or return your final response.\n"
                    "ALWAYS browse source material before generating variants — "
                    "the summary alone may not capture all characters, rules, or locations.\n"
                    "Use name_generate for ALL new original characters instead of inventing names.\n"
                )

        system_prompt = (
            "You are a creative game designer who builds interactive text-adventure campaigns.\n"
            "All characters in the game are adults (18+), regardless of source material ages.\n"
            "For non-canonical/original characters, choose distinctive specific names; avoid generic defaults "
            "(Morgan, Chen, Mendoza, Rollins, Nakamura, Kai, River) unless source canon requires them.\n"
            f"{source_tool_instructions}"
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
        genre_context = ""
        genre_pref = setup_data.get("genre_preference")
        if isinstance(genre_pref, dict):
            genre_value = str(genre_pref.get("value") or "").strip()
            genre_kind = str(genre_pref.get("kind") or "").strip().lower()
            if genre_value:
                if genre_kind == "custom":
                    genre_context = (
                        "\nGenre direction (custom):\n"
                        f"{genre_value}\n"
                        "Treat this as a hard style/tone preference while staying coherent.\n"
                    )
                else:
                    genre_context = (
                        f"\nGenre direction: {genre_value}\n"
                        "Prioritize this tone and genre conventions in all variants.\n"
                    )

        if is_known:
            user_prompt = (
                f"Generate 2-3 storyline variants for an interactive text-adventure campaign "
                f"based on the {work_type or 'work'}: '{raw_name}'.\n"
                f"Description: {work_desc}\n"
                f"{imdb_context}"
                f"{attachment_context}"
                f"{source_index_hint}"
                f"{genre_context}"
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
                f"{source_index_hint}"
                f"{genre_context}"
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
                    cur_prompt = (
                        f"{user_prompt}\n\n"
                        "FORMAT REPAIR: Your previous response was invalid or incomplete JSON. "
                        "Return ONLY one valid JSON object with key 'variants' and no trailing text."
                    )
                    _zork_log(f"SETUP VARIANT RETRY campaign={campaign.id}", cur_prompt)
                response = await cls._setup_tool_loop(
                    system_prompt,
                    cur_prompt,
                    campaign,
                    temperature=0.8,
                    max_tokens=3000,
                )
                _zork_log("SETUP VARIANT RAW RESPONSE", response or "(empty)")
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
        """Parse preferences, then finalize. Two-step: on-rails, then puzzle mode."""
        answer = content.strip().lower()
        prefs = setup_data.get("novel_preferences", {})
        if not isinstance(prefs, dict):
            prefs = {}

        if "on_rails" not in prefs:
            # Step 1: parse on-rails answer, ask puzzle question
            if answer in ("on-rails", "onrails", "on rails", "rails", "strict"):
                prefs["on_rails"] = True
            else:
                prefs["on_rails"] = False
            setup_data["novel_preferences"] = prefs
            state["setup_data"] = setup_data
            campaign.state_json = cls._dump_json(state)
            campaign.updated = db.func.now()
            db.session.commit()
            return (
                "2. **Puzzle encounters?** Should the campaign include mechanical challenges "
                "(dice rolls, riddles, mini-games)?\n"
                "Options: **none** / **light** (environmental puzzles only) / "
                "**moderate** (+ skill checks & riddles) / **full** (+ mini-games)\n"
            )
        else:
            # Step 2: parse puzzle mode, finalize
            puzzle_mode = "none"
            if answer in ("light", "environmental"):
                puzzle_mode = "light"
            elif answer in ("moderate", "mod", "skill", "skill checks", "riddles"):
                puzzle_mode = "moderate"
            elif answer in ("full", "all", "yes", "heavy", "mini-games", "minigames"):
                puzzle_mode = "full"
            prefs["puzzle_mode"] = puzzle_mode
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

        # Build source material index hint if docs are available.
        source_payload = cls._source_material_prompt_payload(campaign.id)
        source_index_hint = ""
        if source_payload.get("available"):
            doc_lines = []
            for doc in source_payload.get("docs") or []:
                doc_lines.append(
                    f"  - document_key='{doc.get('document_key')}' "
                    f"label='{doc.get('document_label')}' "
                    f"format='{doc.get('format')}' "
                    f"snippets={doc.get('chunk_count')}"
                )
            source_index_hint = (
                "\nSOURCE_MATERIAL_INDEX: "
                f"{source_payload.get('document_count')} document(s), "
                f"{source_payload.get('chunk_count')} total snippet(s).\n"
                + "\n".join(doc_lines)
                + "\nIMPORTANT: Before building the world, browse the source material to understand "
                "characters, locations, tone, and rules. Start by listing all keys:\n"
                '  {"tool_call": "source_browse"}\n'
                "Then drill into specific entries with:\n"
                '  {"tool_call": "memory_search", "category": "source", "queries": ["keyword"]}\n'
                "Only return your final world JSON after you have reviewed the source material.\n"
            )

        name_tool_instructions = (
            "\nYou have a name_generate tool for culturally-appropriate character names.\n"
            "To generate names filtered by origin:\n"
            '  {"tool_call": "name_generate", "origins": ["italian"], "gender": "f", "context": "tough bouncer"}\n'
            "To call a tool, return ONLY the JSON tool_call object (no other keys). "
            "You will receive the results and can call more tools or return your final response.\n"
            "Use name_generate for ALL new original characters instead of inventing names.\n"
        )
        source_tool_instructions = name_tool_instructions
        if source_payload.get("available"):
            source_tool_instructions = (
                "\nYou have tools to inspect ingested source material before generating your response.\n"
                "To list all entries in a source document:\n"
                '  {"tool_call": "source_browse", "document_key": "doc-key"}\n'
                "To list all entries across all documents:\n"
                '  {"tool_call": "source_browse"}\n'
                "To filter entries by wildcard:\n"
                '  {"tool_call": "source_browse", "wildcard": "keyword*"}\n'
                "To semantic-search source material:\n"
                '  {"tool_call": "memory_search", "category": "source", "queries": ["query1", "query2"]}\n'
                "To generate culturally-appropriate character names:\n"
                '  {"tool_call": "name_generate", "origins": ["italian"], "gender": "f", "context": "tough bouncer"}\n'
                "To call a tool, return ONLY the JSON tool_call object (no other keys). "
                "You will receive the results and can call more tools or return your final response.\n"
                "ALWAYS browse source material before building the world — "
                "the summary alone may not capture all characters, rules, or locations.\n"
                "Use name_generate for ALL new original characters instead of inventing names.\n"
            )

        finalize_system = (
            "You are a world-builder for interactive text-adventure campaigns.\n"
            "All characters in the game are adults (18+), regardless of source material ages.\n"
            "For non-canonical/original characters, choose distinctive specific names; avoid generic defaults "
            "(Morgan, Chen, Mendoza, Rollins, Nakamura, Kai, River) unless source canon requires them.\n"
            f"{source_tool_instructions}"
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
        genre_context = ""
        genre_pref = setup_data.get("genre_preference")
        if isinstance(genre_pref, dict):
            genre_value = str(genre_pref.get("value") or "").strip()
            if genre_value:
                genre_context = f"\nGenre direction: {genre_value}\n"

        finalize_user = (
            f"Build the complete world for: '{raw_name}'\n"
            f"Known work: {is_known}\n"
            f"Description: {setup_data.get('work_description', '')}\n"
            f"{imdb_context}"
            f"{attachment_context}"
            f"{source_index_hint}"
            f"{genre_context}"
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
                        f"{attachment_context}"
                        f"{source_index_hint}"
                        f"Chosen storyline:\n{json.dumps(chosen, indent=2)}\n\n"
                        "Source-material summary (if present) is authoritative; keep names, locations, and plot faithful to it.\n"
                        "Include all essential NPCs and expand chapters into scenes."
                    )
                    _zork_log(f"SETUP FINALIZE RETRY campaign={campaign.id}", cur_user)
                response = await cls._setup_tool_loop(
                    finalize_system,
                    cur_user,
                    campaign,
                    temperature=0.7,
                    max_tokens=4000,
                )
                _zork_log("SETUP FINALIZE RAW RESPONSE", response or "(empty)")
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

        auto_rulebook_count = 0
        auto_rulebook_msg = ""
        try:
            auto_rulebook_count, auto_rulebook_msg = await cls._generate_campaign_rulebook(
                campaign,
                setup_data,
                chosen,
                world if isinstance(world, dict) else {},
            )
        except Exception as exc:
            logger.warning("Auto rulebook generation crashed: %s", exc)
            _zork_log("SETUP RULEBOOK CRASHED", str(exc))

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
        state["puzzle_mode"] = novel_prefs.get("puzzle_mode", "none")

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
            f"characters={char_count} chapters={chapter_count} on_rails={on_rails} "
            f"auto_rulebook_lines={auto_rulebook_count} auto_rulebook_msg={auto_rulebook_msg!r}",
        )
        return result_msg

    # ── Attachment helpers ─────────────────────────────────────────────

    @classmethod
    def _txt_attachments_from_message(cls, message) -> list:
        attachments = getattr(message, "attachments", None)
        if not attachments:
            inner_message = getattr(message, "message", None)
            attachments = getattr(inner_message, "attachments", None)
        if not attachments:
            return []
        return [
            att
            for att in attachments
            if str(getattr(att, "filename", "") or "").lower().endswith(".txt")
        ]

    @classmethod
    async def _extract_attachment_text_from_attachment(cls, attachment) -> Optional[str]:
        if not attachment or not getattr(attachment, "filename", None):
            return None
        if not str(attachment.filename).lower().endswith(".txt"):
            return None
        if attachment.size and attachment.size > cls.ATTACHMENT_MAX_BYTES:
            size_kb = attachment.size // 1024
            limit_kb = cls.ATTACHMENT_MAX_BYTES // 1024
            return f"ERROR:File too large ({size_kb}KB, limit {limit_kb}KB)"
        try:
            raw = await attachment.read()
        except Exception as e:
            logger.warning(f"Attachment read failed: {e}")
            return "ERROR:Could not read attached `.txt` file. Please re-upload and try again."
        if not raw:
            return "ERROR:Attached `.txt` file is empty."
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        text = text.strip()
        return text if text else "ERROR:Attached `.txt` file is empty."

    @classmethod
    async def _extract_attachment_texts_from_message(cls, message) -> list[tuple]:
        extracted: list[tuple] = []
        for attachment in cls._txt_attachments_from_message(message):
            attachment_text = await cls._extract_attachment_text_from_attachment(attachment)
            extracted.append((attachment, attachment_text))
        return extracted

    @classmethod
    async def _extract_attachment_text(cls, message) -> Optional[str]:
        """Return raw text from first .txt attachment, error string, or None."""
        attachments = cls._txt_attachments_from_message(message)
        if not attachments:
            return None
        return await cls._extract_attachment_text_from_attachment(attachments[0])

    ATTACHMENT_MAX_CHUNKS = 8  # dynamic chunk sizing target

    @classmethod
    def _is_attachment_header_line(cls, line: str) -> bool:
        stripped = str(line or "").strip()
        if not stripped:
            return False
        if re.match(r"^#{1,6}\s+\S", stripped):
            return True
        return bool(re.match(r"^[A-Z0-9][A-Z0-9 _/\-()&'.]{1,80}:\s*$", stripped))

    @classmethod
    def _is_attachment_indented_line(cls, line: str) -> bool:
        raw = str(line or "").rstrip("\n")
        return bool(re.match(r"^(?:\t+|\s{4,})\S", raw))

    @classmethod
    def _split_attachment_structural_blocks(cls, text: str) -> List[str]:
        clean = str(text or "").strip()
        if not clean:
            return []
        lines = clean.splitlines()
        blocks: List[str] = []
        current: List[str] = []

        def flush_current() -> None:
            if not current:
                return
            block = "\n".join(current).strip()
            current.clear()
            if block:
                blocks.append(block)

        for raw_line in lines:
            line = raw_line.rstrip()
            stripped = line.strip()
            if not stripped:
                flush_current()
                continue
            if cls._is_attachment_header_line(line):
                flush_current()
                blocks.append(stripped)
                continue
            if cls._is_attachment_indented_line(raw_line):
                flush_current()
                blocks.append(line)
                continue
            current.append(line)
        flush_current()
        return blocks or [clean]

    @classmethod
    def _hard_wrap_attachment_text(
        cls,
        text: str,
        *,
        target_chunk_tokens: int,
    ) -> List[str]:
        clean = str(text or "").strip()
        if not clean:
            return []
        chars_per_tok = max(len(clean) / max(glm_token_count(clean), 1), 1.0)
        target_chars = max(512, int(target_chunk_tokens * chars_per_tok))
        out: List[str] = []
        start = 0
        length = len(clean)
        while start < length:
            end = min(length, start + target_chars)
            if end < length:
                window = clean[start:end]
                breakpoints = [
                    window.rfind("\n\n"),
                    window.rfind("\n"),
                    window.rfind("    "),
                    window.rfind("\t"),
                    window.rfind(" "),
                ]
                best_break = max(breakpoints)
                if best_break > max(256, target_chars // 3):
                    end = start + best_break
            piece = clean[start:end].strip()
            if piece:
                out.append(piece)
            start = max(end, start + 1)
            while start < length and clean[start].isspace():
                start += 1
        return out

    @classmethod
    def _pack_attachment_chunks(
        cls,
        segments: List[str],
        *,
        target_chunk_tokens: int,
    ) -> List[str]:
        packed: List[str] = []
        current: List[str] = []

        def flush_current() -> None:
            if not current:
                return
            block = "\n\n".join(current).strip()
            current.clear()
            if block:
                packed.append(block)

        for segment in segments:
            piece = str(segment or "").strip()
            if not piece:
                continue
            piece_tokens = glm_token_count(piece)
            if piece_tokens > target_chunk_tokens:
                flush_current()
                packed.extend(
                    cls._hard_wrap_attachment_text(
                        piece,
                        target_chunk_tokens=target_chunk_tokens,
                    )
                )
                continue
            if not current:
                current.append(piece)
                continue
            candidate = "\n\n".join([*current, piece]).strip()
            if glm_token_count(candidate) <= target_chunk_tokens:
                current.append(piece)
                continue
            flush_current()
            current.append(piece)
        flush_current()
        return packed

    @classmethod
    def _chunk_text_by_tokens(
        cls,
        text: str,
        *,
        min_chunk_tokens: Optional[int] = None,
        max_chunks: Optional[int] = None,
    ) -> Tuple[List[str], int, int, float, int]:
        clean = str(text or "").strip()
        if not clean:
            return [], 0, 0, 0.0, 0
        total_tokens = glm_token_count(clean)
        chunk_floor = max(1, int(min_chunk_tokens or cls.ATTACHMENT_CHUNK_TOKENS))
        chunk_limit = max(1, int(max_chunks or cls.ATTACHMENT_MAX_CHUNKS))
        target_chunk_tokens = max(chunk_floor, total_tokens // chunk_limit)
        chars_per_tok = len(clean) / max(total_tokens, 1)
        chunk_char_target = max(1, int(target_chunk_tokens * chars_per_tok))
        blocks = cls._split_attachment_structural_blocks(clean)
        chunks = cls._pack_attachment_chunks(
            blocks,
            target_chunk_tokens=target_chunk_tokens,
        )
        if not chunks:
            chunks = cls._hard_wrap_attachment_text(
                clean,
                target_chunk_tokens=target_chunk_tokens,
            )
        return chunks, total_tokens, target_chunk_tokens, chars_per_tok, chunk_char_target

    @classmethod
    def _estimate_attachment_chunk_count(cls, text: str) -> int:
        chunks, _, _, _, _ = cls._chunk_text_by_tokens(text)
        return len(chunks)

    @classmethod
    def _attachment_setup_length_error(cls, text: str) -> Optional[str]:
        # Short setup attachments are accepted; no minimum chunk threshold.
        return None

    @classmethod
    def _attachment_fallback_summary(cls, text: str) -> str:
        """Deterministic setup fallback when all model chunk summaries fail."""
        clean = str(text or "").strip()
        if not clean:
            return ""
        chunks, _, _, _, _ = cls._chunk_text_by_tokens(
            clean,
            min_chunk_tokens=cls.ATTACHMENT_CHUNK_TOKENS,
            max_chunks=6,
        )
        if not chunks:
            return ""
        selected = chunks[:6]
        lines: List[str] = [
            "Fallback extraction from uploaded source text (automated summary failed):"
        ]
        for idx, chunk in enumerate(selected, start=1):
            snippet = " ".join(str(chunk or "").split())
            if len(snippet) > 1200:
                snippet = snippet[:1200].rsplit(" ", 1)[0].strip() + "..."
            lines.append(f"[Excerpt {idx}/{len(selected)}] {snippet}")
        result = "\n\n".join(lines).strip()
        if len(result) > 9000:
            result = result[:9000].rsplit(" ", 1)[0].strip() + "..."
        return result

    @classmethod
    async def _summarise_long_text(
        cls,
        text: str,
        ctx_message,
        channel=None,
        campaign: Optional[ZorkCampaign] = None,
        summary_instructions: Optional[str] = None,
        show_progress: bool = True,
        allow_single_chunk_passthrough: bool = True,
        progress_label: str = "Summarising uploaded file",
    ) -> str:
        """Chunk, summarise in parallel, condense to budget. Returns summary.
        *channel* overrides ctx_message.channel for progress messages.
        All sizing uses the GLM tokenizer for accurate token counts."""
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
        gpt = cls._new_gpt(
            campaign=campaign,
            channel_id=getattr(progress_channel, "id", None),
        )

        # Step 1 — token-aware dynamic chunking
        chunks, total_tokens, target_chunk_tokens, chars_per_tok, chunk_char_target = await asyncio.to_thread(
            cls._chunk_text_by_tokens,
            text,
            min_chunk_tokens=min_chunk_tokens,
            max_chunks=cls.ATTACHMENT_MAX_CHUNKS,
        )

        if not chunks:
            return ""

        # If single chunk within budget, return as-is
        if (
            allow_single_chunk_passthrough
            and len(chunks) == 1
            and await asyncio.to_thread(glm_token_count, chunks[0]) <= budget_tokens
        ):
            return chunks[0]

        total = len(chunks)
        _zork_log(
            "ATTACHMENT SUMMARISE",
            f"text_len={len(text)} total_tokens={total_tokens} "
            f"chunk_char_target={chunk_char_target} total_chunks={total}",
        )
        status_msg = None
        progress_title = str(progress_label or "Summarising uploaded file").strip()
        if show_progress:
            status_msg = await progress_channel.send(
                f"{progress_title}... [0/{total}]"
            )

        # Step 2 — parallel summarise
        # Scale max_tokens with chunk size so larger chunks get more summary room
        summary_max_tokens = min(
            cls.ATTACHMENT_SUMMARY_MAX_TOKENS,
            max(8_000, target_chunk_tokens // 2),
        )
        instruction_text = " ".join(str(summary_instructions or "").strip().split())[:600]
        summarise_system = (
            "Summarise the following text passage for a text-adventure campaign. "
            "Preserve all character names, plot points, locations, and key events. "
            f"Be detailed but concise. End with the exact line: {guard}"
        )
        if instruction_text:
            summarise_system = (
                f"{summarise_system}\n"
                "Additional user instruction for this summary:\n"
                f"{instruction_text}"
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
            if status_msg is not None:
                try:
                    await status_msg.edit(
                        content=f"{progress_title}... [{processed}/{total}]"
                    )
                except Exception:
                    pass

        # Filter empty summaries
        summaries = [s for s in summaries if s]
        if not summaries:
            logger.error("All chunk summaries failed")
            fallback = cls._attachment_fallback_summary(text)
            if fallback:
                _zork_log(
                    "ATTACHMENT SUMMARY FALLBACK",
                    f"text_len={len(text)} fallback_chars={len(fallback)}",
                )
            if status_msg is not None:
                try:
                    if fallback:
                        await status_msg.edit(
                            content=f"{progress_title} failed — using direct source excerpts fallback."
                        )
                    else:
                        await status_msg.edit(
                            content=f"{progress_title} failed — continuing without summary."
                        )
                    await asyncio.sleep(5)
                    await status_msg.delete()
                except Exception:
                    pass
            return fallback

        # Step 3 — check total token length
        joined = "\n\n".join(summaries)
        joined_tokens = await asyncio.to_thread(glm_token_count, joined)
        if joined_tokens <= budget_tokens:
            _zork_log(
                "ATTACHMENT SUMMARY DONE",
                f"tokens={joined_tokens} chars={len(joined)} (within budget)",
            )
            if status_msg is not None:
                try:
                    file_kb = len(text) // 1024
                    await status_msg.edit(
                        content=f"{progress_title} complete. ({joined_tokens} tokens from {file_kb}KB source)"
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
        summary_tok_counts = await asyncio.to_thread(
            lambda: [glm_token_count(s) for s in summaries]
        )
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
            if status_msg is not None:
                try:
                    await status_msg.edit(
                        content=f"{progress_title}: condensing... [0/{condense_total}]"
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
                        max_tokens=min(
                            cls.ATTACHMENT_SUMMARY_MAX_TOKENS,
                            max(2_048, target_tokens_per + 256),
                        ),
                        temperature=0.2,
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
                if status_msg is not None:
                    try:
                        await status_msg.edit(
                            content=f"{progress_title}: condensing... [{condense_done}/{condense_total}]"
                        )
                    except Exception:
                        pass

        joined = "\n\n".join(summaries)
        joined_tokens = await asyncio.to_thread(glm_token_count, joined)
        # Hard-truncate if still over budget (rare after condensation)
        if joined_tokens > budget_tokens:
            # Trim by chars using the ratio — slightly conservative
            max_chars = int(budget_tokens * chars_per_tok * 0.9)
            if len(joined) > max_chars:
                joined = joined[: max_chars - len("... [truncated]")] + "... [truncated]"
                joined_tokens = await asyncio.to_thread(glm_token_count, joined)

        # Step 5 — final edit + cleanup
        _zork_log(
            "ATTACHMENT SUMMARY DONE",
            f"tokens={joined_tokens} chars={len(joined)} chunks={total} "
            f"condensed={len(to_condense) if to_condense else 0}",
        )
        if status_msg is not None:
            try:
                file_kb = len(text) // 1024
                await status_msg.edit(
                    content=f"{progress_title} complete. ({joined_tokens} tokens from {file_kb}KB source)"
                )
                await asyncio.sleep(5)
                await status_msg.delete()
            except Exception:
                pass

        return joined

    @classmethod
    async def _build_turn_attachment_context(cls, ctx) -> Optional[str]:
        attachment_infos = await cls._extract_attachment_texts_from_message(ctx)
        if not attachment_infos:
            return None

        blocks: List[str] = []
        for attachment, attachment_text in attachment_infos:
            if isinstance(attachment_text, str) and attachment_text.startswith("ERROR:"):
                logger.warning("Turn attachment skipped: %s", attachment_text)
                continue
            text = str(attachment_text or "").strip()
            if not text:
                continue

            filename = str(getattr(attachment, "filename", "") or "attachment.txt").strip()
            attachment_size = getattr(attachment, "size", None)
            if not isinstance(attachment_size, int) or attachment_size <= 0:
                attachment_size = len(text.encode("utf-8", errors="ignore"))

            mode = "raw"
            payload = text
            if attachment_size > cls.TURN_ATTACHMENT_INLINE_BYTES:
                mode = "summary"
                payload = await cls._summarise_long_text(
                    text,
                    ctx,
                    campaign=None,
                    summary_instructions=cls.TURN_ATTACHMENT_SUMMARY_INSTRUCTIONS,
                    show_progress=False,
                    allow_single_chunk_passthrough=False,
                )
                payload = str(payload or "").strip()
                if not payload:
                    continue

            blocks.append(
                "\n".join(
                    [
                        f"FILE: {filename}",
                        f"MODE: {mode}",
                        "SCOPE: ephemeral turn-only reference; do not treat as permanent canon or store it as memory unless the player explicitly asks to establish it in-world.",
                        "CONTENT:",
                        payload,
                    ]
                ).strip()
            )

        if not blocks:
            return None
        return "\n\n---\n\n".join(blocks).strip()

    @classmethod
    def _extract_attachment_label(cls, message, fallback: str = "source-material") -> str:
        if isinstance(message, (list, tuple)):
            attachments = message
        else:
            attachments = getattr(message, "attachments", None) or []
        for att in attachments:
            filename = str(getattr(att, "filename", "") or "").strip()
            if not filename.lower().endswith(".txt"):
                continue
            stem = filename.rsplit("/", 1)[-1]
            stem = stem[:-4] if stem.lower().endswith(".txt") else stem
            stem = " ".join(stem.replace("_", " ").replace("-", " ").split())
            if stem:
                return stem[:120]
        return str(fallback or "source-material").strip()[:120] or "source-material"

    @classmethod
    def _normalize_source_material_format(cls, raw_format: str) -> str:
        normalized = str(raw_format or "").strip().lower()
        normalized = re.sub(r"[^a-z0-9\s-]", "", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if normalized in {"rulebook", "rule-book", "rule_book", "factbook", "rule"}:
            return cls.SOURCE_MATERIAL_FORMAT_RULEBOOK
        if normalized in {
            "story",
            "scripted",
            "story-scripted",
            "story mode",
            "script",
            "scripted story",
            "narrative",
        }:
            return cls.SOURCE_MATERIAL_FORMAT_STORY
        if normalized in {
            "generic",
            "other",
            "dumps",
            "dump",
            "notes",
            "unknown",
        }:
            return cls.SOURCE_MATERIAL_FORMAT_GENERIC
        if (
            "rulebook" in normalized
            or "open set" in normalized
            or "open-set" in normalized
            or "openset" in normalized
        ):
            return cls.SOURCE_MATERIAL_FORMAT_RULEBOOK
        if "script" in normalized or "story" in normalized:
            return cls.SOURCE_MATERIAL_FORMAT_STORY
        if "generic" in normalized or "dump" in normalized:
            return cls.SOURCE_MATERIAL_FORMAT_GENERIC
        return cls.SOURCE_MATERIAL_FORMAT_GENERIC

    @classmethod
    def _source_material_format_heuristic(cls, sample: str) -> str:
        sample_text = str(sample or "").strip()
        lines = [line.strip() for line in str(sample or "").splitlines() if line.strip()]
        if not lines:
            return cls.SOURCE_MATERIAL_FORMAT_GENERIC

        # Heuristic rulebook detection:
        # lines like KEY: value with short key-like prefixes.
        rulebook_lines = 0
        for line in lines[:80]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if not key or not value or len(key) > 140:
                continue
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-\s]*", key):
                rulebook_lines += 1

        if len(lines) == 1 and rulebook_lines == 1:
            return cls.SOURCE_MATERIAL_FORMAT_RULEBOOK

        if len(lines) >= 4 and rulebook_lines >= max(2, len(lines) * 0.45):
            return cls.SOURCE_MATERIAL_FORMAT_RULEBOOK

        # Heuristic story detection from paragraph-style prose.
        if "\n\n" in sample_text:
            return cls.SOURCE_MATERIAL_FORMAT_STORY

        if len(sample_text.split()) >= 140 and any(len(line) > 120 for line in lines):
            return cls.SOURCE_MATERIAL_FORMAT_STORY

        return cls.SOURCE_MATERIAL_FORMAT_GENERIC

    @classmethod
    def _source_material_storage_mode(cls, source_format: str) -> str:
        return cls.SOURCE_MATERIAL_MODE_MAP.get(
            cls._normalize_source_material_format(source_format),
            cls.SOURCE_MATERIAL_MODE_MAP[cls.SOURCE_MATERIAL_FORMAT_GENERIC],
        )

    @classmethod
    async def _classify_source_material_format(
        cls,
        sample_text: str,
        *,
        campaign: Optional[ZorkCampaign] = None,
        channel_id: Optional[int] = None,
    ) -> str:
        sample = str(sample_text or "").strip()
        if not sample:
            return cls.SOURCE_MATERIAL_FORMAT_GENERIC

        gpt = cls._new_gpt(campaign=campaign, channel_id=channel_id)
        sample_preview = sample[:4000]
        system_prompt = (
            "Classify the attached source material into exactly one of three categories.\n"
            "Valid values: story, rulebook, generic.\n"
            "story = scripted narrative, prose, scenes, dialogue, or outline text.\n"
            'rulebook = open-set fact list where each fact is usually one line in "KEY: fact" form.\n'
            "generic = everything else (notes, dumps, mixed structure, etc.).\n"
            'Return ONLY JSON: {"source_material_format": "story|rulebook|generic"}.\n'
            "Do not include markdown, explanation, or extra keys."
        )
        user_prompt = (
            "Classify this sample from an uploaded source text.\n"
            "Sample:\n"
            f"{sample_preview}\n"
            "Return only one JSON key `source_material_format`."
        )
        response = None
        cleaned = ""
        parsed = {}
        try:
            response = await gpt.turbo_completion(
                system_prompt,
                user_prompt,
                temperature=0.2,
                max_tokens=120,
            )
            cleaned = cls._clean_response(response or "")
            json_text = cls._extract_json(cleaned)
            if json_text:
                parsed = cls._parse_json_lenient(json_text)
        except Exception as e:
            logger.warning(f"Source material classification failed (LLM parse): {e}")

        if not isinstance(parsed, dict):
            parsed = {}

        if not parsed:
            return cls._source_material_format_heuristic(sample)

        resolved_format = cls._normalize_source_material_format(
            str(
                parsed.get("source_material_format")
                or parsed.get("format")
                or parsed.get("type")
                or ""
            )
        )
        if resolved_format:
            return resolved_format

        return cls._source_material_format_heuristic(sample)

    @classmethod
    async def _analyze_literary_style(
        cls,
        text: str,
        label: str,
        *,
        campaign: Optional[ZorkCampaign] = None,
        channel_id: Optional[int] = None,
    ) -> Dict[str, dict]:
        """Extract prose-craft profiles from literary text.

        Returns ``{key: {"profile": str, "source_label": str, "created_at": ISO}}``
        where *key* is ``label`` or ``label-SUFFIX`` when multiple registers exist.
        """
        sample = str(text or "").strip()[:8000]
        if not sample:
            return {}

        gpt = cls._new_gpt(campaign=campaign, channel_id=channel_id)
        system_prompt = (
            "You are a prose-craft analyst. Given a sample of literary text, extract the CRAFT — "
            "not the content. Describe: sentence rhythm and length patterns, vocabulary register, "
            "what the prose avoids, phrasing habits, emotional register, punctuation choices, "
            "and any distinctive texture.\n"
            "Do NOT summarise the plot, name characters, or describe events.\n"
            "Return JSON: {\"profiles\": [{\"key_suffix\": null, \"profile\": \"...\"}]}\n"
            "key_suffix is null for a single unified profile. If the sample contains distinctly "
            "different registers (e.g. argument vs tender), you MAY split into multiple profiles "
            "with key_suffix like \"ARGUMENT\", \"TENDER\". Each profile must be ≤400 characters.\n"
            "Return ONLY valid JSON, no markdown, no extra text."
        )
        user_prompt = (
            f"Analyse the prose craft of this sample labelled \"{label}\".\n"
            f"Sample:\n{sample}\n"
            "Return only the JSON object."
        )
        response = None
        cleaned = ""
        parsed = {}
        try:
            response = await gpt.turbo_completion(
                system_prompt,
                user_prompt,
                temperature=0.3,
                max_tokens=1200,
            )
            cleaned = cls._clean_response(response or "")
            json_text = cls._extract_json(cleaned)
            if json_text:
                parsed = cls._parse_json_lenient(json_text)
        except Exception as e:
            logger.warning("Literary style analysis failed (LLM parse): %s", e)

        if not isinstance(parsed, dict):
            parsed = {}

        profiles_raw = parsed.get("profiles")
        if not isinstance(profiles_raw, list):
            profiles_raw = []

        now_iso = datetime.datetime.utcnow().isoformat()
        result: Dict[str, dict] = {}
        for entry in profiles_raw:
            if not isinstance(entry, dict):
                continue
            profile_text = str(entry.get("profile") or "").strip()
            if not profile_text:
                continue
            profile_text = profile_text[: cls.MAX_LITERARY_STYLE_PROFILE_CHARS]
            suffix = entry.get("key_suffix")
            if suffix:
                full_key = f"{label}-{str(suffix).strip().upper()}"
            else:
                full_key = label
            result[full_key] = {
                "profile": profile_text,
                "source_label": label,
                "created_at": now_iso,
            }

        if not result and cleaned:
            # Fallback: treat entire cleaned response as a single profile.
            fallback = cleaned[: cls.MAX_LITERARY_STYLE_PROFILE_CHARS]
            result[label] = {
                "profile": fallback,
                "source_label": label,
                "created_at": now_iso,
            }

        return result

    # -- Writing fragment extraction (literary analysis → searchable chunks) --

    _WRITING_FRAGMENT_SAMPLE_SIZE = 3000  # chars per passage sample
    _WRITING_FRAGMENT_MAX_SAMPLES = 12
    _WRITING_FRAGMENT_MIN_SAMPLES = 4
    _WRITING_FRAGMENT_MAX_CHARS = 800  # max chars per craft fragment

    @classmethod
    async def _extract_writing_fragments(
        cls,
        text: str,
        label: str,
        *,
        campaign: Optional[ZorkCampaign] = None,
        channel_id: Optional[int] = None,
    ) -> List[str]:
        """Extract prose-craft observation fragments from a literary text.

        Samples multiple passages spread throughout the text, sends each
        to the LLM for craft analysis, and returns a list of short
        craft-observation strings suitable for embedding and similarity
        search during gameplay.
        """
        full_text = str(text or "").strip()
        if not full_text:
            return []

        # Build passage samples spread evenly through the text.
        sample_size = cls._WRITING_FRAGMENT_SAMPLE_SIZE
        text_len = len(full_text)
        n_samples = max(
            cls._WRITING_FRAGMENT_MIN_SAMPLES,
            min(cls._WRITING_FRAGMENT_MAX_SAMPLES, text_len // sample_size),
        )
        if text_len <= sample_size * 2:
            passages = [full_text]
        else:
            step = max(1, (text_len - sample_size) // (n_samples - 1))
            passages = []
            for i in range(n_samples):
                start = min(i * step, text_len - sample_size)
                end = start + sample_size
                passage = full_text[start:end]
                # Trim to nearest paragraph/sentence boundary.
                first_newline = passage.find("\n", 40)
                if first_newline > 0:
                    passage = passage[first_newline + 1:]
                last_period = passage.rfind(".", 0, -40)
                if last_period > 0:
                    passage = passage[: last_period + 1]
                passage = passage.strip()
                if passage:
                    passages.append(passage)

        if not passages:
            return []

        system_prompt = (
            "You are a prose-craft analyst for a text-adventure game engine. "
            "Given a passage from a literary work, extract CRAFT observations — NOT plot, NOT content.\n"
            "For each distinct technique you observe, produce a short fragment describing it.\n"
            "Focus on: sentence rhythm, clause stacking, vocabulary register, "
            "punctuation choices, metaphor patterns, dialogue-tag texture, "
            "pacing techniques, tense usage, emotional register, and what the prose avoids.\n"
            "Tag each fragment with the REGISTER it applies to (e.g. DESCRIPTION, DIALOGUE, "
            "ACTION, INTROSPECTION, TRANSITION, ATMOSPHERE).\n"
            'Return ONLY JSON: {"fragments": [{"register": "...", "observation": "..."}]}\n'
            f"Each observation must be <= {cls._WRITING_FRAGMENT_MAX_CHARS} characters. "
            "Produce 3-6 fragments per passage."
        )

        gpt = cls._new_gpt(campaign=campaign, channel_id=channel_id)

        async def _analyze_passage(passage: str) -> List[str]:
            user_prompt = (
                f'Analyse the prose craft in this passage from "{label}".\n'
                f"Passage:\n{passage}\n"
                "Return only the JSON object."
            )
            try:
                response = await gpt.turbo_completion(
                    system_prompt,
                    user_prompt,
                    temperature=0.3,
                    max_tokens=1500,
                )
                cleaned = cls._clean_response(response or "")
                json_text = cls._extract_json(cleaned)
                if not json_text:
                    return []
                parsed = cls._parse_json_lenient(json_text)
                if not isinstance(parsed, dict):
                    return []
                raw_fragments = parsed.get("fragments")
                if not isinstance(raw_fragments, list):
                    return []
                out: List[str] = []
                for entry in raw_fragments:
                    if not isinstance(entry, dict):
                        continue
                    register = " ".join(str(entry.get("register") or "GENERAL").split()).upper()[:30]
                    register = "".join(c for c in register if c.isalnum() or c in ("-", "_", " "))
                    if not register:
                        register = "GENERAL"
                    obs = " ".join(str(entry.get("observation") or "").split())
                    if obs:
                        obs = obs[: cls._WRITING_FRAGMENT_MAX_CHARS]
                        out.append(f"[{register}] {obs}")
                return out
            except Exception as exc:
                logger.warning("Writing fragment extraction failed for a passage: %s", exc)
                return []

        # Run in parallel batches of 4.
        all_fragments: List[str] = []
        batch_size = 4
        for batch_start in range(0, len(passages), batch_size):
            batch = passages[batch_start: batch_start + batch_size]
            results = await asyncio.gather(*[_analyze_passage(p) for p in batch])
            for result in results:
                all_fragments.extend(result)

        # Deduplicate very similar fragments (exact match after lowering).
        seen: set = set()
        deduped: List[str] = []
        for frag in all_fragments:
            key = frag.lower().strip()
            if key not in seen:
                seen.add(key)
                deduped.append(frag)
        return deduped

    @classmethod
    async def ingest_source_material_attachment(
        cls,
        campaign: ZorkCampaign,
        message,
        *,
        label: Optional[str] = None,
        channel=None,
    ) -> Tuple[bool, str, Dict[str, dict]]:
        raw_text = await cls._extract_attachment_text(message)
        if isinstance(raw_text, str) and raw_text.startswith("ERROR:"):
            return False, raw_text.replace("ERROR:", "", 1), {}
        if not raw_text:
            return False, "No `.txt` attachment found.", {}

        return await cls.ingest_source_material_text(
            campaign,
            raw_text,
            label=label,
            channel=channel,
            message=message,
        )

    @classmethod
    async def ingest_source_material_text(
        cls,
        campaign: ZorkCampaign,
        raw_text: str,
        *,
        label: Optional[str] = None,
        channel=None,
        source_format: Optional[str] = None,
        message=None,
    ) -> Tuple[bool, str, Dict[str, dict]]:
        """Ingest source material text into the campaign.

        For **story** format: skips raw paragraph chunk storage.  Instead
        generates a narrative digest, extracts prose-craft writing
        fragments via LLM analysis, stores those as searchable chunks,
        and extracts literary style profiles.

        Returns ``(success, result_message, literary_profiles)`` where
        ``literary_profiles`` is a dict suitable for merging into
        ``campaign_state["literary_styles"]`` (empty for non-story formats).
        """
        if not raw_text:
            return False, "No `.txt` attachment found.", {}

        chunks, total_tokens, _, _, _ = cls._chunk_text_by_tokens(raw_text)
        if not chunks:
            return False, "Attachment has no usable text.", {}

        classification_chunk = chunks[0] if chunks else raw_text[:4000]
        if source_format is None:
            try:
                source_format = await cls._classify_source_material_format(
                    classification_chunk,
                    campaign=campaign,
                    channel_id=getattr(channel, "id", None),
                )
            except Exception as e:
                logger.warning(
                    f"Source material classification crashed; defaulting generic: {e}"
                )
                source_format = cls.SOURCE_MATERIAL_FORMAT_GENERIC
        else:
            source_format = cls._normalize_source_material_format(source_format)
            if not source_format:
                source_format = cls.SOURCE_MATERIAL_FORMAT_GENERIC
        source_mode = cls._source_material_storage_mode(source_format)

        resolved_label = " ".join(str(label or "").strip().split())[:120]
        if not resolved_label:
            resolved_label = cls._extract_attachment_label(message)

        progress_channel = channel or getattr(message, "channel", None)
        status_msg = None
        if progress_channel is not None:
            try:
                status_msg = await progress_channel.send(
                    "Classifying source material format... "
                    f"`{resolved_label}` (document has ~{total_tokens} tokens)"
                )
            except Exception:
                status_msg = None

        if status_msg is not None:
            try:
                await status_msg.edit(
                    content=(
                        f"Detected source material format `{source_format}` for "
                        f"`{resolved_label}`."
                    )
                )
            except Exception:
                pass
        if source_format == cls.SOURCE_MATERIAL_FORMAT_GENERIC:
            result_msg = (
                f"Source material format for `{resolved_label}` is `{source_format}`. "
                "It will not be indexed as source chunks and will be used as setup prompt text."
            )
            if status_msg is not None:
                try:
                    await status_msg.edit(content=result_msg)
                    await asyncio.sleep(3)
                    await status_msg.delete()
                except Exception:
                    pass
            return True, result_msg, {}

        duplicate_doc = await asyncio.to_thread(
            ZorkMemory.find_duplicate_source_material_document,
            campaign.id,
            chunks=chunks,
            source_mode=source_mode,
        )
        if duplicate_doc:
            existing_key = str(duplicate_doc.get("document_key") or "").strip() or "unknown"
            existing_label = str(duplicate_doc.get("document_label") or "").strip() or existing_key
            existing_count = int(duplicate_doc.get("chunk_count") or 0)
            result_msg = (
                f"Source material skipped: `{resolved_label}` matches existing document "
                f"`{existing_label}` as key `{existing_key}` ({existing_count} snippet(s), "
                f"{source_format} format)."
            )
            if status_msg is not None:
                try:
                    await status_msg.edit(content=result_msg)
                    await asyncio.sleep(3)
                    await status_msg.delete()
                except Exception:
                    pass
            return True, result_msg, {}

        # ------------------------------------------------------------------
        # Story format: literary analysis pipeline (no raw chunk storage)
        # ------------------------------------------------------------------
        if source_format == cls.SOURCE_MATERIAL_FORMAT_STORY:
            return await cls._ingest_story_literary(
                campaign,
                raw_text,
                resolved_label=resolved_label,
                total_tokens=total_tokens,
                progress_channel=progress_channel,
                status_msg=status_msg,
                message=message,
                channel=channel,
            )

        # ------------------------------------------------------------------
        # Rulebook / other: existing flow (raw chunks + digest)
        # ------------------------------------------------------------------
        stored_count, document_key = await asyncio.to_thread(
            ZorkMemory.store_source_material_chunks,
            campaign.id,
            document_label=resolved_label,
            chunks=chunks,
            source_mode=source_mode,
            replace_document=True,
        )
        docs = await asyncio.to_thread(
            ZorkMemory.list_source_material_documents,
            campaign.id,
            cls.SOURCE_MATERIAL_MAX_DOCS_IN_PROMPT,
        )
        total_chunk_count = 0
        for row in docs:
            try:
                total_chunk_count += int(row.get("chunk_count") or 0)
            except (TypeError, ValueError):
                continue

        if stored_count <= 0:
            if status_msg is not None:
                try:
                    await status_msg.edit(content="Source material ingestion failed.")
                except Exception:
                    pass
            return False, "Source material ingestion failed.", {}

        result_msg = (
            f"Source material stored: `{resolved_label}` as key `{document_key}` "
            f"({stored_count} snippet(s), {source_format} format, ~{total_tokens} tokens). "
            f"Campaign source corpus now has {total_chunk_count} snippet(s) across {len(docs)} document(s)."
        )

        if source_format == cls.SOURCE_MATERIAL_FORMAT_RULEBOOK:
            await asyncio.to_thread(
                ZorkMemory.delete_source_material_digest,
                campaign.id,
                document_key,
            )

        if status_msg is not None:
            try:
                await status_msg.edit(content=result_msg)
                await asyncio.sleep(4)
                await status_msg.delete()
            except Exception:
                pass
        return True, result_msg, {}

    @classmethod
    async def _ingest_story_literary(
        cls,
        campaign: ZorkCampaign,
        raw_text: str,
        *,
        resolved_label: str,
        total_tokens: int,
        progress_channel=None,
        status_msg=None,
        message=None,
        channel=None,
    ) -> Tuple[bool, str, Dict[str, dict]]:
        """Story-format ingestion: digest + writing fragments + literary profiles.

        Instead of storing raw story paragraphs, this method:
        1. Generates a narrative digest (content/plot retrieval).
        2. Extracts prose-craft writing fragments via LLM and stores
           them as the searchable chunks (style retrieval).
        3. Extracts overall literary style profiles for prompt injection.
        """
        document_key = ZorkMemory._normalize_source_document_key(resolved_label)
        result_msg = ""

        # Step 1: Generate narrative digest.
        try:
            digest_progress_msg = None
            if progress_channel is not None:
                try:
                    digest_progress_msg = await progress_channel.send(
                        f"Generating narrative digest for `{resolved_label}`..."
                    )
                except Exception:
                    digest_progress_msg = None

            digest = await cls._summarise_long_text(
                raw_text,
                ctx_message=message,
                channel=progress_channel,
                campaign=campaign,
                summary_instructions=(
                    "This is source material for a text-adventure campaign. "
                    "Produce a comprehensive narrative digest preserving all characters, "
                    "locations, plot arcs, factions, key events, world rules, and "
                    "relationships. Maintain chronological order where applicable. "
                    "Be detailed and concrete — this digest will be used to ground "
                    "the campaign world."
                ),
                show_progress=bool(progress_channel is not None),
                allow_single_chunk_passthrough=False,
                progress_label="Generating source material digest",
            )
            if digest and digest.strip():
                await asyncio.to_thread(
                    ZorkMemory.store_source_material_digest,
                    campaign.id,
                    document_key,
                    digest.strip(),
                )
                result_msg += " Narrative digest generated."

            if digest_progress_msg is not None:
                try:
                    await digest_progress_msg.delete()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(
                f"Story digest generation failed for campaign {campaign.id}: {e}"
            )

        # Step 2: Clear stale chunks up front, then extract writing
        # fragments and store as new chunks.
        def _clear_stale_chunks():
            conn = ZorkMemory._get_conn()
            conn.execute(
                "DELETE FROM source_material_chunks WHERE campaign_id = ? AND document_key = ?",
                (campaign.id, document_key),
            )
            conn.commit()

        try:
            await asyncio.to_thread(_clear_stale_chunks)
        except Exception as e:
            logger.warning(
                f"Failed to clear stale chunks for campaign {campaign.id} key {document_key}: {e}"
            )

        fragment_count = 0
        try:
            fragment_progress_msg = None
            if progress_channel is not None:
                try:
                    fragment_progress_msg = await progress_channel.send(
                        f"Extracting writing style fragments for `{resolved_label}`..."
                    )
                except Exception:
                    fragment_progress_msg = None

            fragments = await cls._extract_writing_fragments(
                raw_text,
                resolved_label,
                campaign=campaign,
                channel_id=getattr(channel, "id", None),
            )
            if fragments:
                fragment_count, document_key = await asyncio.to_thread(
                    ZorkMemory.store_source_material_chunks,
                    campaign.id,
                    document_label=resolved_label,
                    chunks=fragments,
                    source_mode="generic",
                    replace_document=False,  # Already cleared above.
                )
                result_msg += f" {fragment_count} writing fragment(s) stored."

            if fragment_progress_msg is not None:
                try:
                    await fragment_progress_msg.delete()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(
                f"Writing fragment extraction failed for campaign {campaign.id}: {e}"
            )

        # Step 3: Extract literary style profiles.
        literary_profiles: Dict[str, dict] = {}
        try:
            literary_profiles = await cls._analyze_literary_style(
                raw_text,
                document_key,
                campaign=campaign,
                channel_id=getattr(channel, "id", None),
            )
            if literary_profiles:
                profile_keys = ", ".join(f"`{k}`" for k in sorted(literary_profiles.keys()))
                result_msg += f" Literary style profile(s) extracted: {profile_keys}."
        except Exception as e:
            logger.warning(
                f"Literary style extraction failed for campaign {campaign.id}: {e}"
            )

        final_msg = (
            f"Source material processed: `{resolved_label}` as key `{document_key}` "
            f"(story format, ~{total_tokens} tokens).{result_msg}"
        )
        if status_msg is not None:
            try:
                await status_msg.edit(content=final_msg)
                await asyncio.sleep(4)
                await status_msg.delete()
            except Exception:
                pass
        return True, final_msg, literary_profiles

    @classmethod
    def _source_material_prompt_payload(cls, campaign_id: int) -> Dict[str, object]:
        docs = ZorkMemory.list_source_material_documents(
            campaign_id,
            limit=cls.SOURCE_MATERIAL_MAX_DOCS_IN_PROMPT,
        )
        total_chunk_count = 0
        compact_docs = []
        source_keys: List[Dict[str, object]] = []
        for row in docs:
            try:
                chunk_count = int(row.get("chunk_count") or 0)
            except (TypeError, ValueError):
                chunk_count = 0
            total_chunk_count += chunk_count
            document_key = str(row.get("document_key") or "")
            source_format = cls._source_material_format_heuristic(
                str(row.get("sample_chunk") or "")
            )
            compact_docs.append(
                {
                    "document_key": document_key,
                    "document_label": str(row.get("document_label") or ""),
                    "chunk_count": chunk_count,
                    "format": source_format,
                }
            )
            document_keys: List[str] = []
            if document_key and source_format == "rulebook":
                units = ZorkMemory.get_source_material_document_units(
                    campaign_id,
                    document_key,
                )
                seen_keys: set[str] = set()
                for unit in units:
                    text = str(unit or "").strip()
                    if not text or ":" not in text:
                        continue
                    key = text.split(":", 1)[0].strip()
                    if not key or key in seen_keys:
                        continue
                    seen_keys.add(key)
                    document_keys.append(key)
            source_keys.append(
                {
                    "document_key": document_key,
                    "document_label": str(row.get("document_label") or ""),
                    "format": source_format,
                    "keys": document_keys,
                }
            )
        communication_key = cls.communication_rulebook_document_key()
        rulebook_document_keys = {
            str(doc.get("document_key") or "").strip()
            for doc in compact_docs
            if str(doc.get("format") or "").strip().lower()
            == cls.SOURCE_MATERIAL_FORMAT_RULEBOOK
        }
        digests = {
            str(doc_key): digest_text
            for doc_key, digest_text in (
                ZorkMemory.get_all_source_material_digests(campaign_id) or {}
            ).items()
            if (
                ZorkMemory._normalize_source_document_key(str(doc_key or ""))
                != communication_key
                and str(doc_key or "").strip() not in rulebook_document_keys
            )
        }
        return {
            "available": bool(compact_docs),
            "document_count": len(compact_docs),
            "chunk_count": total_chunk_count,
            "docs": compact_docs,
            "keys": source_keys,
            "digests": digests,
        }

    @classmethod
    def campaign_rulebook_document_key(cls) -> str:
        return ZorkMemory._normalize_source_document_key(cls.AUTO_RULEBOOK_DOCUMENT_LABEL)

    @classmethod
    def list_campaign_rules(cls, campaign_id: int) -> List[Dict[str, str]]:
        return ZorkMemory.list_rulebook_entries(
            campaign_id,
            cls.campaign_rulebook_document_key(),
        )

    @classmethod
    def get_campaign_rule(
        cls,
        campaign_id: int,
        rule_key: str,
    ) -> Optional[Dict[str, str]]:
        return ZorkMemory.get_rulebook_entry(
            campaign_id,
            cls.campaign_rulebook_document_key(),
            rule_key,
        )

    @classmethod
    def put_campaign_rule(
        cls,
        campaign_id: int,
        *,
        rule_key: str,
        rule_text: str,
        upsert: bool = False,
    ) -> Dict[str, object]:
        return ZorkMemory.put_rulebook_entry(
            campaign_id,
            document_label=cls.AUTO_RULEBOOK_DOCUMENT_LABEL,
            rule_key=rule_key,
            rule_text=rule_text,
            replace_existing=bool(upsert),
        )

    # ── End Campaign Setup ─────────────────────────────────────────────

    @classmethod
    def _trim_text(cls, text: str, max_chars: int) -> str:
        if text is None:
            return ""
        if len(text) <= max_chars:
            return text
        return text[:max_chars]

    @classmethod
    def _trim_text_tail(cls, text: str, max_chars: int) -> str:
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
            return cls._trim_text_tail(update, cls.MAX_SUMMARY_CHARS)
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
        return cls._trim_text_tail(merged, cls.MAX_SUMMARY_CHARS)

    @classmethod
    def _summary_has_durable_keywords(cls, text: str) -> bool:
        text_l = str(text or "").lower()
        durable_tokens = (
            "killed",
            "died",
            "dead",
            "injured",
            "wounded",
            "captured",
            "escaped",
            "revealed",
            "discovered",
            "agreed",
            "betrayed",
            "joined",
            "left",
            "arrived",
            "departed",
            "consumed",
            "lost",
            "gained",
            "acquired",
            "stole",
            "destroyed",
            "completed",
            "resolved",
            "overdue",
            "appointment",
            "deadline",
            "timer",
            "calendar",
        )
        return any(token in text_l for token in durable_tokens)

    @classmethod
    def _summary_is_transient_action(cls, text: str) -> bool:
        text_l = " ".join(str(text or "").strip().lower().split())
        if not text_l:
            return True
        transient_re = re.compile(
            r"^(?:[a-z0-9' <>()@-]{2,80})\s+"
            r"(?:readies?|approaches?|looks?|glances?|stands?|walks?|moves?|heads?|"
            r"goes?|waits?|turns?|opens?|closes?|draws?|steps?|continues?)\b"
        )
        return bool(transient_re.match(text_l))

    @classmethod
    def _structured_change_looks_durable(
        cls,
        state_update: object,
        player_state_update: object,
        character_updates: object,
        calendar_update: object,
    ) -> bool:
        if isinstance(calendar_update, dict) and calendar_update:
            return True
        if isinstance(character_updates, dict) and character_updates:
            return True
        if isinstance(state_update, dict) and state_update:
            meaningful_state_keys = {
                str(k)
                for k in state_update.keys()
                if str(k) not in {"game_time", "current_chapter", "current_scene"}
            }
            if meaningful_state_keys:
                return True
        if isinstance(player_state_update, dict) and player_state_update:
            durable_player_keys = {
                "location",
                "inventory_add",
                "inventory_remove",
                "hp",
                "conditions",
                "status",
                "party_status",
                "character_name",
            }
            if any(str(k) in durable_player_keys for k in player_state_update.keys()):
                return True
        return False

    @classmethod
    def _should_keep_summary_update(
        cls,
        summary_text: str,
        *,
        state_update: object,
        player_state_update: object,
        character_updates: object,
        calendar_update: object,
    ) -> bool:
        text = " ".join(str(summary_text or "").strip().split())
        if not text:
            return False
        if cls._summary_has_durable_keywords(text):
            return True
        if cls._structured_change_looks_durable(
            state_update,
            player_state_update,
            character_updates,
            calendar_update,
        ):
            return True
        if cls._summary_is_transient_action(text):
            return False
        # Keep concise informational lines when they are not obviously transient.
        return len(text.split()) >= 6

    @classmethod
    def _compose_world_summary(
        cls,
        campaign: ZorkCampaign,
        campaign_state: Dict[str, object],
        *,
        turns: Optional[List[ZorkTurn]] = None,
        viewer_user_id: Optional[int] = None,
        viewer_slug: str = "",
        viewer_location_key: str = "",
        viewer_private_context_key: str = "",
        max_chars: Optional[int] = None,
    ) -> str:
        raw_summary = cls._strip_inventory_mentions(campaign.summary or "")
        seen: set[str] = set()
        persisted_lines: List[str] = []
        recent_lines: List[str] = []

        def _append_if_relevant(target: List[str], raw_text: object) -> None:
            line = " ".join(str(raw_text or "").strip().split())
            if not line:
                return
            line_key = line.lower()
            if line_key in seen:
                return
            if not cls._should_keep_summary_update(
                line,
                state_update={},
                player_state_update={},
                character_updates={},
                calendar_update=None,
            ):
                return
            seen.add(line_key)
            target.append(line)

        for raw_line in raw_summary.splitlines():
            _append_if_relevant(persisted_lines, raw_line)

        if (
            isinstance(turns, list)
            and viewer_user_id is not None
            and turns
        ):
            for turn in turns[-24:]:
                if not isinstance(turn, ZorkTurn):
                    continue
                if turn.kind != "narrator":
                    continue
                if not cls._turn_visible_to_viewer(
                    turn,
                    viewer_user_id,
                    viewer_slug,
                    viewer_location_key,
                    viewer_private_context_key,
                ):
                    continue
                meta = cls._safe_turn_meta(turn)
                if bool(meta.get("suppress_context")):
                    continue
                summary_candidate = meta.get("summary_update")
                if not str(summary_candidate or "").strip():
                    summary_candidate = cls._brief_event_summary(
                        action_text="",
                        summary_update=None,
                        narration_text=meta.get("scene_output_rendered")
                        or turn.content
                        or "",
                    )
                _append_if_relevant(recent_lines, summary_candidate)

        lines = persisted_lines + recent_lines

        if not lines:
            active_chapters = cls._chapters_for_prompt(
                campaign_state if isinstance(campaign_state, dict) else {},
                active_only=True,
                limit=1,
            )
            if active_chapters:
                title = str(active_chapters[0].get("title") or "").strip()
                summary = " ".join(
                    str(active_chapters[0].get("summary") or "").strip().split()
                )
                fallback = f"{title}: {summary}".strip(": ").strip()
                if fallback:
                    lines.append(fallback)

        if max_chars is None:
            max_chars = cls.MAX_SUMMARY_CHARS
        if not lines:
            return ""

        # Compose WORLD_SUMMARY as a chronological rolling window rather than
        # prefixing recent lines and hard-trimming characters. When we exceed the
        # budget, drop the oldest lines first so the summary behaves like FIFO.
        summary_lines = list(lines)
        while len(summary_lines) > 1 and len("\n".join(summary_lines)) > max_chars:
            summary_lines.pop(0)

        composed = "\n".join(summary_lines)
        if len(composed) > max_chars:
            composed = cls._trim_text(composed, max_chars)
        return composed

    @classmethod
    def _scene_output_is_summary_public_safe(
        cls,
        scene_output: object,
    ) -> bool:
        """Return True unless the scene contains private/limited beats that should not leak into WORLD_SUMMARY."""
        if not isinstance(scene_output, dict):
            return True
        beats = scene_output.get("beats")
        if not isinstance(beats, list) or not beats:
            return True
        for beat in beats:
            if not isinstance(beat, dict):
                continue
            scope = str(beat.get("visibility") or "").strip().lower() or "local"
            if scope in {"private", "limited"}:
                return False
        return True

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
        if not bool(campaign_state.get("on_rails", False)):
            active_chapters = cls._chapters_for_prompt(
                campaign_state,
                active_only=True,
                limit=4,
            )
            if active_chapters:
                def _chapter_scene_label(value: object) -> str:
                    text = str(value or "").strip()
                    if not text:
                        return "Untitled"
                    text = text.replace("_", "-")
                    parts = [part for part in text.split("-") if part]
                    if not parts:
                        return "Untitled"
                    return " ".join(part.capitalize() for part in parts)[:120]

                current = active_chapters[0]
                lines: List[str] = []
                lines.append(f"CURRENT CHAPTER: {current.get('title', 'Untitled')}")
                lines.append(f"  Summary: {current.get('summary', '')}")
                scenes = current.get("scenes") or []
                current_scene_slug = str(current.get("current_scene") or "").strip()
                if isinstance(scenes, list):
                    for i, scene in enumerate(scenes):
                        scene_slug = str(scene or "").strip()
                        marker = (
                            " >>> CURRENT SCENE <<<"
                            if scene_slug and scene_slug == current_scene_slug
                            else ""
                        )
                        lines.append(
                            f"  Scene {i + 1}: {_chapter_scene_label(scene_slug)}{marker}"
                        )
                if len(active_chapters) > 1:
                    lines.append("")
                    for idx, row in enumerate(active_chapters[1:4], start=1):
                        label = "NEXT CHAPTER" if idx == 1 else f"UPCOMING CHAPTER {idx}"
                        lines.append(f"{label}: {row.get('title', 'Untitled')}")
                        summary = str(row.get("summary") or "").strip()
                        if summary:
                            lines.append(f"  Preview: {summary[:320]}")
                        row_scenes = row.get("scenes") or []
                        if isinstance(row_scenes, list) and row_scenes:
                            preview_titles = [
                                _chapter_scene_label(scene)
                                for scene in row_scenes[:3]
                                if str(scene or "").strip()
                            ]
                            if preview_titles:
                                lines.append(f"  Early scenes: {', '.join(preview_titles)}")
                        lines.append("")
                while lines and not lines[-1]:
                    lines.pop()
                return "\n".join(lines) if lines else None

        outline = campaign_state.get("story_outline")
        if not isinstance(outline, dict):
            return None
        chapters = outline.get("chapters")
        if not isinstance(chapters, list) or not chapters:
            return None

        current_ch = cls._coerce_non_negative_int(
            campaign_state.get("current_chapter", 0), default=0
        )
        current_sc = cls._coerce_non_negative_int(
            campaign_state.get("current_scene", 0), default=0
        )
        current_ch = min(current_ch, max(len(chapters) - 1, 0))

        def _preview(value: object, max_chars: int = 320) -> str:
            text = str(value or "").strip()
            if len(text) <= max_chars:
                return text
            clipped = text[:max_chars].rsplit(" ", 1)[0].strip()
            if not clipped:
                clipped = text[:max_chars].strip()
            return f"{clipped}..."

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

        # Next 3 chapters (preview)
        for offset in range(1, 4):
            idx = current_ch + offset
            if idx >= len(chapters):
                break
            nxt = chapters[idx]
            label = "NEXT CHAPTER" if offset == 1 else f"UPCOMING CHAPTER {offset}"
            lines.append(f"{label}: {nxt.get('title', 'Untitled')}")
            preview = _preview(nxt.get("summary", ""))
            if preview:
                lines.append(f"  Preview: {preview}")
            nxt_scenes = nxt.get("scenes")
            if isinstance(nxt_scenes, list):
                titles = []
                for scene in nxt_scenes[:3]:
                    if not isinstance(scene, dict):
                        continue
                    title = str(scene.get("title", "Untitled")).strip() or "Untitled"
                    titles.append(title)
                if titles:
                    lines.append(f"  Early scenes: {', '.join(titles)}")
            lines.append("")

        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines) if lines else None

    @classmethod
    def _auto_advance_on_rails_story_context(
        cls,
        campaign_state: Dict[str, object],
        *,
        action_text: str,
        narration: str,
        summary_update: object,
        state_update: Dict[str, object],
        player_state_update: Dict[str, object],
        character_updates: Dict[str, object],
        calendar_update: object,
    ) -> bool:
        if not bool(campaign_state.get("on_rails", False)):
            return False
        outline = campaign_state.get("story_outline")
        if not isinstance(outline, dict):
            return False
        chapters = outline.get("chapters")
        if not isinstance(chapters, list) or not chapters:
            return False
        if not isinstance(state_update, dict):
            return False
        if "current_chapter" in state_update or "current_scene" in state_update:
            return False

        action_clean = " ".join(str(action_text or "").strip().lower().split())
        if not action_clean:
            return False
        if action_clean in {
            "look",
            "l",
            "inventory",
            "inv",
            "i",
            "calendar",
            "cal",
            "events",
            "roster",
            "characters",
            "npcs",
        }:
            return False
        if action_clean.startswith("[ooc"):
            return False

        if cls._is_emptyish_turn_payload(
            narration=narration,
            state_update=state_update,
            player_state_update=player_state_update if isinstance(player_state_update, dict) else {},
            summary_update=summary_update,
            xp_awarded=0,
            scene_image_prompt=None,
            character_updates=character_updates if isinstance(character_updates, dict) else {},
            calendar_update=calendar_update,
        ):
            return False

        old_ch = cls._coerce_non_negative_int(
            campaign_state.get("current_chapter", 0), default=0
        )
        old_sc = cls._coerce_non_negative_int(
            campaign_state.get("current_scene", 0), default=0
        )
        old_ch = min(old_ch, len(chapters) - 1)
        current_entry = chapters[old_ch] if 0 <= old_ch < len(chapters) else {}
        scenes = current_entry.get("scenes")
        if not isinstance(scenes, list):
            scenes = []

        looks_major = cls._looks_like_major_narrative_beat(
            narration=narration,
            summary_update=summary_update,
            state_update=state_update,
            character_updates=character_updates if isinstance(character_updates, dict) else {},
            calendar_update=calendar_update,
        )
        has_player_motion = bool(
            isinstance(player_state_update, dict)
            and any(
                key in player_state_update
                for key in ("location", "room_title", "room_summary", "room_description")
            )
        )
        has_scene_signal = bool(str(summary_update or "").strip()) or has_player_motion or looks_major
        if not has_scene_signal:
            return False

        new_ch = old_ch
        new_sc = old_sc
        if scenes and old_sc + 1 < len(scenes):
            new_sc = old_sc + 1
        elif old_ch + 1 < len(chapters):
            new_ch = old_ch + 1
            new_sc = 0
            if isinstance(current_entry, dict):
                current_entry["completed"] = True
        else:
            return False

        campaign_state["current_chapter"] = new_ch
        campaign_state["current_scene"] = new_sc
        return True

    @classmethod
    def _normalize_story_progression(cls, value: object) -> Optional[Dict[str, object]]:
        if not isinstance(value, dict):
            return None
        target = " ".join(str(value.get("target") or "").strip().lower().split())
        target = target.replace("_", "-")
        allowed_targets = {"hold", "next-scene", "next-chapter"}
        if target not in allowed_targets:
            target = "hold"
        advance_raw = value.get("advance")
        if isinstance(advance_raw, bool):
            advance = advance_raw
        else:
            advance_text = " ".join(str(advance_raw or "").strip().lower().split())
            advance = advance_text in {"1", "true", "yes", "y", "advance"}
        if target == "hold":
            advance = False
        reason = " ".join(str(value.get("reason") or "").strip().split())[:300]
        return {
            "advance": advance,
            "target": target,
            "reason": reason,
        }

    @classmethod
    def _apply_story_progression_hint(
        cls,
        campaign_state: Dict[str, object],
        story_progression: Optional[Dict[str, object]],
        state_update: Dict[str, object],
    ) -> bool:
        if not bool(campaign_state.get("on_rails", False)):
            return False
        if not isinstance(state_update, dict):
            return False
        if "current_chapter" in state_update or "current_scene" in state_update:
            return False
        if not isinstance(story_progression, dict):
            return False
        if not bool(story_progression.get("advance")):
            return False

        outline = campaign_state.get("story_outline")
        if not isinstance(outline, dict):
            return False
        chapters = outline.get("chapters")
        if not isinstance(chapters, list) or not chapters:
            return False

        old_ch = cls._coerce_non_negative_int(
            campaign_state.get("current_chapter", 0), default=0
        )
        old_sc = cls._coerce_non_negative_int(
            campaign_state.get("current_scene", 0), default=0
        )
        old_ch = min(old_ch, len(chapters) - 1)
        current_entry = chapters[old_ch] if 0 <= old_ch < len(chapters) else {}
        scenes = current_entry.get("scenes")
        if not isinstance(scenes, list):
            scenes = []

        target = str(story_progression.get("target") or "hold")
        new_ch = old_ch
        new_sc = old_sc
        if target == "next-chapter":
            if old_ch + 1 >= len(chapters):
                return False
            new_ch = old_ch + 1
            new_sc = 0
            if isinstance(current_entry, dict):
                current_entry["completed"] = True
        elif target == "next-scene":
            if scenes and old_sc + 1 < len(scenes):
                new_sc = old_sc + 1
            elif old_ch + 1 < len(chapters):
                new_ch = old_ch + 1
                new_sc = 0
                if isinstance(current_entry, dict):
                    current_entry["completed"] = True
            else:
                return False
        else:
            return False

        campaign_state["current_chapter"] = new_ch
        campaign_state["current_scene"] = new_sc
        return True

    @classmethod
    def _sync_game_time_to_player_state(
        cls,
        state_update: Dict[str, object],
        player_state_update: Dict[str, object],
    ) -> Dict[str, object]:
        """Copy game_time from state_update into player_state_update.

        The model places game_time in state_update (campaign-wide), but the
        player card is built from player_state which is only populated via
        player_state_update.  Without this bridge the player card's game_time
        goes stale.
        """
        if not isinstance(state_update, dict) or not isinstance(player_state_update, dict):
            return player_state_update
        gt = state_update.get("game_time")
        if isinstance(gt, dict) and gt:
            player_state_update.setdefault("game_time", gt)
        return player_state_update

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
    def _rescue_misplaced_room_state(
        cls,
        state_update: Dict[str, object],
        player_state_update: Dict[str, object],
    ) -> Tuple[Dict[str, object], Dict[str, object]]:
        """Move room/location keys out of nested state_update dicts into player_state_update.

        The model sometimes nests player room data inside state_update
        (e.g. state_update.character_update.room_title) instead of using the
        top-level player_state_update key.  This method scans every value in
        state_update that is itself a dict and, when it finds 2+ ROOM_STATE_KEYS,
        moves those keys into player_state_update (only if player_state_update
        doesn't already contain room data).  The 2-key minimum prevents false
        positives from an NPC that merely has a ``location`` field.
        """
        if not isinstance(state_update, dict):
            return state_update, player_state_update
        if not isinstance(player_state_update, dict):
            player_state_update = {}

        # If player_state_update already has room data, don't overwrite it.
        existing_room_keys = cls.ROOM_STATE_KEYS & set(player_state_update)
        if len(existing_room_keys) >= 2:
            return state_update, player_state_update

        keys_to_remove = []
        for key, value in state_update.items():
            if not isinstance(value, dict):
                continue
            nested_room_keys = cls.ROOM_STATE_KEYS & set(value)
            if len(nested_room_keys) < 2:
                continue
            # Found misplaced room data — move it.
            for rk in nested_room_keys:
                player_state_update[rk] = value.pop(rk)
            _zork_log(
                "RESCUED MISPLACED ROOM STATE",
                f"Moved {sorted(nested_room_keys)} from state_update.{key} "
                f"into player_state_update",
            )
            # If the nested dict is now empty, mark for cleanup.
            if not value:
                keys_to_remove.append(key)

        for key in keys_to_remove:
            del state_update[key]

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

    @staticmethod
    def _normalize_location_key(value: object) -> str:
        """Collapse a location key to a canonical alphanumeric form.

        Both human-readable names (``"Nothing's Edge Approach"``) and
        slug-style keys (``"nothing-edge-approach"``) normalise to the
        same string (``"nothingsedgeapproach"``), so they compare equal
        regardless of which format was used at storage time.
        """
        if value is None:
            return ""
        text = re.sub(r"[^a-z0-9]", "", str(value).strip().lower())
        return text

    @classmethod
    def _room_key_from_player_state(cls, player_state: Dict[str, object]) -> str:
        if not isinstance(player_state, dict):
            return "unknown-room"
        for key in ("location_key", "room_id", "location", "room_title", "room_summary"):
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
                "updated": cls._format_utc_timestamp(datetime.datetime.now(datetime.timezone.utc)),
                "prompt": (scene_prompt or "").strip(),
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
            cls._format_utc_timestamp(datetime.datetime.now(datetime.timezone.utc))
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
        return prompt

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
        return prompt

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

        # Location is the primary key — if both players share the same
        # location string they are in the same scene.
        if actor_location and other_location and actor_location == other_location:
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
            is_actor = entry.user_id == actor.user_id
            same_scene = is_actor or cls._same_scene(actor_state, state)
            fallback_name = f"Adventurer-{str(entry.user_id)[-4:]}"
            display_name = str(state.get("character_name") or fallback_name).strip()
            player_slug = cls._player_slug_key(display_name) or f"player-{entry.user_id}"
            persona = str(state.get("persona") or "").strip()
            if persona:
                persona = cls._trim_text(persona, cls.MAX_PERSONA_PROMPT_CHARS)
                persona = " ".join(persona.split()[:18])
            attributes = cls.get_player_attributes(entry)
            attribute_cues = cls._build_attribute_cues(attributes)
            visible_items = []
            if is_actor:
                visible_items = cls._normalize_inventory_items(state.get("inventory"))[
                    :3
                ]
            out.append(
                {
                    "user_id": entry.user_id,
                    "discord_mention": f"<@{entry.user_id}>",
                    "name": display_name,
                    "player_slug": player_slug,
                    "is_actor": is_actor,
                    "same_scene": same_scene,
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
                if "room_summary" not in cleaned:
                    cleaned["room_summary"] = None

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
    def _strip_ephemeral_context_lines(cls, text: str) -> str:
        if not text:
            return ""
        kept_lines = []
        for line in str(text).splitlines():
            stripped = line.strip().lower()
            if any(stripped.startswith(p) for p in cls._INVENTORY_LINE_PREFIXES):
                continue
            if any(stripped.startswith(p) for p in cls.UNREAD_SMS_LINE_PREFIXES):
                continue
            if stripped.startswith("\u23f0"):
                continue
            kept_lines.append(line)
        return "\n".join(kept_lines).strip()

    @classmethod
    def _strip_inventory_mentions(cls, text: str) -> str:
        if not text:
            return ""
        return cls._strip_ephemeral_context_lines(text)

    @classmethod
    def _strip_prompt_artifacts(cls, text: str) -> str:
        if not text:
            return ""
        cleaned = re.sub(
            r"\[SYSTEM NOTE: FOR THIS RESPONSE ONLY:.*?\]",
            "",
            str(text),
            flags=re.IGNORECASE | re.DOTALL,
        )
        kept_lines = []
        skipping_attachment = False
        for line in cleaned.splitlines():
            stripped = line.strip()
            upper = stripped.upper()
            if upper.startswith("TURN_ATTACHMENT_CONTEXT:"):
                skipping_attachment = True
                continue
            if skipping_attachment:
                if not stripped:
                    skipping_attachment = False
                continue
            if upper.startswith("PLAYER_ACTION"):
                continue
            kept_lines.append(line)
        cleaned = "\n".join(kept_lines)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    @classmethod
    def _set_turn_ephemeral_notices(
        cls,
        campaign_id: int,
        user_id: int,
        notices: List[str],
    ) -> None:
        cleaned = []
        seen = set()
        for item in notices or []:
            text = " ".join(str(item or "").split()).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            cleaned.append(text[:500])
        key = (int(campaign_id), int(user_id))
        if cleaned:
            cls._turn_ephemeral_notices[key] = cleaned
        else:
            cls._turn_ephemeral_notices.pop(key, None)

    @classmethod
    def pop_turn_ephemeral_notices(
        cls,
        campaign_id: int,
        user_id: int,
    ) -> List[str]:
        return list(cls._turn_ephemeral_notices.pop((int(campaign_id), int(user_id)), []))

    @staticmethod
    def _private_context_key(*parts: object) -> str:
        cleaned = []
        for part in parts:
            text = re.sub(r"[^a-z0-9]+", "-", str(part or "").strip().lower()).strip("-")
            if text:
                cleaned.append(text[:80])
        return ":".join(cleaned)[:240]

    @classmethod
    def _active_private_context_from_state(
        cls, player_state: Dict[str, object]
    ) -> Optional[Dict[str, object]]:
        if not isinstance(player_state, dict):
            return None
        raw = player_state.get(cls.PRIVATE_CONTEXT_STATE_KEY)
        if not isinstance(raw, dict):
            return None
        context_key = str(raw.get("context_key") or "").strip()
        scope = str(raw.get("scope") or "").strip().lower()
        if not context_key or scope not in {"private", "limited"}:
            return None
        out = dict(raw)
        out["context_key"] = context_key
        out["scope"] = scope
        return out

    @classmethod
    def _action_leaves_private_context(
        cls,
        action: str,
        active_context: Optional[Dict[str, object]],
    ) -> bool:
        text = " ".join(str(action or "").strip().lower().split())
        if not text or not active_context:
            return False
        if cls._is_private_phone_command_line(text):
            return False
        if re.search(
            r"\b(?:go|walk|head|return|leave|exit|join|approach|cross|back to|turn back to|out loud|to everyone|announce)\b",
            text,
            re.IGNORECASE,
        ):
            return True
        target_name = str(active_context.get("target_name") or "").strip().lower()
        if target_name and target_name not in text and re.search(r"\b(?:ask|tell|say|talk)\b", text, re.IGNORECASE):
            return True
        return False

    @classmethod
    def _is_private_engagement_setup_action(cls, action: str) -> bool:
        text = " ".join(str(action or "").strip().lower().split())
        if not text:
            return False
        if cls._is_private_phone_command_line(text):
            return False
        return bool(
            re.search(
                r"\b(?:whisper|murmur|lean in|lower my voice|lower your voice|quietly to|under my breath|private word|pull .* aside|take .* aside|step aside with)\b",
                text,
                re.IGNORECASE,
            )
        )

    @classmethod
    def _private_setup_warning_needed(cls, action: str) -> bool:
        if not cls._is_private_engagement_setup_action(action):
            return False
        text = str(action or "").strip()
        sentence_count = len(
            [seg for seg in re.split(r"(?<=[.!?])\s+", text) if seg.strip()]
        )
        if sentence_count > 1:
            return True
        if text.count('"') >= 2 or text.count("'") >= 2:
            return True
        return len(text) > 180

    @classmethod
    def _resolve_private_context_target(
        cls,
        campaign: ZorkCampaign,
        actor: Optional[ZorkPlayer],
        action: str,
    ) -> Optional[Dict[str, object]]:
        text = str(action or "")
        if not text:
            return None
        actor_user_id = getattr(actor, "user_id", None)
        mention_match = re.search(r"<@!?(\d+)>", text)
        registry = cls._campaign_player_registry(campaign.id)
        by_user_id = registry.get("by_user_id", {})
        if mention_match:
            try:
                target_user_id = int(mention_match.group(1))
            except (TypeError, ValueError):
                target_user_id = None
            if target_user_id and target_user_id != actor_user_id:
                entry = by_user_id.get(target_user_id)
                if entry is not None:
                    return {
                        "kind": "player",
                        "target_user_id": target_user_id,
                        "target_slug": str(entry.get("slug") or "").strip(),
                        "target_name": str(entry.get("name") or "").strip(),
                    }
        text_norm = cls._normalize_match_text(text)
        for user_id, entry in by_user_id.items():
            if actor_user_id is not None and int(user_id) == int(actor_user_id):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            if cls._normalize_match_text(name) in text_norm:
                return {
                    "kind": "player",
                    "target_user_id": int(user_id),
                    "target_slug": str(entry.get("slug") or "").strip(),
                    "target_name": name,
                }
        characters = cls.get_campaign_characters(campaign)
        if isinstance(characters, dict):
            for slug, payload in characters.items():
                if not isinstance(payload, dict):
                    continue
                name = str(payload.get("name") or "").strip()
                candidates = [str(slug or "").strip(), name]
                for candidate in candidates:
                    candidate_norm = cls._normalize_match_text(candidate)
                    if candidate_norm and candidate_norm in text_norm:
                        return {
                            "kind": "npc",
                            "target_slug": str(slug or "").strip(),
                            "target_name": name or str(slug or "").strip(),
                        }
        return None

    @classmethod
    def _derive_private_context_candidate(
        cls,
        campaign: ZorkCampaign,
        actor: Optional[ZorkPlayer],
        player_state: Dict[str, object],
        action: str,
    ) -> Optional[Dict[str, object]]:
        active_context = cls._active_private_context_from_state(player_state)
        if cls._action_leaves_private_context(action, active_context):
            return None
        actor_slug = cls._player_slug_key(player_state.get("character_name")) or (
            f"player-{getattr(actor, 'user_id', '')}" if actor is not None else ""
        )
        location_key = cls._room_key_from_player_state(player_state)
        # Continue an already-active private context (managed by explicit
        # player commands — slash commands or reactions).
        if active_context:
            carried = dict(active_context)
            carried["engagement"] = "continue"
            return carried
        # New private contexts are only started via explicit player commands
        # (slash command / reaction), not auto-detected from narration text.
        return None

    @classmethod
    def _apply_private_context_candidate(
        cls,
        turn_visibility: Dict[str, object],
        candidate: Optional[Dict[str, object]],
    ) -> Dict[str, object]:
        if not candidate:
            return turn_visibility
        merged = dict(turn_visibility or {})
        merged["scope"] = str(candidate.get("scope") or merged.get("scope") or "private")
        merged["context_key"] = str(candidate.get("context_key") or "").strip() or None
        location_key = str(candidate.get("location_key") or "").strip()
        if location_key:
            merged["location_key"] = location_key
        reason = " ".join(str(merged.get("reason") or "").split()).strip()
        if not reason:
            target_name = str(candidate.get("target_name") or "").strip()
            if target_name:
                merged["reason"] = f"Private exchange with {target_name}"
            else:
                merged["reason"] = "Private exchange"
        if merged.get("scope") == "limited":
            visible_slugs = list(merged.get("visible_player_slugs") or [])
            target_slug = str(candidate.get("target_slug") or "").strip()
            if target_slug and target_slug not in visible_slugs:
                visible_slugs.append(target_slug)
            merged["visible_player_slugs"] = visible_slugs
            visible_user_ids = list(merged.get("visible_user_ids") or [])
            target_user_id = candidate.get("target_user_id")
            if isinstance(target_user_id, int) and target_user_id not in visible_user_ids:
                visible_user_ids.append(target_user_id)
            merged["visible_user_ids"] = visible_user_ids
        return merged

    @classmethod
    def _persist_private_context_state(
        cls,
        player_state: Dict[str, object],
        turn_visibility: Dict[str, object],
        action: str,
        candidate: Optional[Dict[str, object]],
    ) -> None:
        if not isinstance(player_state, dict):
            return
        active_context = cls._active_private_context_from_state(player_state)
        scope = str(turn_visibility.get("scope") or "").strip().lower()
        context_key = str(turn_visibility.get("context_key") or "").strip()
        if context_key and scope in {"private", "limited"}:
            payload = {
                "scope": scope,
                "context_key": context_key,
                "location_key": str(turn_visibility.get("location_key") or "").strip() or None,
                "target_name": str((candidate or {}).get("target_name") or (active_context or {}).get("target_name") or "").strip() or None,
                "target_slug": str((candidate or {}).get("target_slug") or (active_context or {}).get("target_slug") or "").strip() or None,
            }
            player_state[cls.PRIVATE_CONTEXT_STATE_KEY] = payload
            return
        if cls._action_leaves_private_context(action, active_context) or scope in {"public", "local"}:
            player_state.pop(cls.PRIVATE_CONTEXT_STATE_KEY, None)

    @classmethod
    def _infer_aware_npc_slugs(
        cls,
        campaign: ZorkCampaign,
        player_state: Dict[str, object],
        turn_visibility: Dict[str, object],
        *,
        narration_text: str = "",
        summary_update: object = None,
        private_context_candidate: Optional[Dict[str, object]] = None,
    ) -> List[str]:
        seen: set[str] = set()
        out: List[str] = []

        def _add(slug: object) -> None:
            text = str(slug or "").strip()
            if not text or text in seen:
                return
            seen.add(text)
            out.append(text)

        raw_existing = turn_visibility.get("aware_npc_slugs")
        if isinstance(raw_existing, list):
            for item in raw_existing:
                _add(item)
        if out:
            return out

        candidate_slug = str((private_context_candidate or {}).get("target_slug") or "").strip()
        if candidate_slug:
            _add(candidate_slug)
        if out:
            return out

        combined_text = cls._normalize_match_text(
            f"{str(narration_text or '')}\n{str(summary_update or '')}"
        )
        characters = cls.get_campaign_characters(campaign)
        same_scene_slugs: List[str] = []
        if isinstance(characters, dict):
            for slug, payload in characters.items():
                if not isinstance(payload, dict) or payload.get("deceased_reason"):
                    continue
                char_name = str(payload.get("name") or slug or "").strip()
                char_state = {
                    "location": payload.get("location"),
                    "room_title": payload.get("room_title"),
                    "room_summary": payload.get("room_summary"),
                    "room_id": payload.get("room_id"),
                }
                if cls._same_scene(player_state, char_state):
                    same_scene_slugs.append(str(slug))
                if combined_text:
                    for candidate in (slug, char_name):
                        candidate_norm = cls._normalize_match_text(candidate)
                        if candidate_norm and candidate_norm in combined_text:
                            _add(slug)
                            break
        if out:
            return out
        if str(turn_visibility.get("scope") or "").strip().lower() in {"private", "limited"} and len(same_scene_slugs) == 1:
            _add(same_scene_slugs[0])
        return out

    @classmethod
    def _filter_aware_npc_slugs_for_scene(
        cls,
        campaign: ZorkCampaign,
        player_state: Dict[str, object],
        aware_npc_slugs: object,
    ) -> List[str]:
        if not isinstance(aware_npc_slugs, list):
            return []
        characters = cls.get_campaign_characters(campaign)
        if not isinstance(characters, dict):
            return []
        out: List[str] = []
        seen: set[str] = set()
        for item in aware_npc_slugs:
            candidate = str(item or "").strip()
            if not candidate:
                continue
            resolved_slug = cls._resolve_existing_character_slug(characters, candidate)
            if not resolved_slug or resolved_slug in seen:
                continue
            payload = characters.get(resolved_slug)
            if not isinstance(payload, dict) or payload.get("deceased_reason"):
                continue
            char_state = {
                "location": payload.get("location"),
                "room_title": payload.get("room_title"),
                "room_summary": payload.get("room_summary"),
                "room_id": payload.get("room_id"),
            }
            if not cls._same_scene(player_state, char_state):
                continue
            seen.add(resolved_slug)
            out.append(resolved_slug)
        return out

    @classmethod
    def _filter_aware_npc_slugs_for_location_key(
        cls,
        campaign: ZorkCampaign,
        location_key: object,
        aware_npc_slugs: object,
    ) -> List[str]:
        location_text = str(location_key or "").strip()
        if not location_text:
            return []
        return cls._filter_aware_npc_slugs_for_scene(
            campaign,
            {"location": location_text},
            aware_npc_slugs,
        )

    @classmethod
    def _sanitize_turn_awareness_for_scene(
        cls,
        campaign: ZorkCampaign,
        player_state: Dict[str, object],
        turn_visibility: Dict[str, object],
    ) -> Dict[str, object]:
        if not isinstance(turn_visibility, dict):
            return turn_visibility
        filtered = cls._filter_aware_npc_slugs_for_scene(
            campaign,
            player_state,
            turn_visibility.get("aware_npc_slugs"),
        )
        result = dict(turn_visibility)
        result["aware_npc_slugs"] = filtered
        return result

    _REASONING_PREFIXES = re.compile(
        r"^(I need to |I should |I'll |Let me |I want to |I will |First,? I |"
        r"Now I |My plan |Step \d|To respond|Before I |I must )",
        re.IGNORECASE,
    )

    @classmethod
    def _looks_like_reasoning(cls, text: str) -> bool:
        """Return True when *text* looks like model chain-of-thought rather than narration."""
        stripped = text.strip()
        if not stripped:
            return False
        if cls._REASONING_PREFIXES.match(stripped):
            return True
        return False

    @classmethod
    def _sanitize_reasoning(cls, value: object) -> Optional[str]:
        if not isinstance(value, str):
            return None
        cleaned = " ".join(value.strip().split())
        if not cleaned:
            return None
        return cleaned[:1200]

    @classmethod
    def _format_reasoning_spoiler(cls, value: object) -> Optional[str]:
        cleaned = cls._sanitize_reasoning(value)
        if not cleaned:
            return None
        cleaned = cleaned.replace("||", "| |")
        return f"Reasoning: ||{cleaned}||"

    @classmethod
    def _humanize_context_key(cls, value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return " ".join(part for part in text.replace(":", " ").replace("-", " ").split())

    @classmethod
    def _format_private_context_status(
        cls,
        player_state: Dict[str, object],
        *,
        recent_contexts: Optional[List[Dict[str, object]]] = None,
    ) -> str:

        active_context = cls._active_private_context_from_state(player_state)
        if isinstance(active_context, dict):
            scope = str(active_context.get("scope") or "").strip().lower() or "private"
            target_name = str(active_context.get("target_name") or "").strip()
            label = "limited" if scope == "limited" else "private"
            if target_name:
                return (
                    f"Private context: {label} thread active with {target_name}. "
                    "Keep talking to continue it, or move/rejoin/speak out loud to leave it."
                )
            return (
                f"Private context: {label} thread active. "
                "Keep talking to continue it, or move/rejoin/speak out loud to leave it."
            )
        recent_labels: List[str] = []
        for row in list(recent_contexts or [])[:3]:
            if not isinstance(row, dict):
                continue
            label = cls._humanize_context_key(row.get("context_key"))
            if not label or label in recent_labels:
                continue
            recent_labels.append(label)
        if recent_labels:
            return (
                f"Private context: none active. Recent threads: {', '.join(recent_labels)}. "
                "Use a slash command or reaction to start or resume a private thread, or use phone/text actions."
            )
        return (
            "Private context: none. To start one, use a slash command or reaction, "
            "or use phone/text actions."
        )

    @classmethod
    def _format_turn_look_info(cls, player_state: Dict[str, object]) -> str:
        if not isinstance(player_state, dict):
            return "Look: unavailable."
        room_title = str(player_state.get("room_title") or "").strip()
        room_summary = str(player_state.get("room_summary") or "").strip()
        room_description = str(player_state.get("room_description") or "").strip()
        location = str(player_state.get("location") or "").strip()
        exits = player_state.get("exits")
        exit_list: List[str] = []
        if isinstance(exits, list):
            exit_list = [str(item).strip() for item in exits if str(item or "").strip()]
        elif isinstance(exits, str) and exits.strip():
            exit_list = [part.strip() for part in exits.split(",") if part.strip()]
        look_bits: List[str] = []
        title = room_title or location or "unknown"
        if room_summary:
            look_bits.append(room_summary)
        elif room_description:
            compact_desc = " ".join(room_description.split())
            if compact_desc:
                look_bits.append(compact_desc[:240])
        if exit_list:
            look_bits.append(f"Exits: {', '.join(exit_list)}")
        if look_bits:
            return f"Look: {title}. {' '.join(look_bits)}"
        return f"Look: {title}."

    @classmethod
    def _format_turn_calendar_info(
        cls,
        campaign_state: Dict[str, object],
        *,
        campaign_id: Optional[int] = None,
        viewer_user_id: Optional[int] = None,
    ) -> str:
        game_time = {}
        if isinstance(campaign_state, dict):
            raw_time = campaign_state.get("game_time")
            if isinstance(raw_time, dict):
                game_time = raw_time
        day = cls._coerce_non_negative_int(game_time.get("day", 1), default=1) or 1
        hour = cls._coerce_non_negative_int(game_time.get("hour", 8), default=8)
        minute = cls._coerce_non_negative_int(game_time.get("minute", 0), default=0)
        period = str(game_time.get("period") or "").strip()
        date_label = str(game_time.get("date_label") or "").strip()
        time_prefix = f"Day {day} {hour:02d}:{minute:02d}"
        if period:
            time_prefix = f"{time_prefix} ({period})"
        if date_label:
            time_prefix = f"{time_prefix} [{date_label}]"
        calendar_entries = cls._calendar_for_prompt(
            campaign_state,
            campaign_id=campaign_id,
            viewer_user_id=viewer_user_id,
        )
        if not calendar_entries:
            return f"Cal: {time_prefix}. No upcoming events."
        items: List[str] = []
        for entry in calendar_entries[:3]:
            name = str(entry.get("name") or "Event").strip()
            fire_day = cls._coerce_non_negative_int(entry.get("fire_day", day), default=day)
            fire_hour = cls._coerce_non_negative_int(entry.get("fire_hour", 23), default=23)
            status = str(entry.get("_status") or "").strip().lower()
            location = str(entry.get("location") or "").strip()
            description = str(entry.get("description") or "").strip()
            detail = f"Day {fire_day} {fire_hour:02d}:00"
            if location:
                detail = f"{detail} @ {location}"
            if description:
                detail = f"{detail} - {description}"
            if status and status not in {"upcoming", "today"}:
                detail = f"{detail} [{status}]"
            items.append(f"{name} ({detail})")
        suffix = ""
        if len(calendar_entries) > 3:
            suffix = f" +{len(calendar_entries) - 3} more"
        return f"Cal: {time_prefix}. " + " | ".join(items) + suffix

    @classmethod
    def _format_turn_player_info(cls, player: Optional[ZorkPlayer]) -> List[str]:
        if player is None:
            return ["Player: unavailable."]
        attributes = cls.get_player_attributes(player)
        level = max(1, int(getattr(player, "level", 1) or 1))
        xp = max(0, int(getattr(player, "xp", 0) or 0))
        next_xp = cls.xp_needed_for_level(level)
        total_points = cls.total_points_for_level(level)
        spent = cls.points_spent(attributes)
        remaining = max(0, total_points - spent)
        if attributes:
            attr_text = ", ".join(
                f"{key} {value}"
                for key, value in sorted(attributes.items(), key=lambda item: str(item[0]).lower())
                if isinstance(value, int)
            )
        else:
            attr_text = "none"
        return [
            f"Player: level {level}, XP {xp}/{next_xp} to level up.",
            f"Attributes: {attr_text}. Points: {remaining}/{total_points} free.",
            f"Level up: use `!zork level` at {next_xp}+ XP. Use `!zork attributes` to review or spend points.",
        ]

    @classmethod
    def _format_turn_style_info(
        cls,
        campaign: Optional["ZorkCampaign"],
        *,
        channel_id: Optional[int] = None,
    ) -> str:
        style = cls._resolve_zork_style(campaign=campaign, channel_id=channel_id)
        return f"Style: {style}."

    @classmethod
    def _format_turn_backend_info(
        cls,
        campaign: Optional["ZorkCampaign"],
        *,
        channel_id: Optional[int] = None,
    ) -> str:
        backend_config = cls._resolve_zork_backend(campaign=campaign, channel_id=channel_id)
        backend = str(backend_config.get("backend") or "zai").strip() or "zai"
        model = str(backend_config.get("model") or "").strip()
        if model:
            return f"Backend: {backend} ({model})."
        return f"Backend: {backend}."

    @classmethod
    def _format_turn_visibility_info(cls, visibility: object) -> str:
        if not isinstance(visibility, dict):
            return "Private context: public."
        scope = str(visibility.get("scope") or "").strip().lower() or "public"
        context_label = cls._humanize_context_key(visibility.get("context_key"))
        if scope in {"private", "limited"}:
            if context_label:
                return f"Private context: {scope} thread ({context_label})."
            return f"Private context: {scope}."
        if scope == "local":
            return "Private context: local room context."
        return "Private context: public."

    @classmethod
    def get_turn_info_text_for_message(cls, discord_message_id: int) -> Optional[str]:
        try:
            message_id = int(discord_message_id)
        except (TypeError, ValueError):
            return None
        turn = (
            ZorkTurn.query.filter_by(discord_message_id=message_id, kind="narrator")
            .order_by(ZorkTurn.id.desc())
            .first()
        )
        if turn is None:
            return None
        campaign = ZorkCampaign.query.get(turn.campaign_id) if turn.campaign_id else None
        player = None
        if turn.campaign_id and turn.user_id:
            player = ZorkPlayer.query.filter_by(
                campaign_id=turn.campaign_id,
                user_id=turn.user_id,
            ).first()
        meta = cls._safe_turn_meta(turn)
        player_state: Dict[str, object] = {}
        campaign_state: Dict[str, object] = (
            cls.get_campaign_state(campaign) if campaign is not None else {}
        )
        inventory_line = "Inventory: empty"
        snapshot = ZorkSnapshot.query.filter_by(turn_id=turn.id).first()
        if snapshot is not None:
            snapshot_campaign_state = cls._load_json(snapshot.campaign_state_json or "{}", {})
            if isinstance(snapshot_campaign_state, dict) and snapshot_campaign_state:
                campaign_state = snapshot_campaign_state
            try:
                players_data = json.loads(snapshot.players_json or "[]")
            except Exception:
                players_data = []
            for row in players_data:
                if not isinstance(row, dict):
                    continue
                try:
                    player_id = int(row.get("player_id") or 0)
                except (TypeError, ValueError):
                    player_id = 0
                if player_id != int(turn.user_id or 0):
                    continue
                raw_state = row.get("state_json")
                if isinstance(raw_state, dict):
                    player_state = raw_state
                else:
                    player_state = cls._load_json(raw_state or "{}", {})
                inventory_line = cls._format_inventory(player_state) or "Inventory: empty"
                break
        if not player_state and player is not None:
            player_state = cls.get_player_state(player)
            inventory_line = cls._format_inventory(player_state) or "Inventory: empty"
        viewer_slug = cls._player_slug_key(player_state.get("character_name"))
        active_context_key = str(
            (cls._active_private_context_from_state(player_state) or {}).get("context_key")
            or ""
        ).strip()
        turns = (
            ZorkTurn.query.filter(
                ZorkTurn.campaign_id == turn.campaign_id,
                ZorkTurn.id <= turn.id,
            )
            .order_by(ZorkTurn.id.asc())
            .limit(64)
            .all()
        )
        recent_contexts = cls._recent_private_contexts_for_prompt(
            turns,
            viewer_user_id=int(turn.user_id or 0),
            viewer_slug=viewer_slug,
            active_context_key=active_context_key,
            limit=3,
        )
        private_context_line = cls._format_private_context_status(
            player_state,
            recent_contexts=recent_contexts,
        )
        look_line = cls._format_turn_look_info(player_state)
        calendar_line = cls._format_turn_calendar_info(
            campaign_state,
            campaign_id=turn.campaign_id,
            viewer_user_id=int(turn.user_id or 0) if turn.user_id else None,
        )
        player_lines = cls._format_turn_player_info(player)
        style_line = cls._format_turn_style_info(campaign, channel_id=turn.channel_id)
        backend_line = cls._format_turn_backend_info(campaign, channel_id=turn.channel_id)
        reasoning_line = cls._format_reasoning_spoiler(meta.get("reasoning")) or "Reasoning: unavailable."
        return "\n".join(
            [
                look_line,
                calendar_line,
                *player_lines,
                style_line,
                backend_line,
                inventory_line,
                private_context_line,
                reasoning_line,
            ]
        )

    @classmethod
    def _recent_private_contexts_for_prompt(
        cls,
        turns: List["ZorkTurn"],
        *,
        viewer_user_id: int,
        viewer_slug: str,
        active_context_key: str = "",
        limit: int = 3,
    ) -> List[Dict[str, object]]:
        out: List[Dict[str, object]] = []
        seen: set[str] = set()
        viewer_slug_key = cls._player_slug_key(viewer_slug)
        active_context_key = str(active_context_key or "").strip()
        for turn in reversed(list(turns or [])):
            meta = cls._safe_turn_meta(turn)
            visibility = meta.get("visibility")
            if not isinstance(visibility, dict):
                continue
            scope = str(visibility.get("scope") or "").strip().lower()
            if scope not in {"private", "limited"}:
                continue
            context_key = str(
                visibility.get("context_key") or meta.get("context_key") or ""
            ).strip()
            if not context_key or context_key in seen:
                continue
            actor_user_id = visibility.get("actor_user_id")
            actor_player_slug = cls._player_slug_key(
                visibility.get("actor_player_slug") or ""
            )
            visible_user_ids: List[int] = []
            for item in list(visibility.get("visible_user_ids") or []):
                try:
                    visible_user_ids.append(int(item))
                except (TypeError, ValueError):
                    continue
            visible_player_slugs = [
                cls._player_slug_key(item)
                for item in list(visibility.get("visible_player_slugs") or [])
                if cls._player_slug_key(item)
            ]
            if not (
                (actor_user_id is not None and int(actor_user_id) == int(viewer_user_id))
                or (viewer_user_id in visible_user_ids)
                or (viewer_slug_key and viewer_slug_key == actor_player_slug)
                or (viewer_slug_key and viewer_slug_key in visible_player_slugs)
            ):
                continue
            row = {
                "context_key": context_key,
                "scope": scope,
                "location_key": str(visibility.get("location_key") or meta.get("location_key") or "").strip() or None,
                "actor_player_slug": actor_player_slug or None,
                "visible_player_slugs": visible_player_slugs,
                "visible_user_ids": visible_user_ids,
                "aware_npc_slugs": [
                    str(item or "").strip()
                    for item in list(visibility.get("aware_npc_slugs") or [])
                    if str(item or "").strip()
                ],
                "active": bool(active_context_key and context_key == active_context_key),
                "turn_id": int(getattr(turn, "id", 0) or 0),
            }
            out.append(row)
            seen.add(context_key)
            if len(out) >= max(1, int(limit)):
                break
        if active_context_key and active_context_key not in seen:
            out.insert(
                0,
                {
                    "context_key": active_context_key,
                    "scope": "private",
                    "location_key": None,
                    "actor_player_slug": viewer_slug_key or None,
                    "visible_player_slugs": [viewer_slug_key] if viewer_slug_key else [],
                    "visible_user_ids": [int(viewer_user_id)],
                    "aware_npc_slugs": [],
                    "active": True,
                    "turn_id": None,
                },
            )
        return out[: max(1, int(limit))]

    @classmethod
    def _fallback_private_context_from_recent(
        cls,
        turns: List["ZorkTurn"],
        *,
        viewer_user_id: int,
        viewer_slug: str,
        viewer_location_key: str,
        limit: int = 6,
    ) -> Optional[Dict[str, object]]:
        viewer_location_key_norm = cls._normalize_location_key(viewer_location_key)
        if not viewer_location_key_norm:
            return None
        recent_contexts = cls._recent_private_contexts_for_prompt(
            turns,
            viewer_user_id=viewer_user_id,
            viewer_slug=viewer_slug,
            active_context_key="",
            limit=limit,
        )
        for row in recent_contexts:
            if not isinstance(row, dict):
                continue
            row_location_key = cls._normalize_location_key(row.get("location_key"))
            if row_location_key != viewer_location_key_norm:
                continue
            scope = str(row.get("scope") or "").strip().lower()
            context_key = str(row.get("context_key") or "").strip()
            if scope not in {"private", "limited"} or not context_key:
                continue
            return {
                "scope": scope,
                "context_key": context_key,
                "location_key": str(row.get("location_key") or "").strip() or None,
                "target_name": None,
                "target_slug": None,
                "target_user_id": None,
                "engagement": "resume",
            }
        return None

    @classmethod
    def _fallback_private_context_from_rows(
        cls,
        recent_contexts: List[Dict[str, object]],
        *,
        viewer_location_key: str,
    ) -> Optional[Dict[str, object]]:
        viewer_location_key_norm = cls._normalize_location_key(viewer_location_key)
        if not viewer_location_key_norm:
            return None
        for row in list(recent_contexts or []):
            if not isinstance(row, dict):
                continue
            row_location_key = cls._normalize_location_key(row.get("location_key"))
            if row_location_key != viewer_location_key_norm:
                continue
            scope = str(row.get("scope") or "").strip().lower()
            context_key = str(row.get("context_key") or "").strip()
            if scope not in {"private", "limited"} or not context_key:
                continue
            return {
                "scope": scope,
                "context_key": context_key,
                "location_key": str(row.get("location_key") or "").strip() or None,
                "target_name": None,
                "target_slug": None,
                "target_user_id": None,
                "engagement": "resume",
            }
        return None

    @classmethod
    def _fallback_narration_from_payload(cls, payload: Dict[str, object]) -> str:
        if not isinstance(payload, dict):
            return ""
        player_state_update = payload.get("player_state_update")
        if isinstance(player_state_update, dict):
            room_summary = str(player_state_update.get("room_summary") or "").strip()
            if room_summary:
                return room_summary[:300]
            room_title = str(player_state_update.get("room_title") or "").strip()
            if room_title:
                return f"{room_title}."
        summary_update = str(payload.get("summary_update") or "").strip()
        if summary_update:
            return summary_update.splitlines()[0][:300]
        character_updates = payload.get("character_updates")
        if isinstance(character_updates, dict) and character_updates:
            return "Character roster updated."
        calendar_update = payload.get("calendar_update")
        if isinstance(calendar_update, dict) and calendar_update:
            return "Calendar updated."
        state_update = payload.get("state_update")
        if isinstance(state_update, dict) and state_update:
            return "Noted."
        if isinstance(player_state_update, dict) and player_state_update:
            return "Noted."
        return ""

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

    @staticmethod
    def _normalize_location_text(value: object) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip())

    @classmethod
    def _location_state_key(cls, value: object) -> str:
        text = cls._normalize_location_text(value).lower()
        if not text:
            return ""
        return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:100]

    @classmethod
    def _active_location_modifications_for_prompt(
        cls,
        campaign_state: Dict[str, object],
        player_state: Dict[str, object],
    ) -> Dict[str, object]:
        if not isinstance(campaign_state, dict) or not isinstance(player_state, dict):
            return {}
        raw_locations = campaign_state.get("locations")
        if not isinstance(raw_locations, dict):
            return {}
        candidate_keys: List[str] = []
        for raw in (
            player_state.get("location"),
            player_state.get("room_title"),
            player_state.get("room_summary"),
        ):
            key = cls._location_state_key(raw)
            if key and key not in candidate_keys:
                candidate_keys.append(key)
        if not candidate_keys:
            return {}
        for key in candidate_keys:
            row = raw_locations.get(key)
            if not isinstance(row, dict):
                continue
            mods = row.get("modifications")
            if isinstance(mods, list):
                clean_mods = []
                for item in mods[:24]:
                    item_text = " ".join(str(item or "").strip().split())[:180]
                    if item_text:
                        clean_mods.append(item_text)
                if clean_mods:
                    return {
                        "location_key": key,
                        "modifications": clean_mods,
                    }
            elif isinstance(mods, dict) and mods:
                return {
                    "location_key": key,
                    "modifications": mods,
                }
        return {}

    @classmethod
    def _resolve_player_location_for_state_sync(
        cls, player_state: Dict[str, object]
    ) -> str:
        if not isinstance(player_state, dict):
            return ""
        for key in ("location", "room_title", "room_summary"):
            text = cls._normalize_location_text(player_state.get(key))
            if text:
                return text[:160]
        return ""

    @classmethod
    def _player_entity_aliases_for_state_sync(
        cls,
        player_state: Dict[str, object],
    ) -> List[str]:
        aliases: List[str] = []
        seen: set[str] = set()

        def _add(raw: object) -> None:
            slug = cls._player_slug_key(raw)
            if not slug or slug in seen:
                return
            seen.add(slug)
            aliases.append(slug)

        name = str(player_state.get("character_name") or "").strip()
        if not name:
            return aliases
        _add(name)
        for sep in (",", "(", " - "):
            if sep in name:
                _add(name.split(sep, 1)[0].strip())
        words = [part for part in re.split(r"\s+", name) if part]
        if words:
            _add(words[0])
        return aliases

    @classmethod
    def _sync_player_states_from_campaign_entities(
        cls,
        campaign: ZorkCampaign,
        campaign_state: Dict[str, object],
        *,
        skip_user_id: Optional[int] = None,
    ) -> int:
        if not isinstance(campaign_state, dict):
            return 0
        synced = 0
        relevant_keys = (
            "location",
            "room_title",
            "room_summary",
            "room_description",
            "exits",
            "current_status",
        )
        players = ZorkPlayer.query.filter_by(campaign_id=campaign.id).all()
        for target in players:
            user_id = getattr(target, "user_id", None)
            if skip_user_id is not None and user_id == skip_user_id:
                continue
            target_state = cls.get_player_state(target)
            entity_state = None
            for alias in cls._player_entity_aliases_for_state_sync(target_state):
                candidate = campaign_state.get(alias)
                if isinstance(candidate, dict) and any(
                    key in candidate for key in relevant_keys
                ):
                    entity_state = candidate
                    break
            if not isinstance(entity_state, dict):
                continue

            changed = False
            old_location = str(target_state.get("location") or "").strip()
            new_location = str(entity_state.get("location") or "").strip()
            if new_location and new_location != old_location:
                target_state["location"] = new_location
                changed = True
                if "room_title" in entity_state:
                    target_state["room_title"] = entity_state.get("room_title")
                else:
                    target_state.pop("room_title", None)
                if "room_summary" in entity_state:
                    target_state["room_summary"] = entity_state.get("room_summary")
                else:
                    target_state.pop("room_summary", None)
                if "room_description" in entity_state:
                    target_state["room_description"] = entity_state.get("room_description")
                else:
                    target_state.pop("room_description", None)
                if "exits" in entity_state:
                    target_state["exits"] = entity_state.get("exits")
                else:
                    target_state.pop("exits", None)

            for key in ("current_status",):
                if key in entity_state and entity_state.get(key) != target_state.get(key):
                    target_state[key] = entity_state.get(key)
                    changed = True

            if changed:
                target.state_json = cls._dump_json(target_state)
                synced += 1
        return synced

    @classmethod
    def _normalize_co_located_player_slugs(
        cls,
        raw_value: object,
        *,
        actor_slug: str = "",
    ) -> List[str]:
        if not isinstance(raw_value, list):
            return []
        actor_slug_key = cls._player_slug_key(actor_slug)
        out: List[str] = []
        seen: set[str] = set()
        for item in raw_value:
            slug = cls._player_slug_key(item)
            if not slug or slug == actor_slug_key or slug in seen:
                continue
            seen.add(slug)
            out.append(slug)
        return out

    @classmethod
    def _sync_marked_co_located_players(
        cls,
        campaign_id: int,
        source_user_id: int,
        source_state: Dict[str, object],
        player_slugs: List[str],
    ) -> int:
        if not isinstance(source_state, dict) or not isinstance(player_slugs, list):
            return 0
        slug_set = {
            cls._player_slug_key(item)
            for item in player_slugs
            if cls._player_slug_key(item)
        }
        if not slug_set:
            return 0
        has_room_context = any(
            source_state.get(key)
            for key in ("room_id", "location", "room_title", "room_summary", "room_description")
        )
        if not has_room_context:
            return 0
        changed = 0
        targets = (
            ZorkPlayer.query.filter(
                ZorkPlayer.campaign_id == campaign_id,
                ZorkPlayer.user_id != source_user_id,
            )
            .all()
        )
        for target in targets:
            target_state = cls.get_player_state(target)
            target_slug = cls._player_slug_key(target_state.get("character_name"))
            fallback_slug = cls._player_slug_key(f"player-{getattr(target, 'user_id', '')}")
            if target_slug not in slug_set and fallback_slug not in slug_set:
                continue
            before = dict(target_state)
            for key in cls.ROOM_STATE_KEYS:
                src_val = source_state.get(key)
                if src_val is None:
                    target_state.pop(key, None)
                else:
                    target_state[key] = src_val
            if target_state == before:
                continue
            target.state_json = cls._dump_json(target_state)
            target.updated = db.func.now()
            changed += 1
        return changed

    @staticmethod
    def _entity_name_candidates_for_sync(
        state_key: object, entity_state: Dict[str, object]
    ) -> List[str]:
        candidates: List[str] = []
        raw_name = ""
        if isinstance(entity_state, dict):
            raw_name = str(entity_state.get("name") or "").strip().lower()
        if raw_name:
            candidates.append(re.sub(r"\s+", " ", raw_name))
        key_text = re.sub(r"[_\-]+", " ", str(state_key or "").strip().lower())
        key_text = re.sub(r"\s+", " ", key_text).strip()
        if key_text:
            candidates.append(key_text)
        deduped: List[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if len(candidate) < 3:
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            deduped.append(candidate)
        return deduped

    @classmethod
    def _narration_implies_entity_with_player(
        cls,
        narration_text: str,
        name_candidates: List[str],
    ) -> bool:
        text = str(narration_text or "").strip().lower()
        if not text or not name_candidates:
            return False
        cues = (
            "at your heels",
            "at your heel",
            "by your side",
            "beside you",
            "with you",
            "follows you",
            "following you",
            "trailing you",
            "trotting at",
            "walks with you",
            "stays close",
        )
        if not any(cue in text for cue in cues):
            return False
        for name in name_candidates:
            if re.search(rf"\b{re.escape(name)}\b", text):
                return True
        return False

    @classmethod
    def _narration_mentions_entity_in_active_scene(
        cls,
        narration_text: str,
        name_candidates: List[str],
    ) -> bool:
        text = str(narration_text or "").strip().lower()
        if not text or not name_candidates:
            return False
        remote_cues = (
            "sms",
            "text message",
            "texts you",
            "calls you",
            "on the phone",
            "voicemail",
            "news feed",
            "on tv",
            "radio says",
            "video call",
        )
        if any(cue in text for cue in remote_cues):
            return False
        presence_cues = (
            "is here",
            "in the room",
            "across from you",
            "beside you",
            "nearby",
            "waits",
            "stands",
            "sits",
            "arrives",
            "at the desk",
            "at reception",
        )
        if not any(cue in text for cue in presence_cues):
            return False
        for name in name_candidates:
            if re.search(rf"\b{re.escape(name)}\b", text):
                return True
        return False

    @classmethod
    def _auto_sync_companion_locations(
        cls,
        campaign_state: Dict[str, object],
        *,
        player_state: Dict[str, object],
        narration_text: str,
    ) -> int:
        if not isinstance(campaign_state, dict):
            return 0
        player_location = cls._resolve_player_location_for_state_sync(player_state)
        if not player_location:
            return 0
        changed = 0
        for raw_key, raw_value in campaign_state.items():
            key = str(raw_key or "")
            if key in cls.MODEL_STATE_EXCLUDE_KEYS:
                continue
            if not isinstance(raw_value, dict):
                continue
            if "location" not in raw_value:
                continue
            current_location = cls._normalize_location_text(raw_value.get("location"))
            if not current_location or current_location == player_location:
                continue
            follows_flag = any(
                bool(raw_value.get(flag))
                for flag in (
                    "follows_player",
                    "following_player",
                    "with_player",
                    "companion",
                    "pet",
                    "at_heels",
                )
            )
            if not follows_flag:
                names = cls._entity_name_candidates_for_sync(key, raw_value)
                if not cls._narration_implies_entity_with_player(
                    narration_text, names
                ):
                    continue
            raw_value["location"] = player_location
            changed += 1
        return changed

    @classmethod
    def _auto_sync_character_locations(
        cls,
        campaign: ZorkCampaign,
        *,
        player_state: Dict[str, object],
        narration_text: str,
    ) -> int:
        player_location = cls._resolve_player_location_for_state_sync(player_state)
        if not player_location:
            return 0
        characters = cls.get_campaign_characters(campaign)
        if not isinstance(characters, dict) or not characters:
            return 0
        changed = 0
        for slug, entry in characters.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("deceased_reason"):
                continue
            current_location = cls._normalize_location_text(entry.get("location"))
            if not current_location or current_location == player_location:
                continue
            names = cls._entity_name_candidates_for_sync(slug, entry)
            if not (
                cls._narration_implies_entity_with_player(narration_text, names)
                or cls._narration_mentions_entity_in_active_scene(
                    narration_text, names
                )
            ):
                continue
            entry["location"] = player_location
            changed += 1
        if changed:
            campaign.characters_json = cls._dump_json(characters)
        return changed

    @classmethod
    def _sync_npc_locations_from_state_to_roster(
        cls,
        campaign: ZorkCampaign,
        state_update: Dict[str, object],
    ) -> int:
        """Propagate NPC location data from state_update overlay into characters_json.

        When the model puts NPC mutable fields (especially ``location`` and
        ``current_status``) inside ``state_update.<slug>`` instead of
        ``character_updates.<slug>``, the campaign_state overlay gets updated
        but the persistent roster (``characters_json``) stays stale.  This
        method detects those cases and patches the roster so both stores agree.
        """
        if not isinstance(state_update, dict):
            return 0
        characters = cls.get_campaign_characters(campaign)
        if not isinstance(characters, dict) or not characters:
            return 0
        overlay_mutable = {"location", "current_status", "allegiance"}
        changed = 0
        for slug, overlay in state_update.items():
            if not isinstance(overlay, dict):
                continue
            if slug not in characters:
                continue
            entry = characters[slug]
            if not isinstance(entry, dict):
                continue
            for field in overlay_mutable:
                if field not in overlay:
                    continue
                new_val = overlay[field]
                if new_val is None:
                    continue
                old_val = entry.get(field)
                if old_val != new_val:
                    entry[field] = new_val
                    changed += 1
        if changed:
            campaign.characters_json = cls._dump_json(characters)
            _zork_log(
                "NPC ROSTER SYNC FROM STATE_UPDATE",
                f"Patched {changed} field(s) in characters_json from state_update overlay",
            )
        return changed

    @classmethod
    def _sync_active_player_character_location(
        cls,
        campaign: ZorkCampaign,
        *,
        player_state: Dict[str, object],
    ) -> int:
        player_location = cls._resolve_player_location_for_state_sync(player_state)
        if not player_location:
            return 0
        character_name = cls._normalize_location_text(
            player_state.get("character_name")
        ).lower()
        if not character_name:
            return 0
        characters = cls.get_campaign_characters(campaign)
        if not isinstance(characters, dict) or not characters:
            return 0

        target_slug = cls._resolve_existing_character_slug(characters, character_name)
        if target_slug is None:
            for slug, entry in characters.items():
                if not isinstance(entry, dict):
                    continue
                entry_name = cls._normalize_location_text(entry.get("name")).lower()
                if entry_name and entry_name == character_name:
                    target_slug = slug
                    break
        if target_slug is None:
            return 0

        entry = characters.get(target_slug)
        if not isinstance(entry, dict):
            return 0
        current_location = cls._normalize_location_text(entry.get("location"))
        if current_location == player_location:
            return 0
        entry["location"] = player_location
        campaign.characters_json = cls._dump_json(characters)
        return 1

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
    def create_campaign(
        cls, guild_id: int, name: str, created_by: int
    ) -> ZorkCampaign:
        normalized = cls._normalize_campaign_name(name)
        for _ in range(5):
            candidate_name = cls._allocate_unique_campaign_name(guild_id, normalized)
            campaign = ZorkCampaign(
                guild_id=guild_id,
                name=candidate_name,
                created_by=created_by,
                summary="",
                state_json="{}",
            )
            db.session.add(campaign)
            try:
                db.session.commit()
                return campaign
            except IntegrityError:
                db.session.rollback()
                continue
        raise RuntimeError(
            f"Could not allocate a unique campaign name for guild {guild_id}: {normalized!r}"
        )

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
    def _sync_main_party_room_state(
        cls,
        campaign_id: int,
        source_user_id: int,
        source_state: Dict[str, object],
    ) -> None:
        if not isinstance(source_state, dict):
            return
        party_status = str(source_state.get("party_status") or "").strip().lower()
        if party_status != "main_party":
            return
        has_room_context = any(
            source_state.get(key)
            for key in ("room_id", "location", "room_title", "room_summary", "room_description")
        )
        if not has_room_context:
            return

        targets = (
            ZorkPlayer.query.filter(
                ZorkPlayer.campaign_id == campaign_id,
                ZorkPlayer.user_id != source_user_id,
            )
            .all()
        )
        for target in targets:
            target_state = cls.get_player_state(target)
            if str(target_state.get("party_status") or "").strip().lower() != "main_party":
                continue
            for key in cls.ROOM_STATE_KEYS:
                src_val = source_state.get(key)
                if src_val is None:
                    target_state.pop(key, None)
                else:
                    target_state[key] = src_val
            target.state_json = cls._dump_json(target_state)
            target.updated = db.func.now()

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
        if not cls.PRESET_CAMPAIGNS:
            return "No in-repo campaigns are configured."
        sample = ", ".join(cls.PRESET_CAMPAIGNS.keys())
        return f"Available campaigns: {sample}"

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
        prefix = cls.SCENE_IMAGE_PRESERVE_PREFIX.strip()
        if prefix and not prompt_for_generation.lower().startswith(prefix.lower()):
            prompt_for_generation = f"{prefix}. {prompt_for_generation}".strip()
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
                    "zork_scene_prompt": scene_image_prompt,
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
        if not isinstance(data, dict):
            return {}
        sanitized = cls._sanitize_npc_roster_against_players(campaign.id, data)
        if sanitized != data:
            campaign.characters_json = cls._dump_json(sanitized)
            campaign.updated = db.func.now()
            db.session.commit()
        return sanitized

    @classmethod
    def _campaign_player_match_keys(
        cls,
        campaign_id: int,
    ) -> Dict[str, Dict[str, object]]:
        registry = cls._campaign_player_registry(campaign_id)
        out: Dict[str, Dict[str, object]] = {}

        def _add_player_token(tokens: set[str], raw_value: object) -> None:
            value_text = str(raw_value or "").strip()
            if not value_text:
                return
            candidates = {value_text}
            for sep in (",", "(", " - "):
                if sep in value_text:
                    prefix = value_text.split(sep, 1)[0].strip()
                    if prefix:
                        candidates.add(prefix)
            for candidate in candidates:
                normalized = cls._normalize_match_text(candidate)
                if normalized:
                    tokens.add(normalized)
                slug_key = cls._player_slug_key(candidate)
                if slug_key:
                    tokens.add(slug_key)

        for entry in registry.get("by_user_id", {}).values():
            if not isinstance(entry, dict):
                continue
            tokens: set[str] = set()
            user_id = entry.get("user_id")
            name = str(entry.get("name") or "").strip()
            slug = str(entry.get("slug") or "").strip()
            mention = str(entry.get("discord_mention") or "").strip()
            if isinstance(user_id, int):
                tokens.add(str(user_id))
                tokens.add(cls._normalize_match_text(f"<@{user_id}>"))
                tokens.add(cls._normalize_match_text(f"<@!{user_id}>"))
            if name:
                _add_player_token(tokens, name)
            if slug:
                tokens.add(cls._normalize_match_text(slug))
            if mention:
                tokens.add(cls._normalize_match_text(mention))
            tokens = {token for token in tokens if token}
            for token in tokens:
                out[token] = entry
        return out

    @classmethod
    def _character_update_hits_player(
        cls,
        campaign_id: int,
        raw_slug: object,
        fields: object,
    ) -> Optional[Dict[str, object]]:
        player_index = cls._campaign_player_match_keys(campaign_id)
        candidates: List[str] = []
        slug_text = str(raw_slug or "").strip()
        if slug_text:
            candidates.append(cls._normalize_match_text(slug_text))
            slug_key = cls._player_slug_key(slug_text)
            if slug_key:
                candidates.append(slug_key)
        if isinstance(fields, dict):
            for key in ("name", "slug", "player_slug", "discord_mention", "user_id"):
                raw_value = fields.get(key)
                value_text = str(raw_value or "").strip()
                if not value_text:
                    continue
                candidates.append(cls._normalize_match_text(value_text))
                value_slug = cls._player_slug_key(value_text)
                if value_slug:
                    candidates.append(value_slug)
        for candidate in candidates:
            if not candidate:
                continue
            match = player_index.get(candidate)
            if match is not None:
                return match
        return None

    @classmethod
    def _sanitize_npc_roster_against_players(
        cls,
        campaign_id: int,
        characters: Dict[str, dict],
    ) -> Dict[str, dict]:
        if not isinstance(characters, dict) or not characters:
            return {}
        out: Dict[str, dict] = {}
        for slug, payload in characters.items():
            match = cls._character_update_hits_player(campaign_id, slug, payload)
            if match is not None:
                logger.warning(
                    "Dropping WORLD_CHARACTERS entry %r because it collides with player %r (%s)",
                    slug,
                    match.get("name"),
                    match.get("user_id"),
                )
                continue
            out[str(slug)] = payload
        return out

    @classmethod
    def _apply_character_updates(
        cls,
        existing: Dict[str, dict],
        updates: Dict[str, object],
        on_rails: bool = False,
        campaign_id: Optional[int] = None,
    ) -> Dict[str, dict]:
        """Merge character updates into existing characters dict.

        New slugs get all fields stored.  Existing slugs only get mutable
        fields updated — immutable fields are silently dropped.
        When *on_rails* is True, new slugs are rejected entirely.
        """
        if not isinstance(updates, dict):
            return existing
        for raw_slug, fields in updates.items():
            slug = str(raw_slug).strip()
            if not slug:
                continue
            if campaign_id is not None:
                player_match = cls._character_update_hits_player(
                    campaign_id,
                    slug,
                    fields,
                )
                if player_match is not None:
                    logger.warning(
                        "Ignoring character_update for %r because it targets player %r (%s)",
                        slug,
                        player_match.get("name"),
                        player_match.get("user_id"),
                    )
                    continue

            # Resolve loose slug/name variants back to an existing slug.
            target_slug = cls._resolve_existing_character_slug(existing, slug)

            delete_requested = (
                fields is None
                or (
                    isinstance(fields, str)
                    and fields.strip().lower() in {"delete", "remove", "null"}
                )
                or (
                    isinstance(fields, dict)
                    and bool(
                        fields.get("remove")
                        or fields.get("delete")
                        or fields.get("_delete")
                        or fields.get("deleted")
                    )
                )
            )
            if delete_requested:
                existing.pop(target_slug or slug, None)
                continue
            if not isinstance(fields, dict):
                continue
            if target_slug in existing:
                # Existing character — only accept mutable fields.
                old_location = str(existing[target_slug].get("location") or "").strip().lower()
                for key, value in fields.items():
                    if key not in cls.IMMUTABLE_CHARACTER_FIELDS:
                        if key == "relationships":
                            current_rel = existing[target_slug].get("relationships")
                            if not isinstance(current_rel, dict):
                                current_rel = {}
                            if value is None:
                                existing[target_slug].pop("relationships", None)
                                continue
                            if not isinstance(value, dict):
                                continue
                            merged_rel = dict(current_rel)
                            for rel_key_raw, rel_value in value.items():
                                rel_key = " ".join(
                                    str(rel_key_raw or "").strip().lower().split()
                                )[:80]
                                if not rel_key:
                                    continue
                                if rel_value is None:
                                    merged_rel.pop(rel_key, None)
                                    continue
                                if not isinstance(rel_value, dict):
                                    continue
                                row = dict(merged_rel.get(rel_key) or {})
                                for rel_field in (
                                    "status",
                                    "dynamic",
                                    "notes",
                                ):
                                    if rel_field in rel_value:
                                        row[rel_field] = str(
                                            rel_value.get(rel_field) or ""
                                        ).strip()[:220]
                                for rel_list_field in ("knows_about", "doesnt_know"):
                                    if rel_list_field in rel_value:
                                        items = rel_value.get(rel_list_field)
                                        if isinstance(items, list):
                                            clean_items = []
                                            seen_items = set()
                                            for item in items[:32]:
                                                item_text = " ".join(
                                                    str(item or "").strip().split()
                                                )[:120]
                                                if not item_text:
                                                    continue
                                                item_key = item_text.lower()
                                                if item_key in seen_items:
                                                    continue
                                                seen_items.add(item_key)
                                                clean_items.append(item_text)
                                            row[rel_list_field] = clean_items
                                if row:
                                    merged_rel[rel_key] = row
                            if merged_rel:
                                existing[target_slug]["relationships"] = merged_rel
                            else:
                                existing[target_slug].pop("relationships", None)
                            continue
                        existing[target_slug][key] = value
                # Clear stale current_status when location changes.
                if (
                    "location" in fields
                    and "current_status" not in fields
                    and "current_status" in existing[target_slug]
                ):
                    new_location = str(fields["location"] or "").strip().lower()
                    if old_location and new_location and old_location != new_location:
                        existing[target_slug]["current_status"] = ""
            else:
                if on_rails:
                    logger.info("On-rails: rejected new character slug %r", slug)
                    continue
                # New character — store everything.
                existing[slug] = dict(fields)
        if campaign_id is not None:
            return cls._sanitize_npc_roster_against_players(campaign_id, existing)
        return existing

    @classmethod
    def _resolve_existing_character_slug(
        cls,
        existing: Dict[str, dict],
        raw_slug: object,
    ) -> Optional[str]:
        slug = str(raw_slug or "").strip()
        if not slug:
            return None
        canonical = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")
        if slug in existing:
            return slug
        if canonical and canonical in existing:
            return canonical
        partial_matches: List[str] = []
        for existing_slug, existing_fields in existing.items():
            existing_canonical = re.sub(
                r"[^a-z0-9]+", "-", str(existing_slug).lower()
            ).strip("-")
            if canonical and canonical == existing_canonical:
                return existing_slug
            if canonical and (
                existing_canonical.startswith(canonical)
                or canonical in existing_canonical
            ):
                partial_matches.append(existing_slug)
            if isinstance(existing_fields, dict):
                name_canonical = re.sub(
                    r"[^a-z0-9]+", "-",
                    str(existing_fields.get("name") or "").lower(),
                ).strip("-")
                if canonical and canonical == name_canonical:
                    return existing_slug
                if canonical and (
                    name_canonical.startswith(canonical)
                    or canonical in name_canonical
                ):
                    partial_matches.append(existing_slug)
        if canonical:
            unique_matches = list(dict.fromkeys(partial_matches))
            if len(unique_matches) == 1:
                return unique_matches[0]
        return None

    @classmethod
    def _character_updates_from_state_nulls(
        cls,
        state_update: object,
        existing_chars: Dict[str, dict],
    ) -> Dict[str, object]:
        out: Dict[str, object] = {}
        if not isinstance(state_update, dict) or not isinstance(existing_chars, dict):
            return out
        for key, value in state_update.items():
            if value is not None:
                continue
            resolved = cls._resolve_existing_character_slug(existing_chars, key)
            if resolved:
                out[resolved] = None
        return out

    @classmethod
    def _character_delete_requested(cls, fields: object) -> bool:
        return bool(
            fields is None
            or (
                isinstance(fields, str)
                and fields.strip().lower() in {"delete", "remove", "null"}
            )
            or (
                isinstance(fields, dict)
                and bool(
                    fields.get("remove")
                    or fields.get("delete")
                    or fields.get("_delete")
                    or fields.get("deleted")
                )
            )
        )

    @classmethod
    def _character_delete_allowed(
        cls,
        *,
        raw_slug: str,
        fields: object,
        existing_row: Optional[Dict[str, object]],
        context_text: str,
    ) -> bool:
        context = " ".join(str(context_text or "").lower().split())
        if not context:
            return False
        if isinstance(fields, dict) and str(fields.get("deceased_reason") or "").strip():
            return True

        remove_cues = (
            "remove from roster",
            "roster remove",
            "remove character",
            "delete character",
            "drop character",
            "purge duplicate",
            "duplicate",
            "cleanup roster",
            "roster cleanup",
            "retcon",
            "written out",
            "no longer in story",
        )
        death_cues = (
            "dead",
            "dies",
            "died",
            "killed",
            "murdered",
            "executed",
            "corpse",
            "funeral",
            "deceased",
        )
        has_delete_intent = any(cue in context for cue in remove_cues) or any(
            cue in context for cue in death_cues
        )
        if not has_delete_intent:
            return False

        aliases: List[str] = []
        slug_alias = re.sub(r"[^a-z0-9]+", " ", str(raw_slug or "").lower()).strip()
        if slug_alias:
            aliases.append(slug_alias)
        if isinstance(existing_row, dict):
            name_alias = re.sub(
                r"[^a-z0-9]+", " ",
                str(existing_row.get("name") or "").lower(),
            ).strip()
            if name_alias:
                aliases.append(name_alias)
        for alias in aliases:
            if alias and alias in context:
                return True
            tokens = [t for t in alias.split() if len(t) >= 4]
            if any(token in context for token in tokens):
                return True
        return False

    @classmethod
    def _sanitize_character_removals(
        cls,
        existing_chars: Dict[str, dict],
        updates: object,
        *,
        resolution_context: str = "",
        campaign_state: Optional[Dict[str, object]] = None,
        counter_key: str = "character_remove_blocked",
    ) -> Dict[str, object]:
        if not isinstance(updates, dict):
            return {}
        # Character deletion is now fully model-controlled (reasoning + structured updates).
        # Keep this hook for compatibility, but do not block removals.
        return dict(updates)

    @classmethod
    def _guard_state_null_character_prunes(
        cls,
        state_update: object,
        existing_chars: Dict[str, dict],
        *,
        resolution_context: str = "",
        campaign_state: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        if not isinstance(state_update, dict):
            return {}
        if not isinstance(existing_chars, dict):
            return dict(state_update)
        candidate_deletes: Dict[str, object] = {}
        for raw_key, value in state_update.items():
            if value is not None:
                continue
            resolved = cls._resolve_existing_character_slug(existing_chars, raw_key)
            if resolved:
                candidate_deletes[str(raw_key)] = None
        if not candidate_deletes:
            return dict(state_update)
        allowed_deletes = cls._sanitize_character_removals(
            existing_chars,
            candidate_deletes,
            resolution_context=resolution_context,
            campaign_state=campaign_state,
            counter_key="state_character_prune_blocked",
        )
        out = dict(state_update)
        for raw_key in candidate_deletes.keys():
            if raw_key not in allowed_deletes:
                out.pop(raw_key, None)
        return out

    @staticmethod
    def _calendar_resolve_fire_point(
        current_day: int,
        current_hour: int,
        time_remaining: object,
        time_unit: object,
    ) -> Tuple[int, int]:
        try:
            day = int(current_day)
        except (TypeError, ValueError):
            day = 1
        try:
            hour = int(current_hour)
        except (TypeError, ValueError):
            hour = 8
        day = max(1, day)
        hour = min(23, max(0, hour))
        try:
            remaining = int(time_remaining)
        except (TypeError, ValueError):
            remaining = 1
        unit = str(time_unit or "days").strip().lower()
        base_hours = (day - 1) * 24 + hour
        if unit.startswith("hour"):
            fire_abs_hours = base_hours + remaining
        else:
            fire_abs_hours = base_hours + (remaining * 24)
        fire_abs_hours = max(0, int(fire_abs_hours))
        fire_day = (fire_abs_hours // 24) + 1
        fire_hour = fire_abs_hours % 24
        return max(1, int(fire_day)), min(23, max(0, int(fire_hour)))

    @staticmethod
    def _calendar_resolve_fire_day(
        current_day: int,
        current_hour: int,
        time_remaining: object,
        time_unit: object,
    ) -> int:
        fire_day, _ = ZorkEmulator._calendar_resolve_fire_point(
            current_day=current_day,
            current_hour=current_hour,
            time_remaining=time_remaining,
            time_unit=time_unit,
        )
        return fire_day

    @staticmethod
    def _calendar_fix_ampm(fire_hour: int, description: str) -> int:
        """Fix AM/PM mismatch — e.g. LLM outputs fire_hour=7 for '7pm'."""
        if not description:
            return fire_hour
        text = description.lower()
        for m in re.finditer(r"\b(\d{1,2})(?:\s*:\s*\d{2})?\s*(am|pm)\b", text):
            desc_hour = int(m.group(1))
            ampm = m.group(2)
            if desc_hour < 1 or desc_hour > 12:
                continue
            if ampm == "pm":
                expected_24h = desc_hour if desc_hour == 12 else desc_hour + 12
            else:
                expected_24h = 0 if desc_hour == 12 else desc_hour
            if fire_hour == desc_hour and fire_hour != expected_24h:
                fire_hour = expected_24h
                break
        return fire_hour

    @staticmethod
    def _calendar_fix_relative_day(fire_day: int, description: str, current_day: int) -> int:
        """Fix off-by-one when description says 'tomorrow' but fire_day == today."""
        if not description:
            return fire_day
        text = description.lower()
        if re.search(r"\btomorrow\b", text):
            expected = current_day + 1
            if fire_day == current_day:
                return expected
        elif re.search(r"\btoday\b", text):
            if fire_day > current_day:
                return current_day
        return fire_day

    @classmethod
    def _calendar_normalize_event(
        cls,
        event: object,
        *,
        current_day: int,
        current_hour: int,
    ) -> Optional[Dict[str, object]]:
        if not isinstance(event, dict):
            return None
        name = str(event.get("name") or "").strip()
        if not name:
            return None
        fire_day_raw = event.get("fire_day")
        fire_hour_raw = event.get("fire_hour")
        if (
            isinstance(fire_day_raw, (int, float))
            and not isinstance(fire_day_raw, bool)
            and isinstance(fire_hour_raw, (int, float))
            and not isinstance(fire_hour_raw, bool)
        ):
            fire_day = max(1, int(fire_day_raw))
            fire_hour = min(23, max(0, int(fire_hour_raw)))
        elif isinstance(fire_day_raw, (int, float)) and not isinstance(
            fire_day_raw, bool
        ):
            fire_day = max(1, int(fire_day_raw))
            # Backward compatibility for legacy day-only events.
            fire_hour = 23
        else:
            fire_day, fire_hour = cls._calendar_resolve_fire_point(
                current_day=current_day,
                current_hour=current_hour,
                time_remaining=event.get("time_remaining", 1),
                time_unit=event.get("time_unit", "days"),
            )
        description = str(event.get("description") or "")[:200]
        fire_hour = cls._calendar_fix_ampm(fire_hour, description)
        fire_day = cls._calendar_fix_relative_day(fire_day, description, current_day)
        normalized: Dict[str, object] = {
            "name": name,
            "fire_day": fire_day,
            "fire_hour": fire_hour,
            "description": description,
            "known_by": cls._calendar_known_by_from_event(event),
        }
        target_players = cls._calendar_target_tokens_from_event(event)
        if target_players:
            normalized["target_players"] = target_players
        for key in ("created_day", "created_hour"):
            raw = event.get(key)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                normalized[key] = int(raw)
        for key in ("fired_notice_key", "fired_notice_day", "fired_notice_hour"):
            raw = event.get(key)
            if raw is None:
                continue
            if key == "fired_notice_key":
                normalized[key] = str(raw)[:120]
                continue
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                normalized[key] = int(raw)
        return normalized

    @classmethod
    def _calendar_should_prune_stale_event(
        cls,
        event: Dict[str, object],
        *,
        hours_remaining: int,
    ) -> bool:
        if not isinstance(event, dict):
            return False
        if hours_remaining >= 0:
            return False
        overdue_hours = abs(int(hours_remaining))
        fired_notice_key = str(event.get("fired_notice_key") or "").strip()
        if overdue_hours >= 24:
            return True
        if fired_notice_key and overdue_hours >= 6:
            return True
        return False

    @classmethod
    def _calendar_for_prompt(
        cls,
        campaign_state: Dict[str, object],
        *,
        campaign_id: Optional[int] = None,
        viewer_user_id: Optional[int] = None,
    ) -> List[Dict[str, object]]:
        game_time = campaign_state.get("game_time") if isinstance(campaign_state, dict) else {}
        if not isinstance(game_time, dict):
            game_time = {}
        current_day = cls._coerce_non_negative_int(game_time.get("day", 1), default=1) or 1
        current_hour = cls._coerce_non_negative_int(game_time.get("hour", 8), default=8)
        current_hour = min(23, max(0, current_hour))
        calendar = campaign_state.get("calendar") if isinstance(campaign_state, dict) else []
        if not isinstance(calendar, list):
            calendar = []
        entries: List[Dict[str, object]] = []
        calendar_changed = False
        kept_calendar: List[Dict[str, object]] = []
        for raw in calendar:
            normalized = cls._calendar_normalize_event(
                raw,
                current_day=current_day,
                current_hour=current_hour,
            )
            if normalized is None:
                continue
            fire_day = int(normalized.get("fire_day", current_day))
            fire_hour = cls._coerce_non_negative_int(
                normalized.get("fire_hour", 23), default=23
            )
            fire_hour = min(23, max(0, fire_hour))
            if isinstance(raw, dict):
                raw_fire_day = raw.get("fire_day")
                raw_fire_hour = raw.get("fire_hour")
                has_fire_day = isinstance(raw_fire_day, (int, float)) and not isinstance(
                    raw_fire_day, bool
                )
                has_fire_hour = isinstance(raw_fire_hour, (int, float)) and not isinstance(
                    raw_fire_hour, bool
                )
                if (not has_fire_day) or int(raw_fire_day) != fire_day:
                    raw["fire_day"] = fire_day
                    calendar_changed = True
                if (not has_fire_hour) or int(raw_fire_hour) != fire_hour:
                    raw["fire_hour"] = fire_hour
                    calendar_changed = True
                if "time_remaining" in raw:
                    raw.pop("time_remaining", None)
                    calendar_changed = True
                if "time_unit" in raw:
                    raw.pop("time_unit", None)
                    calendar_changed = True
            hours_remaining = ((fire_day - current_day) * 24) + (fire_hour - current_hour)
            days_remaining = fire_day - current_day
            if cls._calendar_should_prune_stale_event(
                normalized,
                hours_remaining=hours_remaining,
            ):
                calendar_changed = True
                continue
            if hours_remaining < 0:
                status = "overdue"
            elif days_remaining == 0:
                status = "today"
            elif hours_remaining <= 24:
                status = "imminent"
            else:
                status = "upcoming"
            view = dict(normalized)
            view["_days_until"] = days_remaining
            view["_hours_until"] = hours_remaining
            view["_status"] = status
            if campaign_id is not None and viewer_user_id is not None:
                target_user_ids = cls._calendar_event_notification_targets(
                    int(campaign_id), raw if isinstance(raw, dict) else normalized
                )
                if target_user_ids and int(viewer_user_id) not in target_user_ids:
                    kept_calendar.append(
                        dict(raw) if isinstance(raw, dict) else dict(normalized)
                    )
                    continue
            entries.append(view)
            kept_calendar.append(dict(raw) if isinstance(raw, dict) else dict(normalized))
        entries.sort(
            key=lambda item: (
                int(item.get("fire_day", current_day)),
                int(item.get("fire_hour", 23)),
                str(item.get("name", "")).lower(),
            )
        )
        if calendar_changed and isinstance(campaign_state, dict):
            campaign_state["calendar"] = kept_calendar
        return entries

    @classmethod
    def _calendar_reminder_text(
        cls,
        calendar_entries: List[Dict[str, object]],
        active_scene_names: Optional[List[str]] = None,
        campaign_state: Optional[Dict[str, object]] = None,
    ) -> str:
        if not calendar_entries:
            return "None"

        def _event_key(event: Dict[str, object]) -> str:
            name = str(event.get("name", "")).strip().lower()
            slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")[:80] or "event"
            created_day = event.get("created_day")
            created_hour = event.get("created_hour")
            if isinstance(created_day, (int, float)) and not isinstance(
                created_day, bool
            ) and isinstance(created_hour, (int, float)) and not isinstance(
                created_hour, bool
            ):
                return (
                    f"{slug}:"
                    f"{max(1, int(created_day))}:"
                    f"{min(23, max(0, int(created_hour)))}"
                )
            desc = str(event.get("description", "")).strip().lower()
            desc_slug = re.sub(r"[^a-z0-9]+", "-", desc).strip("-")[:40] or "na"
            return f"{slug}:{desc_slug}"

        def _reminder_bucket(hours: int) -> Optional[str]:
            if hours == 0:
                return "now"
            if hours < 0:
                overdue = abs(hours)
                # Overdue reminders at 1h for first 3h, then every 6h.
                if overdue <= 3:
                    return f"overdue_1h_{overdue}"
                return f"overdue_6h_{overdue // 6}"
            # Future reminders use widening cadence buckets.
            if hours > 24:
                return f"future_12h_{hours // 12}"
            if hours > 6:
                return f"future_6h_{hours // 6}"
            if hours > 1:
                return f"future_2h_{hours // 2}"
            return "future_1h_1"

        alerts = []
        active_keys = {
            cls._calendar_name_key(name)
            for name in (active_scene_names or [])
            if cls._calendar_name_key(name)
        }
        global_tokens = {"all", "any", "everyone", "global", "scene", "party"}
        reminder_state = {}
        if isinstance(campaign_state, dict):
            raw_state = campaign_state.get(cls.CALENDAR_REMINDER_STATE_KEY)
            if isinstance(raw_state, dict):
                reminder_state = dict(raw_state)
        current_event_keys: set[str] = set()
        reminder_state_changed = False
        for event in calendar_entries:
            known_by = cls._calendar_known_by_from_event(event)
            if known_by:
                known_keys = {
                    cls._calendar_name_key(name)
                    for name in known_by
                    if cls._calendar_name_key(name)
                }
                if not (known_keys & global_tokens):
                    if not active_keys or not (known_keys & active_keys):
                        continue
            hours = int(event.get("_hours_until", event.get("hours_remaining", 0)))
            name = str(event.get("name", "Unknown"))
            fire_day = int(event.get("fire_day", 1))
            fire_hour = max(0, min(23, int(event.get("fire_hour", 23))))
            bucket = _reminder_bucket(hours)
            if not bucket:
                continue
            event_key = _event_key(event)
            current_event_keys.add(event_key)
            if reminder_state.get(event_key) == bucket:
                continue
            if hours < 0:
                alerts.append(
                    f"- OVERDUE: {name} (was Day {fire_day}, {fire_hour:02d}:00; {abs(hours)} hour(s) overdue)"
                )
            elif hours == 0:
                alerts.append(
                    f"- NOW: {name} (fires at Day {fire_day}, {fire_hour:02d}:00)"
                )
            else:
                alerts.append(
                    f"- SOON: {name} (fires in {hours} hour(s) at Day {fire_day}, {fire_hour:02d}:00)"
                )
            reminder_state[event_key] = bucket
            reminder_state_changed = True
        if isinstance(campaign_state, dict):
            stale_keys = [
                key for key in list(reminder_state.keys()) if key not in current_event_keys
            ]
            if stale_keys:
                for key in stale_keys:
                    reminder_state.pop(key, None)
                reminder_state_changed = True
            if reminder_state_changed:
                campaign_state[cls.CALENDAR_REMINDER_STATE_KEY] = reminder_state
        alerts = alerts[:2]
        return "\n".join(alerts) if alerts else "None"

    @classmethod
    def _memory_search_term_key(cls, raw_term: object) -> str:
        return re.sub(r"[^a-z0-9]+", "-", str(raw_term or "").lower()).strip("-")[:80]

    @classmethod
    def _memory_search_usage_from_state(cls, campaign_state: Dict[str, object]) -> Dict[str, dict]:
        raw = (
            campaign_state.get(cls.MEMORY_SEARCH_USAGE_KEY)
            if isinstance(campaign_state, dict)
            else {}
        )
        if not isinstance(raw, dict):
            raw = {}
        out: Dict[str, dict] = {}
        for raw_key, raw_value in raw.items():
            key = cls._memory_search_term_key(raw_key)
            if not key or not isinstance(raw_value, dict):
                continue
            count = cls._coerce_non_negative_int(raw_value.get("count", 0), default=0)
            if count <= 0:
                continue
            label = str(raw_value.get("label") or raw_key).strip()[:120] or key
            out[key] = {"count": count, "label": label}
        return out

    @staticmethod
    def _memory_search_term_looks_character_like(term_key: str) -> bool:
        if not term_key:
            return False
        parts = [part for part in term_key.split("-") if part]
        if not parts or len(parts) > 4:
            return False
        if len(term_key) < 3 or len(term_key) > 48:
            return False
        blocked = {
            "where",
            "what",
            "when",
            "why",
            "how",
            "room",
            "scene",
            "inventory",
            "calendar",
            "event",
            "events",
            "map",
            "summary",
            "story",
            "chapter",
            "turn",
        }
        return not any(part in blocked for part in parts)

    @classmethod
    def _record_memory_search_usage_and_hints(
        cls,
        campaign,
        queries: List[str],
    ) -> List[Dict[str, object]]:
        campaign_state = cls.get_campaign_state(campaign)
        usage = cls._memory_search_usage_from_state(campaign_state)
        updated_keys: List[str] = []
        for query in queries[:8]:
            query_text = str(query or "").strip()
            if not query_text:
                continue
            term_key = cls._memory_search_term_key(query_text)
            if not term_key:
                continue
            row = usage.get(term_key, {"count": 0, "label": query_text[:120]})
            row["count"] = cls._coerce_non_negative_int(row.get("count", 0), default=0) + 1
            if not str(row.get("label") or "").strip():
                row["label"] = query_text[:120]
            usage[term_key] = row
            updated_keys.append(term_key)

        if len(usage) > cls.MEMORY_SEARCH_USAGE_MAX_TERMS:
            ranked = sorted(
                usage.items(),
                key=lambda kv: (
                    cls._coerce_non_negative_int(kv[1].get("count", 0), default=0),
                    kv[0],
                ),
                reverse=True,
            )
            usage = dict(ranked[: cls.MEMORY_SEARCH_USAGE_MAX_TERMS])

        campaign_state[cls.MEMORY_SEARCH_USAGE_KEY] = usage
        campaign.state_json = cls._dump_json(campaign_state)
        campaign.updated = db.func.now()

        characters = cls.get_campaign_characters(campaign)
        hints: List[Dict[str, object]] = []
        seen_keys: set[str] = set()
        for term_key in updated_keys:
            if term_key in seen_keys:
                continue
            seen_keys.add(term_key)
            row = usage.get(term_key) or {}
            count = cls._coerce_non_negative_int(row.get("count", 0), default=0)
            if count < cls.MEMORY_SEARCH_ROSTER_HINT_THRESHOLD:
                continue
            if not cls._memory_search_term_looks_character_like(term_key):
                continue
            if isinstance(characters, dict) and cls._resolve_existing_character_slug(characters, term_key):
                continue
            hints.append(
                {
                    "term": str(row.get("label") or term_key),
                    "slug": term_key,
                    "count": count,
                }
            )
        return hints

    @classmethod
    def _plot_thread_key(cls, value: object) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip().lower())
        if not text:
            return ""
        return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:80]

    @classmethod
    def _plot_threads_from_state(
        cls, campaign_state: Dict[str, object]
    ) -> Dict[str, Dict[str, object]]:
        raw = (
            campaign_state.get(cls.PLOT_THREADS_STATE_KEY)
            if isinstance(campaign_state, dict)
            else {}
        )
        if not isinstance(raw, dict):
            raw = {}
        threads: Dict[str, Dict[str, object]] = {}
        for raw_key, raw_value in raw.items():
            if not isinstance(raw_value, dict):
                continue
            thread_key = cls._plot_thread_key(raw_value.get("thread") or raw_key)
            if not thread_key:
                continue
            status = str(raw_value.get("status") or "active").strip().lower()
            if status not in {"active", "resolved"}:
                status = "active"
            dependencies = raw_value.get("dependencies")
            if not isinstance(dependencies, list):
                dependencies = []
            dep_clean = []
            for dep in dependencies[: cls.MAX_PLOT_DEPENDENCIES]:
                dep_text = " ".join(str(dep or "").strip().split())[:120]
                if dep_text:
                    dep_clean.append(dep_text)
            raw_player_slugs = raw_value.get("player_slugs")
            if isinstance(raw_player_slugs, str):
                raw_player_slugs = [raw_player_slugs]
            player_slugs: List[str] = []
            if isinstance(raw_player_slugs, list):
                seen_player_slugs: set[str] = set()
                for item in raw_player_slugs[:8]:
                    slug = cls._player_slug_key(item)
                    if not slug or slug in seen_player_slugs:
                        continue
                    seen_player_slugs.add(slug)
                    player_slugs.append(slug)
            raw_visible_player_slugs = raw_value.get("visible_player_slugs")
            if isinstance(raw_visible_player_slugs, str):
                raw_visible_player_slugs = [raw_visible_player_slugs]
            visible_player_slugs: List[str] = []
            if isinstance(raw_visible_player_slugs, list):
                seen_visible_player_slugs: set[str] = set()
                for item in raw_visible_player_slugs[:8]:
                    slug = cls._player_slug_key(item)
                    if not slug or slug in seen_visible_player_slugs:
                        continue
                    seen_visible_player_slugs.add(slug)
                    visible_player_slugs.append(slug)
            raw_visible_user_ids = raw_value.get("visible_user_ids")
            if isinstance(raw_visible_user_ids, (str, int)):
                raw_visible_user_ids = [raw_visible_user_ids]
            visible_user_ids: List[int] = []
            if isinstance(raw_visible_user_ids, list):
                seen_visible_user_ids: set[int] = set()
                for item in raw_visible_user_ids[:8]:
                    try:
                        user_id = int(item)
                    except (TypeError, ValueError):
                        continue
                    if user_id <= 0 or user_id in seen_visible_user_ids:
                        continue
                    seen_visible_user_ids.add(user_id)
                    visible_user_ids.append(user_id)
            raw_npc_slugs = raw_value.get("npc_slugs")
            if isinstance(raw_npc_slugs, str):
                raw_npc_slugs = [raw_npc_slugs]
            npc_slugs: List[str] = []
            if isinstance(raw_npc_slugs, list):
                seen_npc_slugs: set[str] = set()
                for item in raw_npc_slugs[:12]:
                    slug = str(item or "").strip()
                    if not slug or slug in seen_npc_slugs:
                        continue
                    seen_npc_slugs.add(slug)
                    npc_slugs.append(slug)
            raw_aware_npc_slugs = raw_value.get("aware_npc_slugs")
            if isinstance(raw_aware_npc_slugs, str):
                raw_aware_npc_slugs = [raw_aware_npc_slugs]
            aware_npc_slugs: List[str] = []
            if isinstance(raw_aware_npc_slugs, list):
                seen_aware_npc_slugs: set[str] = set()
                for item in raw_aware_npc_slugs[:12]:
                    slug = str(item or "").strip()
                    if not slug or slug in seen_aware_npc_slugs:
                        continue
                    seen_aware_npc_slugs.add(slug)
                    aware_npc_slugs.append(slug)
            visibility = str(raw_value.get("visibility") or "").strip().lower()
            if visibility not in {"public", "private", "limited", "local"}:
                visibility = ""
            target_turns = cls._coerce_non_negative_int(
                raw_value.get("target_turns", 0), default=0
            )
            if target_turns <= 0:
                target_turns = 8
            threads[thread_key] = {
                "thread": thread_key,
                "setup": str(raw_value.get("setup") or "").strip()[:260],
                "intended_payoff": str(raw_value.get("intended_payoff") or "").strip()[
                    :260
                ],
                "target_turns": min(250, max(1, target_turns)),
                "dependencies": dep_clean,
                "player_slugs": player_slugs,
                "npc_slugs": npc_slugs,
                "visibility": visibility,
                "visible_player_slugs": visible_player_slugs,
                "visible_user_ids": visible_user_ids,
                "aware_npc_slugs": aware_npc_slugs,
                "location_key": str(raw_value.get("location_key") or "").strip()[:160],
                "hint": str(raw_value.get("hint") or "").strip()[:220],
                "status": status,
                "resolution": str(raw_value.get("resolution") or "").strip()[:260],
                "created_turn": cls._coerce_non_negative_int(
                    raw_value.get("created_turn", 0), default=0
                ),
                "updated_turn": cls._coerce_non_negative_int(
                    raw_value.get("updated_turn", 0), default=0
                ),
            }
        return threads

    @classmethod
    def _plot_threads_for_prompt(
        cls,
        campaign_state: Dict[str, object],
        *,
        campaign: Optional[ZorkCampaign] = None,
        viewer_user_id: int = 0,
        viewer_player_slug: str = "",
        viewer_location_key: str = "",
        active_scene_npc_slugs: Optional[set[str]] = None,
        limit: int = 10,
    ) -> List[Dict[str, object]]:
        threads = cls._plot_threads_from_state(campaign_state)
        rows = list(threads.values())
        rows.sort(
            key=lambda row: (
                0 if str(row.get("status")) == "active" else 1,
                -cls._coerce_non_negative_int(row.get("updated_turn", 0), default=0),
                str(row.get("thread") or ""),
            )
        )
        out = []
        for row in rows:
            if not cls._plot_thread_visible_to_viewer(
                row,
                campaign=campaign,
                viewer_user_id=viewer_user_id,
                viewer_player_slug=viewer_player_slug,
                viewer_location_key=viewer_location_key,
                active_scene_npc_slugs=active_scene_npc_slugs or set(),
            ):
                continue
            out.append(
                {
                    "thread": row.get("thread"),
                    "setup": row.get("setup"),
                    "intended_payoff": row.get("intended_payoff"),
                    "target_turns": row.get("target_turns"),
                    "dependencies": list(row.get("dependencies") or []),
                    "hint": row.get("hint"),
                    "visibility": row.get("visibility"),
                    "visible_player_slugs": list(row.get("visible_player_slugs") or []),
                    "visible_user_ids": list(row.get("visible_user_ids") or []),
                    "aware_npc_slugs": list(row.get("aware_npc_slugs") or []),
                    "location_key": row.get("location_key"),
                    "status": row.get("status"),
                    "resolution": row.get("resolution"),
                }
            )
            if len(out) >= max(1, int(limit or 10)):
                break
        return out

    @classmethod
    def _plot_thread_visibility_descriptor(
        cls,
        row: Dict[str, object],
        *,
        campaign: Optional[ZorkCampaign] = None,
    ) -> Tuple[str, set[int], set[str], set[str], str]:
        scope = str(row.get("visibility") or "").strip().lower()
        raw_user_ids = row.get("visible_user_ids")
        if isinstance(raw_user_ids, (str, int)):
            raw_user_ids = [raw_user_ids]
        visible_user_ids: set[int] = set()
        if isinstance(raw_user_ids, list):
            for item in raw_user_ids[:8]:
                try:
                    user_id = int(item)
                except (TypeError, ValueError):
                    continue
                if user_id > 0:
                    visible_user_ids.add(user_id)
        explicit_visible_player_slugs = {
            cls._player_slug_key(item)
            for item in list(row.get("visible_player_slugs") or [])
            if cls._player_slug_key(item)
        }
        explicit_aware_npc_slugs = {
            str(item or "").strip()
            for item in list(row.get("aware_npc_slugs") or [])
            if str(item or "").strip()
        }
        explicit_player_slugs = {
            cls._player_slug_key(item)
            for item in list(row.get("player_slugs") or [])
            if cls._player_slug_key(item)
        }
        explicit_npc_slugs = {
            str(item or "").strip()
            for item in list(row.get("npc_slugs") or [])
            if str(item or "").strip()
        }
        location_key = str(row.get("location_key") or "").strip().lower()
        haystack = " ".join(
            str(row.get(field) or "").strip().lower()
            for field in ("thread", "setup", "intended_payoff", "resolution", "hint")
        )
        haystack += " " + " ".join(
            str(item or "").strip().lower() for item in list(row.get("dependencies") or [])
        )
        inferred_player_slugs: set[str] = set()
        inferred_npc_slugs: set[str] = set()
        if campaign is not None:
            registry = cls._campaign_player_registry(campaign.id)
            for entry in registry.get("by_user_id", {}).values():
                if not isinstance(entry, dict):
                    continue
                slug = cls._player_slug_key(entry.get("slug") or entry.get("name") or "")
                if not slug:
                    continue
                aliases = {
                    slug,
                    " ".join(str(entry.get("name") or "").strip().lower().split()),
                }
                name = str(entry.get("name") or "").strip()
                for sep in (",", "(", " - "):
                    if sep in name:
                        aliases.add(" ".join(name.split(sep, 1)[0].strip().lower().split()))
                if any(alias and alias in haystack for alias in aliases):
                    inferred_player_slugs.add(slug)
            characters = cls.get_campaign_characters(campaign)
            if isinstance(characters, dict):
                for slug, char in characters.items():
                    aliases = {
                        str(slug or "").strip().lower(),
                        " ".join(str((char or {}).get("name") or "").strip().lower().split()),
                    }
                    if any(alias and alias in haystack for alias in aliases):
                        inferred_npc_slugs.add(str(slug))
        visible_player_slugs = explicit_visible_player_slugs or explicit_player_slugs or inferred_player_slugs
        aware_npc_slugs = explicit_aware_npc_slugs or explicit_npc_slugs or inferred_npc_slugs
        if scope not in {"public", "private", "limited", "local"}:
            if visible_player_slugs:
                scope = "private" if len(visible_player_slugs) <= 1 else "limited"
            elif location_key:
                scope = "local"
            else:
                scope = "public"
        return scope, visible_user_ids, visible_player_slugs, aware_npc_slugs, location_key

    @classmethod
    def _plot_thread_visible_to_viewer(
        cls,
        row: Dict[str, object],
        *,
        campaign: Optional[ZorkCampaign],
        viewer_user_id: int,
        viewer_player_slug: str,
        viewer_location_key: str,
        active_scene_npc_slugs: set[str],
    ) -> bool:
        (
            scope,
            visible_user_ids,
            visible_player_slugs,
            aware_npc_slugs,
            location_key,
        ) = cls._plot_thread_visibility_descriptor(
            row,
            campaign=campaign,
        )
        viewer_slug = cls._player_slug_key(viewer_player_slug)
        viewer_loc = str(viewer_location_key or "").strip().lower()
        viewer_allowed = bool(
            (viewer_user_id > 0 and viewer_user_id in visible_user_ids)
            or (viewer_slug and viewer_slug in visible_player_slugs)
        )
        if scope in {"private", "limited"}:
            return viewer_allowed
        if scope == "local":
            if viewer_allowed:
                return True
            return bool(location_key and viewer_loc and location_key == viewer_loc)
        if scope == "public":
            return True
        if viewer_allowed:
            return True
        if location_key and viewer_loc and location_key == viewer_loc:
            return True
        if aware_npc_slugs and active_scene_npc_slugs:
            return bool(aware_npc_slugs.intersection(active_scene_npc_slugs))
        return True

    @classmethod
    def _plot_hints_for_viewer(
        cls,
        campaign: ZorkCampaign,
        campaign_state: Dict[str, object],
        *,
        viewer_user_id: int,
        viewer_player_slug: str,
        viewer_location_key: str,
        active_scene_npc_slugs: set[str],
        limit: int = 3,
    ) -> List[Dict[str, object]]:
        rows = cls._plot_threads_for_prompt(
            campaign_state,
            campaign=campaign,
            viewer_user_id=viewer_user_id,
            viewer_player_slug=viewer_player_slug,
            viewer_location_key=viewer_location_key,
            active_scene_npc_slugs=active_scene_npc_slugs,
            limit=max(limit * 2, 6),
        )
        active_rows = [row for row in rows if str(row.get("status")) == "active"]
        hints: List[Dict[str, object]] = []
        for row in active_rows[: max(1, int(limit or 3))]:
            hint_text = " ".join(str(row.get("hint") or "").split()).strip()
            if not hint_text:
                hint_text = " ".join(str(row.get("intended_payoff") or "").split()).strip()
            if not hint_text:
                hint_text = " ".join(str(row.get("setup") or "").split()).strip()
            hint_text = hint_text[:220]
            if not hint_text:
                continue
            hints.append(
                {
                    "thread": row.get("thread"),
                    "hint": hint_text,
                    "visibility": row.get("visibility"),
                    "status": row.get("status"),
                }
            )
        return hints

    @classmethod
    def _apply_plot_plan_tool(
        cls,
        campaign_state: Dict[str, object],
        payload: Dict[str, object],
        *,
        current_turn: int = 0,
    ) -> Dict[str, object]:
        threads = cls._plot_threads_from_state(campaign_state)
        raw_plans = payload.get("plans")
        if isinstance(raw_plans, dict):
            raw_plans = [raw_plans]
        if not isinstance(raw_plans, list):
            raw_plans = []
        updated = 0
        removed = 0

        for raw_plan in raw_plans[:12]:
            if not isinstance(raw_plan, dict):
                continue
            thread_key = cls._plot_thread_key(
                raw_plan.get("thread") or raw_plan.get("slug")
            )
            if not thread_key:
                continue
            delete_requested = bool(
                raw_plan.get("remove")
                or raw_plan.get("delete")
                or raw_plan.get("_delete")
            )
            if delete_requested:
                if thread_key in threads:
                    threads.pop(thread_key, None)
                    removed += 1
                continue

            row = dict(
                threads.get(
                    thread_key,
                    {
                        "thread": thread_key,
                        "setup": "",
                        "intended_payoff": "",
                        "target_turns": 8,
                        "dependencies": [],
                        "player_slugs": [],
                        "npc_slugs": [],
                        "visibility": "",
                        "visible_player_slugs": [],
                        "visible_user_ids": [],
                        "aware_npc_slugs": [],
                        "location_key": "",
                        "hint": "",
                        "status": "active",
                        "resolution": "",
                        "created_turn": max(0, int(current_turn or 0)),
                        "updated_turn": max(0, int(current_turn or 0)),
                    },
                )
            )
            for field in ("setup", "intended_payoff", "resolution"):
                if field in raw_plan and raw_plan.get(field) is not None:
                    row[field] = " ".join(
                        str(raw_plan.get(field) or "").strip().split()
                    )[:260]

            if "target_turns" in raw_plan:
                target_turns = cls._coerce_non_negative_int(
                    raw_plan.get("target_turns", row.get("target_turns", 8)), default=8
                )
                row["target_turns"] = min(250, max(1, target_turns))

            raw_deps = raw_plan.get("dependencies")
            if isinstance(raw_deps, list):
                dep_clean = []
                for dep in raw_deps[: cls.MAX_PLOT_DEPENDENCIES]:
                    dep_text = " ".join(str(dep or "").strip().split())[:120]
                    if dep_text:
                        dep_clean.append(dep_text)
                row["dependencies"] = dep_clean

            if "visibility" in raw_plan:
                visibility = str(raw_plan.get("visibility") or "").strip().lower()
                if visibility in {"public", "private", "limited", "local"}:
                    row["visibility"] = visibility

            if "visible_user_ids" in raw_plan:
                raw_visible_user_ids = raw_plan.get("visible_user_ids")
                if isinstance(raw_visible_user_ids, (str, int)):
                    raw_visible_user_ids = [raw_visible_user_ids]
                cleaned_visible_user_ids = []
                seen_visible_user_ids: set[int] = set()
                if isinstance(raw_visible_user_ids, list):
                    for item in raw_visible_user_ids[:8]:
                        try:
                            user_id = int(item)
                        except (TypeError, ValueError):
                            continue
                        if user_id <= 0 or user_id in seen_visible_user_ids:
                            continue
                        seen_visible_user_ids.add(user_id)
                        cleaned_visible_user_ids.append(user_id)
                row["visible_user_ids"] = cleaned_visible_user_ids

            if "player_slugs" in raw_plan:
                raw_player_slugs = raw_plan.get("player_slugs")
                if isinstance(raw_player_slugs, str):
                    raw_player_slugs = [raw_player_slugs]
                cleaned_player_slugs = []
                seen_player_slugs: set[str] = set()
                if isinstance(raw_player_slugs, list):
                    for item in raw_player_slugs[:8]:
                        slug = cls._player_slug_key(item)
                        if not slug or slug in seen_player_slugs:
                            continue
                        seen_player_slugs.add(slug)
                        cleaned_player_slugs.append(slug)
                row["player_slugs"] = cleaned_player_slugs

            if "visible_player_slugs" in raw_plan:
                raw_visible_player_slugs = raw_plan.get("visible_player_slugs")
                if isinstance(raw_visible_player_slugs, str):
                    raw_visible_player_slugs = [raw_visible_player_slugs]
                cleaned_visible_player_slugs = []
                seen_visible_player_slugs: set[str] = set()
                if isinstance(raw_visible_player_slugs, list):
                    for item in raw_visible_player_slugs[:8]:
                        slug = cls._player_slug_key(item)
                        if not slug or slug in seen_visible_player_slugs:
                            continue
                        seen_visible_player_slugs.add(slug)
                        cleaned_visible_player_slugs.append(slug)
                row["visible_player_slugs"] = cleaned_visible_player_slugs

            if "npc_slugs" in raw_plan:
                raw_npc_slugs = raw_plan.get("npc_slugs")
                if isinstance(raw_npc_slugs, str):
                    raw_npc_slugs = [raw_npc_slugs]
                cleaned_npc_slugs = []
                seen_npc_slugs: set[str] = set()
                if isinstance(raw_npc_slugs, list):
                    for item in raw_npc_slugs[:12]:
                        slug = str(item or "").strip()
                        if not slug or slug in seen_npc_slugs:
                            continue
                        seen_npc_slugs.add(slug)
                        cleaned_npc_slugs.append(slug)
                row["npc_slugs"] = cleaned_npc_slugs

            if "aware_npc_slugs" in raw_plan:
                raw_aware_npc_slugs = raw_plan.get("aware_npc_slugs")
                if isinstance(raw_aware_npc_slugs, str):
                    raw_aware_npc_slugs = [raw_aware_npc_slugs]
                cleaned_aware_npc_slugs = []
                seen_aware_npc_slugs: set[str] = set()
                if isinstance(raw_aware_npc_slugs, list):
                    for item in raw_aware_npc_slugs[:12]:
                        slug = str(item or "").strip()
                        if not slug or slug in seen_aware_npc_slugs:
                            continue
                        seen_aware_npc_slugs.add(slug)
                        cleaned_aware_npc_slugs.append(slug)
                row["aware_npc_slugs"] = cleaned_aware_npc_slugs

            if "location_key" in raw_plan:
                row["location_key"] = str(raw_plan.get("location_key") or "").strip()[:160]

            if "hint" in raw_plan:
                row["hint"] = " ".join(str(raw_plan.get("hint") or "").strip().split())[:220]

            status = str(raw_plan.get("status") or row.get("status") or "active").strip().lower()
            if raw_plan.get("resolve"):
                status = "resolved"
            if status not in {"active", "resolved"}:
                status = "active"
            row["status"] = status

            if row.get("status") == "resolved" and not row.get("resolution"):
                row["resolution"] = "resolved"
            if row.get("status") != "resolved":
                row["resolution"] = str(row.get("resolution") or "")[:260]

            row["updated_turn"] = max(0, int(current_turn or 0))
            if cls._coerce_non_negative_int(row.get("created_turn", 0), default=0) <= 0:
                row["created_turn"] = max(0, int(current_turn or 0))
            threads[thread_key] = row
            updated += 1

        if len(threads) > cls.MAX_PLOT_THREADS:
            ranked = sorted(
                threads.items(),
                key=lambda kv: (
                    0 if str(kv[1].get("status")) == "active" else 1,
                    -cls._coerce_non_negative_int(kv[1].get("updated_turn", 0), default=0),
                    kv[0],
                ),
            )
            threads = dict(ranked[: cls.MAX_PLOT_THREADS])

        campaign_state[cls.PLOT_THREADS_STATE_KEY] = threads
        active_threads = [
            row
            for row in cls._plot_threads_for_prompt(campaign_state, limit=12)
            if str(row.get("status")) == "active"
        ]
        return {
            "updated": updated,
            "removed": removed,
            "total": len(threads),
            "active": active_threads,
        }

    @classmethod
    def _chapter_slug_key(cls, value: object) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip().lower())
        if not text:
            return ""
        return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:80]

    @classmethod
    def _chapter_plan_from_state(
        cls, campaign_state: Dict[str, object]
    ) -> Dict[str, Dict[str, object]]:
        raw = (
            campaign_state.get(cls.CHAPTER_PLAN_STATE_KEY)
            if isinstance(campaign_state, dict)
            else {}
        )
        if not isinstance(raw, dict):
            raw = {}
        chapters: Dict[str, Dict[str, object]] = {}
        for raw_slug, raw_entry in raw.items():
            if not isinstance(raw_entry, dict):
                continue
            slug = cls._chapter_slug_key(raw_entry.get("slug") or raw_slug)
            if not slug:
                continue
            scenes_raw = raw_entry.get("scenes")
            if not isinstance(scenes_raw, list):
                scenes_raw = []
            scenes = []
            for scene in scenes_raw[:20]:
                scene_slug = cls._chapter_slug_key(scene)
                if scene_slug:
                    scenes.append(scene_slug)
            current_scene = cls._chapter_slug_key(raw_entry.get("current_scene"))
            if not current_scene and scenes:
                current_scene = scenes[0]
            status = str(raw_entry.get("status") or "active").strip().lower()
            if status not in {"active", "resolved"}:
                status = "active"
            chapters[slug] = {
                "slug": slug,
                "title": " ".join(str(raw_entry.get("title") or slug).strip().split())[
                    :120
                ],
                "summary": str(raw_entry.get("summary") or "").strip()[:260],
                "scenes": scenes,
                "current_scene": current_scene,
                "status": status,
                "resolution": str(raw_entry.get("resolution") or "").strip()[:260],
                "created_turn": cls._coerce_non_negative_int(
                    raw_entry.get("created_turn", 0), default=0
                ),
                "updated_turn": cls._coerce_non_negative_int(
                    raw_entry.get("updated_turn", 0), default=0
                ),
            }
        return chapters

    @classmethod
    def _chapters_for_prompt(
        cls,
        campaign_state: Dict[str, object],
        *,
        active_only: bool = True,
        limit: int = 8,
    ) -> List[Dict[str, object]]:
        chapters = cls._chapter_plan_from_state(campaign_state)
        rows = list(chapters.values())
        if active_only:
            rows = [row for row in rows if str(row.get("status")) == "active"]
        rows.sort(
            key=lambda row: (
                0 if str(row.get("status")) == "active" else 1,
                -cls._coerce_non_negative_int(row.get("updated_turn", 0), default=0),
                str(row.get("slug") or ""),
            )
        )
        out = []
        for row in rows[: max(1, int(limit or 8))]:
            out.append(
                {
                    "slug": row.get("slug"),
                    "title": row.get("title"),
                    "summary": row.get("summary"),
                    "current_scene": row.get("current_scene"),
                    "scenes": list(row.get("scenes") or []),
                    "status": row.get("status"),
                    "resolution": row.get("resolution"),
                }
            )
        return out

    @classmethod
    def _apply_chapter_plan_tool(
        cls,
        campaign_state: Dict[str, object],
        payload: Dict[str, object],
        *,
        current_turn: int = 0,
        on_rails: bool = False,
    ) -> Dict[str, object]:
        if on_rails:
            return {"updated": 0, "ignored": True, "reason": "on_rails_enabled"}

        chapters = cls._chapter_plan_from_state(campaign_state)
        action = str(payload.get("action") or "create").strip().lower()
        changed = 0

        def _resolve_slug() -> str:
            chapter_ref = payload.get("chapter")
            if isinstance(chapter_ref, dict):
                return cls._chapter_slug_key(
                    chapter_ref.get("slug") or chapter_ref.get("title")
                )
            return cls._chapter_slug_key(payload.get("chapter") or payload.get("slug"))

        slug = _resolve_slug()
        chapter_payload = payload.get("chapter")
        if isinstance(chapter_payload, dict):
            if not slug:
                slug = cls._chapter_slug_key(
                    chapter_payload.get("slug") or chapter_payload.get("title")
                )
        if action in {"create", "update"}:
            if not slug:
                return {"updated": 0, "ignored": True, "reason": "missing_slug"}
            row = dict(
                chapters.get(
                    slug,
                    {
                        "slug": slug,
                        "title": slug,
                        "summary": "",
                        "scenes": [],
                        "current_scene": "",
                        "status": "active",
                        "resolution": "",
                        "created_turn": max(0, int(current_turn or 0)),
                        "updated_turn": max(0, int(current_turn or 0)),
                    },
                )
            )
            if isinstance(chapter_payload, dict):
                if chapter_payload.get("title") is not None:
                    row["title"] = " ".join(
                        str(chapter_payload.get("title") or "").strip().split()
                    )[:120] or row.get("title") or slug
                if chapter_payload.get("summary") is not None:
                    row["summary"] = str(chapter_payload.get("summary") or "").strip()[
                        :260
                    ]
                scenes_raw = chapter_payload.get("scenes")
                if isinstance(scenes_raw, list):
                    scenes = []
                    for scene in scenes_raw[:20]:
                        scene_slug = cls._chapter_slug_key(scene)
                        if scene_slug:
                            scenes.append(scene_slug)
                    row["scenes"] = scenes
                if chapter_payload.get("current_scene") is not None:
                    row["current_scene"] = cls._chapter_slug_key(
                        chapter_payload.get("current_scene")
                    )
                if chapter_payload.get("active") is not None:
                    row["status"] = (
                        "active" if bool(chapter_payload.get("active")) else "resolved"
                    )
            if not row.get("current_scene") and row.get("scenes"):
                row["current_scene"] = row["scenes"][0]
            row["updated_turn"] = max(0, int(current_turn or 0))
            if cls._coerce_non_negative_int(row.get("created_turn", 0), default=0) <= 0:
                row["created_turn"] = max(0, int(current_turn or 0))
            chapters[slug] = row
            changed += 1

        elif action == "advance_scene":
            if not slug or slug not in chapters:
                return {"updated": 0, "ignored": True, "reason": "chapter_not_found"}
            row = dict(chapters.get(slug) or {})
            to_scene = cls._chapter_slug_key(
                payload.get("to_scene") or payload.get("scene")
            )
            scenes = list(row.get("scenes") or [])
            if to_scene:
                if to_scene not in scenes:
                    scenes.append(to_scene)
                row["current_scene"] = to_scene
            elif scenes:
                current = cls._chapter_slug_key(row.get("current_scene"))
                try:
                    idx = scenes.index(current)
                except ValueError:
                    idx = -1
                next_idx = min(len(scenes) - 1, idx + 1)
                row["current_scene"] = scenes[next_idx]
            row["scenes"] = scenes[:20]
            row["status"] = "active"
            row["updated_turn"] = max(0, int(current_turn or 0))
            chapters[slug] = row
            changed += 1

        elif action in {"resolve", "close"}:
            if not slug or slug not in chapters:
                return {"updated": 0, "ignored": True, "reason": "chapter_not_found"}
            row = dict(chapters.get(slug) or {})
            row["status"] = "resolved"
            row["resolution"] = " ".join(
                str(payload.get("resolution") or row.get("resolution") or "").split()
            )[:260]
            row["updated_turn"] = max(0, int(current_turn or 0))
            chapters[slug] = row
            changed += 1

        if len(chapters) > cls.MAX_OFFRAILS_CHAPTERS:
            ranked = sorted(
                chapters.items(),
                key=lambda kv: (
                    0 if str(kv[1].get("status")) == "active" else 1,
                    -cls._coerce_non_negative_int(kv[1].get("updated_turn", 0), default=0),
                    kv[0],
                ),
            )
            chapters = dict(ranked[: cls.MAX_OFFRAILS_CHAPTERS])

        campaign_state[cls.CHAPTER_PLAN_STATE_KEY] = chapters
        active_chapters = cls._chapters_for_prompt(
            campaign_state, active_only=True, limit=10
        )
        return {
            "updated": changed,
            "ignored": False,
            "total": len(chapters),
            "active": active_chapters,
        }

    @classmethod
    def _consequence_id_key(cls, value: object) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip().lower())
        if not text:
            return ""
        return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:90]

    @classmethod
    def _consequences_from_state(
        cls, campaign_state: Dict[str, object]
    ) -> Dict[str, Dict[str, object]]:
        raw = (
            campaign_state.get(cls.CONSEQUENCE_STATE_KEY)
            if isinstance(campaign_state, dict)
            else {}
        )
        if not isinstance(raw, dict):
            raw = {}
        out: Dict[str, Dict[str, object]] = {}
        for raw_key, raw_entry in raw.items():
            if not isinstance(raw_entry, dict):
                continue
            cid = cls._consequence_id_key(raw_entry.get("id") or raw_key)
            if not cid:
                continue
            status = str(raw_entry.get("status") or "active").strip().lower()
            if status not in {"active", "resolved"}:
                status = "active"
            expires_at_turn = cls._coerce_non_negative_int(
                raw_entry.get("expires_at_turn", 0), default=0
            )
            out[cid] = {
                "id": cid,
                "trigger": str(raw_entry.get("trigger") or "").strip()[:240],
                "consequence": str(raw_entry.get("consequence") or "").strip()[:300],
                "severity": str(raw_entry.get("severity") or "low").strip().lower()[:24],
                "status": status,
                "created_turn": cls._coerce_non_negative_int(
                    raw_entry.get("created_turn", 0), default=0
                ),
                "updated_turn": cls._coerce_non_negative_int(
                    raw_entry.get("updated_turn", 0), default=0
                ),
                "expires_at_turn": expires_at_turn,
                "resolution": str(raw_entry.get("resolution") or "").strip()[:260],
            }
        return out

    @classmethod
    def _consequences_for_prompt(
        cls,
        campaign_state: Dict[str, object],
        *,
        current_turn: int = 0,
        limit: int = 12,
    ) -> List[Dict[str, object]]:
        rows = list(cls._consequences_from_state(campaign_state).values())
        active_rows = []
        turn_now = max(0, int(current_turn or 0))
        for row in rows:
            if str(row.get("status")) != "active":
                continue
            expires_at_turn = cls._coerce_non_negative_int(
                row.get("expires_at_turn", 0), default=0
            )
            if expires_at_turn > 0 and turn_now > 0 and expires_at_turn < turn_now:
                continue
            active_rows.append(row)
        active_rows.sort(
            key=lambda row: (
                {"critical": 0, "high": 1, "moderate": 2, "low": 3}.get(
                    str(row.get("severity") or "low"), 4
                ),
                cls._coerce_non_negative_int(row.get("expires_at_turn", 0), default=0)
                if cls._coerce_non_negative_int(row.get("expires_at_turn", 0), default=0) > 0
                else 10**9,
                -cls._coerce_non_negative_int(row.get("updated_turn", 0), default=0),
            )
        )
        out = []
        for row in active_rows[: max(1, int(limit or 12))]:
            out.append(
                {
                    "id": row.get("id"),
                    "trigger": row.get("trigger"),
                    "consequence": row.get("consequence"),
                    "severity": row.get("severity"),
                    "expires_at_turn": row.get("expires_at_turn"),
                }
            )
        return out

    @classmethod
    def _apply_consequence_log_tool(
        cls,
        campaign_state: Dict[str, object],
        payload: Dict[str, object],
        *,
        current_turn: int = 0,
    ) -> Dict[str, object]:
        rows = cls._consequences_from_state(campaign_state)
        turn_now = max(0, int(current_turn or 0))
        added = 0
        updated = 0
        resolved = 0
        removed = 0

        def _iter_entries(value: object) -> List[Dict[str, object]]:
            if isinstance(value, dict):
                return [value]
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
            return []

        for entry in _iter_entries(payload.get("add")):
            trigger = " ".join(str(entry.get("trigger") or "").strip().split())[:240]
            consequence = " ".join(
                str(entry.get("consequence") or "").strip().split()
            )[:300]
            if not trigger or not consequence:
                continue
            cid = cls._consequence_id_key(
                entry.get("id")
                or entry.get("slug")
                or trigger[:60]
            )
            if not cid:
                continue
            severity = str(entry.get("severity") or "low").strip().lower()
            if severity not in {"low", "moderate", "high", "critical"}:
                severity = "low"
            expires_turns = cls._coerce_non_negative_int(
                entry.get("expires_turns", 0), default=0
            )
            expires_at_turn = (turn_now + expires_turns) if expires_turns > 0 else 0
            row = dict(rows.get(cid) or {})
            is_new = not bool(row)
            row.update(
                {
                    "id": cid,
                    "trigger": trigger,
                    "consequence": consequence,
                    "severity": severity,
                    "status": "active",
                    "updated_turn": turn_now,
                    "expires_at_turn": expires_at_turn,
                    "resolution": str(row.get("resolution") or "")[:260],
                }
            )
            if is_new:
                row["created_turn"] = turn_now
                added += 1
            else:
                updated += 1
            rows[cid] = row

        for entry in _iter_entries(payload.get("resolve")):
            cid = cls._consequence_id_key(
                entry.get("id") or entry.get("slug") or entry.get("trigger")
            )
            if not cid or cid not in rows:
                continue
            row = dict(rows.get(cid) or {})
            row["status"] = "resolved"
            row["updated_turn"] = turn_now
            row["resolution"] = " ".join(
                str(entry.get("resolution") or row.get("resolution") or "resolved")
                .strip()
                .split()
            )[:260]
            rows[cid] = row
            resolved += 1

        remove_keys = payload.get("remove")
        if isinstance(remove_keys, list):
            for raw_key in remove_keys:
                cid = cls._consequence_id_key(raw_key)
                if cid and cid in rows:
                    rows.pop(cid, None)
                    removed += 1

        for cid, row in list(rows.items()):
            expires_at_turn = cls._coerce_non_negative_int(
                row.get("expires_at_turn", 0), default=0
            )
            if (
                expires_at_turn > 0
                and turn_now > 0
                and turn_now > expires_at_turn
                and str(row.get("status")) == "active"
            ):
                rows.pop(cid, None)
                removed += 1

        if len(rows) > cls.MAX_CONSEQUENCES:
            ranked = sorted(
                rows.items(),
                key=lambda kv: (
                    0 if str(kv[1].get("status")) == "active" else 1,
                    -cls._coerce_non_negative_int(kv[1].get("updated_turn", 0), default=0),
                    kv[0],
                ),
            )
            rows = dict(ranked[: cls.MAX_CONSEQUENCES])

        campaign_state[cls.CONSEQUENCE_STATE_KEY] = rows
        active = cls._consequences_for_prompt(
            campaign_state, current_turn=turn_now, limit=12
        )
        return {
            "added": added,
            "updated": updated,
            "resolved": resolved,
            "removed": removed,
            "total": len(rows),
            "active": active,
        }

    @classmethod
    def _sms_normalize_thread_key(cls, value: object) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip().lower())
        if not text:
            return ""
        return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:80]

    @classmethod
    def _sms_threads_from_state(cls, campaign_state: Dict[str, object]) -> Dict[str, dict]:
        raw = campaign_state.get(cls.SMS_STATE_KEY) if isinstance(campaign_state, dict) else {}
        if not isinstance(raw, dict):
            raw = {}
        threads: Dict[str, dict] = {}
        for raw_key, raw_value in raw.items():
            key = cls._sms_normalize_thread_key(raw_key)
            if not key or not isinstance(raw_value, dict):
                continue
            label = str(raw_value.get("label") or raw_key).strip()[:80] or key
            raw_messages = raw_value.get("messages")
            if not isinstance(raw_messages, list):
                raw_messages = []
            messages = []
            for msg in raw_messages[-cls.SMS_MAX_MESSAGES_PER_THREAD :]:
                if not isinstance(msg, dict):
                    continue
                text = str(msg.get("message") or "").strip()
                if not text:
                    continue
                messages.append(
                    {
                        "from": str(msg.get("from") or "Unknown")[:80],
                        "to": str(msg.get("to") or "")[:80],
                        "message": text[:500],
                        "day": cls._coerce_non_negative_int(msg.get("day", 1), default=1) or 1,
                        "hour": min(
                            23,
                            max(0, cls._coerce_non_negative_int(msg.get("hour", 0), default=0)),
                        ),
                        "minute": min(
                            59,
                            max(0, cls._coerce_non_negative_int(msg.get("minute", 0), default=0)),
                        ),
                        "turn_id": cls._coerce_non_negative_int(msg.get("turn_id", 0), default=0),
                        "seq": cls._coerce_non_negative_int(msg.get("seq", 0), default=0),
                    }
                )
            threads[key] = {"label": label, "messages": messages}
        return threads

    @classmethod
    def _sms_list_threads(
        cls,
        campaign_state: Dict[str, object],
        wildcard: str = "*",
        limit: int = 20,
    ) -> List[Dict[str, object]]:
        threads = cls._sms_threads_from_state(campaign_state)
        pattern = str(wildcard or "*").strip().lower() or "*"
        out: List[Dict[str, object]] = []
        for key in reversed(list(threads.keys())):
            row = threads.get(key) or {}
            label = str(row.get("label") or key)
            if pattern != "*":
                if not fnmatch.fnmatch(key, pattern) and not fnmatch.fnmatch(
                    label.lower(), pattern
                ):
                    continue
            messages = row.get("messages")
            if not isinstance(messages, list):
                messages = []
            last = messages[-1] if messages else {}
            preview = str(last.get("message") or "").strip()
            if len(preview) > cls.SMS_MAX_PREVIEW_CHARS:
                preview = preview[: cls.SMS_MAX_PREVIEW_CHARS - 1].rstrip() + "…"
            out.append(
                {
                    "thread": key,
                    "label": label,
                    "count": len(messages),
                    "last_from": str(last.get("from") or ""),
                    "last_preview": preview,
                    "day": cls._coerce_non_negative_int(last.get("day", 0), default=0),
                    "hour": cls._coerce_non_negative_int(last.get("hour", 0), default=0),
                    "minute": cls._coerce_non_negative_int(last.get("minute", 0), default=0),
                }
            )
            if len(out) >= max(1, int(limit or 20)):
                break
        return out

    @classmethod
    def _sms_read_thread(
        cls,
        campaign_state: Dict[str, object],
        thread: str,
        limit: int = 20,
    ) -> Tuple[Optional[str], Optional[str], List[Dict[str, object]]]:
        threads = cls._sms_threads_from_state(campaign_state)
        if not threads:
            return None, None, []
        query_key = cls._sms_normalize_thread_key(thread)
        selected_key = query_key if query_key in threads else None
        if selected_key is None and query_key:
            for key in threads.keys():
                key_norm = cls._sms_normalize_thread_key(key)
                if query_key in key_norm:
                    selected_key = key
                    break
        if selected_key is None and not query_key:
            return None, None, []

        def _thread_matches(key: str, row: Dict[str, object]) -> bool:
            if not query_key:
                return False
            key_norm = cls._sms_normalize_thread_key(key)
            label_norm = cls._sms_normalize_thread_key(row.get("label"))
            if query_key and (
                query_key == key_norm
                or query_key in key_norm
                or query_key == label_norm
                or query_key in label_norm
            ):
                return True
            raw_messages = row.get("messages")
            if not isinstance(raw_messages, list):
                raw_messages = []
            for msg in raw_messages:
                if not isinstance(msg, dict):
                    continue
                from_norm = cls._sms_normalize_thread_key(msg.get("from"))
                to_norm = cls._sms_normalize_thread_key(msg.get("to"))
                if (from_norm and query_key in from_norm) or (to_norm and query_key in to_norm):
                    return True
            return False

        matched_keys: List[str] = []
        if selected_key is not None:
            matched_keys.append(selected_key)
        for key, row in threads.items():
            if key in matched_keys or not isinstance(row, dict):
                continue
            if _thread_matches(key, row):
                matched_keys.append(key)
        if not matched_keys:
            return None, None, []

        merged_messages: List[Dict[str, object]] = []
        for key in matched_keys:
            row = threads.get(key) or {}
            messages = row.get("messages")
            if not isinstance(messages, list):
                messages = []
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                enriched = dict(msg)
                enriched["thread"] = key
                merged_messages.append(enriched)

        merged_messages.sort(
            key=lambda msg: (
                cls._coerce_non_negative_int(msg.get("day", 0), default=0),
                cls._coerce_non_negative_int(msg.get("hour", 0), default=0),
                cls._coerce_non_negative_int(msg.get("minute", 0), default=0),
                cls._coerce_non_negative_int(msg.get("turn_id", 0), default=0),
            )
        )
        capped = merged_messages[-max(1, min(40, int(limit or 20))) :]

        canonical_key = selected_key or query_key or matched_keys[0]
        first_row = threads.get(matched_keys[0]) or {}
        base_label = str(first_row.get("label") or matched_keys[0])
        if len(matched_keys) <= 1:
            resolved_label = base_label
        else:
            resolved_label = f"{base_label} (+{len(matched_keys) - 1} related thread(s))"
        return canonical_key, resolved_label, list(capped)

    @classmethod
    def _sms_actor_key(cls, actor_id: object) -> str:
        key = cls._sms_normalize_thread_key(f"actor-{actor_id}")
        return key or "actor-unknown"

    @classmethod
    def _sms_player_aliases(
        cls,
        *,
        actor_id: object,
        player_state: Dict[str, object] | None,
    ) -> set[str]:
        aliases: set[str] = set()

        def _add(raw: object) -> None:
            text = str(raw or "").strip()
            if not text:
                return
            norm = cls._sms_normalize_thread_key(text)
            if norm:
                aliases.add(norm)

        actor_text = str(actor_id or "").strip()
        _add(actor_text)
        if actor_text:
            _add(f"<@{actor_text}>")
            _add(f"<@!{actor_text}>")
            _add(f"player {actor_text}")
        if isinstance(player_state, dict):
            char_name = str(player_state.get("character_name") or "").strip()
            _add(char_name)
            for token in re.split(r"[\s\-]+", char_name):
                if len(token) >= 3:
                    _add(token)
        return aliases

    @classmethod
    def _sms_read_state_from_campaign_state(
        cls,
        campaign_state: Dict[str, object],
    ) -> Dict[str, Dict[str, object]]:
        raw = campaign_state.get(cls.SMS_READ_STATE_KEY) if isinstance(campaign_state, dict) else {}
        if not isinstance(raw, dict):
            return {}
        out: Dict[str, Dict[str, object]] = {}
        for raw_actor_key, raw_row in raw.items():
            actor_key = cls._sms_normalize_thread_key(raw_actor_key)
            if not actor_key or not isinstance(raw_row, dict):
                continue
            row_threads = raw_row.get("threads")
            if not isinstance(row_threads, dict):
                row_threads = {}
            cleaned_threads: Dict[str, int] = {}
            for raw_thread_key, raw_marker in row_threads.items():
                thread_key = cls._sms_normalize_thread_key(raw_thread_key)
                if not thread_key:
                    continue
                marker = cls._coerce_non_negative_int(raw_marker, default=0)
                cleaned_threads[thread_key] = marker
            out[actor_key] = {
                "threads": cleaned_threads,
                "last_notified_abs_hour": cls._coerce_non_negative_int(
                    raw_row.get("last_notified_abs_hour", -1),
                    default=-1,
                ),
            }
        return out

    @classmethod
    def _sms_mark_threads_read(
        cls,
        campaign_state: Dict[str, object],
        *,
        actor_id: object,
        player_state: Dict[str, object] | None,
        thread_markers: Dict[str, int],
    ) -> bool:
        if not isinstance(campaign_state, dict):
            return False
        if not isinstance(thread_markers, dict) or not thread_markers:
            return False
        actor_key = cls._sms_actor_key(actor_id)
        state = cls._sms_read_state_from_campaign_state(campaign_state)
        row = dict(state.get(actor_key) or {})
        threads = row.get("threads")
        if not isinstance(threads, dict):
            threads = {}
        changed = False
        for raw_thread_key, raw_marker in thread_markers.items():
            thread_key = cls._sms_normalize_thread_key(raw_thread_key)
            if not thread_key:
                continue
            marker = cls._coerce_non_negative_int(raw_marker, default=0)
            if marker <= 0:
                continue
            current = cls._coerce_non_negative_int(threads.get(thread_key, 0), default=0)
            if marker > current:
                threads[thread_key] = marker
                changed = True
        if not changed:
            return False
        row["threads"] = threads
        state[actor_key] = row
        campaign_state[cls.SMS_READ_STATE_KEY] = state
        return True

    @classmethod
    def _sms_unread_summary_for_player(
        cls,
        campaign_state: Dict[str, object],
        *,
        actor_id: object,
        player_state: Dict[str, object] | None,
    ) -> Dict[str, object]:
        aliases = cls._sms_player_aliases(actor_id=actor_id, player_state=player_state)
        if not aliases:
            return {"messages": 0, "threads": 0, "labels": []}
        actor_key = cls._sms_actor_key(actor_id)
        read_state = cls._sms_read_state_from_campaign_state(campaign_state)
        actor_row = read_state.get(actor_key) or {}
        read_threads = actor_row.get("threads")
        if not isinstance(read_threads, dict):
            read_threads = {}
        threads = cls._sms_threads_from_state(campaign_state)
        unread_messages = 0
        unread_threads = 0
        labels: List[str] = []
        unread_thread_markers: Dict[str, int] = {}
        for thread_key, row in threads.items():
            if not isinstance(row, dict):
                continue
            messages = row.get("messages")
            if not isinstance(messages, list):
                continue
            seen_marker = cls._coerce_non_negative_int(read_threads.get(thread_key, 0), default=0)
            thread_unread = 0
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                to_norm = cls._sms_normalize_thread_key(msg.get("to"))
                if not to_norm or to_norm not in aliases:
                    continue
                seq = cls._coerce_non_negative_int(msg.get("seq", 0), default=0)
                turn_id = cls._coerce_non_negative_int(msg.get("turn_id", 0), default=0)
                marker = seq if seq > 0 else turn_id
                if marker > seen_marker:
                    thread_unread += 1
                    prev_marker = cls._coerce_non_negative_int(
                        unread_thread_markers.get(thread_key, 0), default=0
                    )
                    if marker > prev_marker:
                        unread_thread_markers[thread_key] = marker
            if thread_unread <= 0:
                continue
            unread_messages += thread_unread
            unread_threads += 1
            label = str(row.get("label") or thread_key).strip()
            if label:
                labels.append(label[:40])
        deduped_labels: List[str] = []
        seen_labels: set[str] = set()
        for label in labels:
            key = cls._sms_normalize_thread_key(label)
            if not key or key in seen_labels:
                continue
            seen_labels.add(key)
            deduped_labels.append(label)
            if len(deduped_labels) >= 3:
                break
        return {
            "messages": unread_messages,
            "threads": unread_threads,
            "labels": deduped_labels,
            "thread_markers": unread_thread_markers,
            "last_notified_abs_hour": cls._coerce_non_negative_int(
                actor_row.get("last_notified_abs_hour", -1),
                default=-1,
            ),
        }

    @classmethod
    def _sms_unread_hourly_notification(
        cls,
        campaign_state: Dict[str, object],
        *,
        actor_id: object,
        player_state: Dict[str, object] | None,
        game_time: Dict[str, int] | None,
    ) -> Optional[str]:
        if not isinstance(campaign_state, dict):
            return None
        summary = cls._sms_unread_summary_for_player(
            campaign_state,
            actor_id=actor_id,
            player_state=player_state,
        )
        unread_messages = cls._coerce_non_negative_int(summary.get("messages", 0), default=0)
        unread_threads = cls._coerce_non_negative_int(summary.get("threads", 0), default=0)
        if unread_messages <= 0 or unread_threads <= 0:
            return None
        game_time_obj = game_time if isinstance(game_time, dict) else {}
        day = max(1, cls._coerce_non_negative_int(game_time_obj.get("day", 1), default=1))
        hour = min(
            23,
            max(0, cls._coerce_non_negative_int(game_time_obj.get("hour", 0), default=0)),
        )
        abs_hour = ((day - 1) * 24) + hour
        actor_key = cls._sms_actor_key(actor_id)
        read_state = cls._sms_read_state_from_campaign_state(campaign_state)
        row = dict(read_state.get(actor_key) or {})
        last_notified = cls._coerce_non_negative_int(
            row.get("last_notified_abs_hour", -1),
            default=-1,
        )
        if last_notified == abs_hour:
            return None
        row["last_notified_abs_hour"] = abs_hour
        read_state[actor_key] = row
        campaign_state[cls.SMS_READ_STATE_KEY] = read_state
        labels = summary.get("labels") if isinstance(summary.get("labels"), list) else []
        labels = [str(label).strip()[:40] for label in labels if str(label).strip()]
        if labels:
            suffix = f" ({', '.join(labels[:2])})"
        else:
            suffix = ""
        return (
            f"📨 Unread SMS: {unread_messages} message(s) in "
            f"{unread_threads} thread(s){suffix}."
        )

    _SMS_ARTICLES = frozenset({"the", "a", "an", "my"})

    @classmethod
    def _extract_inline_sms_intent(
        cls,
        action: str,
    ) -> Optional[Tuple[str, str]]:
        text = str(action or "").strip()
        if not text:
            return None
        # Pattern 1: Colon-delimited — "text the Doc: hello"
        m = re.match(
            r"^\s*(?:i\s+)?(?:send\s+)?(?:sms|text|message)\s+(?:to\s+)?([^:\n]{1,120})\s*:\s*(.+?)\s*$",
            text,
            flags=re.IGNORECASE,
        )
        if m:
            recipient = str(m.group(1) or "").strip().strip("\"'` ")
            message = str(m.group(2) or "").strip()
        else:
            # Pattern 2: Space-delimited — "text the Doc hello"
            # Consume articles before the recipient so "I SMS the Doc" captures "Doc".
            m = re.match(
                r"^\s*(?:i\s+)?(?:send\s+)?(?:sms|text|message)\s+(?:to\s+)?"
                r"(?:(?:the|a|an|my)\s+)?([^\s:\n]{1,80})\s+(.+?)\s*$",
                text,
                flags=re.IGNORECASE,
            )
            if not m:
                return None
            recipient = str(m.group(1) or "").strip().strip("\"'` ")
            message = str(m.group(2) or "").strip()
        if (
            len(message) >= 2
            and message[0] == message[-1]
            and message[0] in {'"', "'"}
        ):
            message = message[1:-1].strip()
        if not recipient or not message:
            return None
        # Bail out if recipient is still a bare article.
        if recipient.lower() in cls._SMS_ARTICLES:
            return None
        return recipient[:80], message[:500]

    @staticmethod
    def _is_private_phone_command_line(value: object) -> bool:
        text = " ".join(str(value or "").strip().split())
        if not text:
            return False
        return bool(
            re.match(
                r"^(?:i\s+)?(?:(?:send|check|read|open|view|look\s+at)\s+)?"
                r"(?:(?:my|the)\s+)?(?:phone|sms|texts?|messages?)\b",
                text,
                flags=re.IGNORECASE,
            )
            or re.match(
                r"^(?:i\s+)?(?:send\s+)?(?:sms|text|message)\b",
                text,
                flags=re.IGNORECASE,
            )
        )

    @classmethod
    def _redact_private_phone_command_lines(
        cls,
        text: str,
    ) -> Tuple[str, bool]:
        if not text:
            return "", False
        kept_lines: List[str] = []
        redacted = False
        for raw_line in str(text).splitlines():
            if cls._is_private_phone_command_line(raw_line):
                redacted = True
                continue
            kept_lines.append(raw_line)
        return "\n".join(kept_lines).strip(), redacted

    @classmethod
    def _force_private_visibility_for_phone_activity(
        cls,
        visibility: Dict[str, object],
        *,
        actor_slug: str,
        actor_user_id: Optional[int],
    ) -> Dict[str, object]:
        reason = cls._trim_text(str(visibility.get("reason") or "").strip(), 240)
        return {
            "scope": "private",
            "actor_player_slug": actor_slug or None,
            "actor_user_id": actor_user_id,
            "visible_player_slugs": [actor_slug] if actor_slug else [],
            "visible_user_ids": [actor_user_id] if actor_user_id is not None else [],
            "location_key": None,
            "aware_npc_slugs": [],
            "reason": reason or "Private phone/SMS activity is actor-only unless explicitly shared.",
            "source": "auto-private-phone",
        }

    @classmethod
    def _sms_write(
        cls,
        campaign_state: Dict[str, object],
        *,
        thread: str,
        sender: str,
        recipient: str,
        message: str,
        game_time: Dict[str, int],
        turn_id: int = 0,
    ) -> Tuple[str, str, Dict[str, object]]:
        threads = cls._sms_threads_from_state(campaign_state)
        thread_key = cls._sms_normalize_thread_key(thread or recipient or sender or "unknown")
        if not thread_key:
            thread_key = "unknown"
        existing = threads.pop(thread_key, {"label": thread or recipient or sender or thread_key, "messages": []})
        label = str(existing.get("label") or thread or recipient or sender or thread_key).strip()[:80] or thread_key
        messages = existing.get("messages")
        if not isinstance(messages, list):
            messages = []
        entry = {
            "from": str(sender or "Unknown")[:80],
            "to": str(recipient or "")[:80],
            "message": str(message or "").strip()[:500],
            "day": cls._coerce_non_negative_int(game_time.get("day", 1), default=1) or 1,
            "hour": min(23, max(0, cls._coerce_non_negative_int(game_time.get("hour", 0), default=0))),
            "minute": min(59, max(0, cls._coerce_non_negative_int(game_time.get("minute", 0), default=0))),
            "turn_id": max(0, int(turn_id or 0)),
            "seq": 0,
        }
        if messages:
            last = messages[-1]
            if isinstance(last, dict):
                if (
                    str(last.get("from") or "") == str(entry.get("from") or "")
                    and str(last.get("to") or "") == str(entry.get("to") or "")
                    and str(last.get("message") or "") == str(entry.get("message") or "")
                    and cls._coerce_non_negative_int(last.get("day", 0), default=0)
                    == cls._coerce_non_negative_int(entry.get("day", 0), default=0)
                    and cls._coerce_non_negative_int(last.get("hour", 0), default=0)
                    == cls._coerce_non_negative_int(entry.get("hour", 0), default=0)
                    and cls._coerce_non_negative_int(last.get("minute", 0), default=0)
                    == cls._coerce_non_negative_int(entry.get("minute", 0), default=0)
                ):
                    threads[thread_key] = {"label": label, "messages": messages}
                    campaign_state[cls.SMS_STATE_KEY] = threads
                    return thread_key, label, dict(last)
        next_seq = cls._coerce_non_negative_int(
            campaign_state.get(cls.SMS_MESSAGE_SEQ_KEY, 0), default=0
        ) + 1
        entry["seq"] = max(1, next_seq)
        messages.append(entry)
        messages = messages[-cls.SMS_MAX_MESSAGES_PER_THREAD :]
        threads[thread_key] = {"label": label, "messages": messages}
        while len(threads) > cls.SMS_MAX_THREADS:
            oldest_key = next(iter(threads))
            threads.pop(oldest_key, None)
        campaign_state[cls.SMS_STATE_KEY] = threads
        campaign_state[cls.SMS_MESSAGE_SEQ_KEY] = int(entry.get("seq", next_seq))
        return thread_key, label, entry

    @classmethod
    def _register_pending_sms_task(cls, campaign_id: int, task: asyncio.Task) -> None:
        bucket = cls._pending_sms_tasks.setdefault(campaign_id, set())
        bucket.add(task)

        def _cleanup(done_task: asyncio.Task):
            tasks = cls._pending_sms_tasks.get(campaign_id)
            if tasks is None:
                return
            tasks.discard(done_task)
            if not tasks:
                cls._pending_sms_tasks.pop(campaign_id, None)

        task.add_done_callback(_cleanup)

    @classmethod
    def cancel_pending_sms_deliveries(cls, campaign_id: int) -> int:
        tasks = cls._pending_sms_tasks.pop(campaign_id, set())
        cancelled = 0
        for task in list(tasks):
            if task is not None and not task.done():
                task.cancel()
                cancelled += 1
        return cancelled

    @classmethod
    def _schedule_sms_delivery(
        cls,
        campaign_id: int,
        delay_seconds: int,
        thread: str,
        sender: str,
        recipient: str,
        message: str,
    ) -> None:
        task = asyncio.create_task(
            cls._sms_delivery_task(
                campaign_id=campaign_id,
                delay_seconds=max(1, int(delay_seconds)),
                thread=thread,
                sender=sender,
                recipient=recipient,
                message=message,
            )
        )
        cls._register_pending_sms_task(campaign_id, task)

    @classmethod
    async def _sms_delivery_task(
        cls,
        *,
        campaign_id: int,
        delay_seconds: int,
        thread: str,
        sender: str,
        recipient: str,
        message: str,
    ) -> None:
        try:
            await asyncio.sleep(max(1, int(delay_seconds)))
        except asyncio.CancelledError:
            return

        app = AppConfig.get_flask()
        if app is None:
            return
        try:
            with app.app_context():
                campaign = ZorkCampaign.query.get(campaign_id)
                if campaign is None:
                    return
                campaign_state_sms = cls.get_campaign_state(campaign)
                game_time_sms = cls._extract_game_time_snapshot(campaign_state_sms)
                latest_turn = (
                    ZorkTurn.query.filter_by(campaign_id=campaign_id)
                    .order_by(ZorkTurn.id.desc())
                    .first()
                )
                turn_id = int(latest_turn.id) if latest_turn is not None else 0
                cls._sms_write(
                    campaign_state_sms,
                    thread=thread,
                    sender=sender,
                    recipient=recipient,
                    message=message,
                    game_time=game_time_sms,
                    turn_id=turn_id,
                )
                campaign.state_json = cls._dump_json(campaign_state_sms)
                campaign.updated = db.func.now()
                db.session.commit()
        except Exception:
            logger.exception(
                "Zork scheduled SMS delivery failed: campaign=%s thread=%r",
                campaign_id,
                thread,
            )

    @classmethod
    def _normalize_calendar_update(cls, raw: object) -> Optional[Dict[str, object]]:
        """Coerce model calendar_update payloads into canonical {"add": [...], "remove": [...]} form.

        Handles common model mistakes:
        - Flat event dict (missing add/remove wrapper)
        - Single dict in 'add' instead of a list
        - 'hours_remaining'/'days_remaining' echoed back instead of 'time_remaining'/'time_unit'
        """
        if not isinstance(raw, dict) or not raw:
            return None

        has_add = "add" in raw
        has_remove = "remove" in raw

        # --- Detect flat event dict (has 'name' but no add/remove) ---
        if not has_add and not has_remove and raw.get("name"):
            raw = {"add": [dict(raw)]}
            has_add = True

        result: Dict[str, object] = {}

        # --- Normalize 'add' ---
        if has_add:
            add_val = raw["add"]
            # Single dict instead of list
            if isinstance(add_val, dict):
                add_val = [add_val]
            if isinstance(add_val, list):
                normalized_add: List[Dict[str, object]] = []
                for entry in add_val:
                    if not isinstance(entry, dict):
                        continue
                    entry = dict(entry)  # shallow copy for mutation
                    # Accept 'hours_remaining' / 'days_remaining' as aliases
                    if "time_remaining" not in entry:
                        if "hours_remaining" in entry:
                            entry["time_remaining"] = entry.pop("hours_remaining")
                            entry.setdefault("time_unit", "hours")
                        elif "days_remaining" in entry:
                            entry["time_remaining"] = entry.pop("days_remaining")
                            entry.setdefault("time_unit", "days")
                    # Strip read-only prompt fields the model may echo back
                    for echo_key in ("status", "_status",
                                     "hours_remaining", "days_remaining",
                                     "_hours_until", "_days_until",
                                     "created_day", "created_hour"):
                        entry.pop(echo_key, None)
                    normalized_add.append(entry)
                result["add"] = normalized_add

        # --- Normalize 'remove' ---
        if has_remove:
            remove_val = raw["remove"]
            if isinstance(remove_val, str):
                remove_val = [remove_val]
            if isinstance(remove_val, list):
                result["remove"] = remove_val

        return result if result else None

    @classmethod
    def _apply_calendar_update(
        cls,
        campaign_state: Dict[str, object],
        calendar_update: dict,
        resolution_context: str = "",
        actor_user_id: Optional[int] = None,
    ) -> Dict[str, object]:
        """Process calendar add/remove ops and persist absolute fire_day entries."""
        if not isinstance(calendar_update, dict):
            return campaign_state
        calendar_update = cls._normalize_calendar_update(calendar_update)
        if calendar_update is None:
            return campaign_state
        calendar_raw = list(campaign_state.get("calendar") or [])
        game_time = campaign_state.get("game_time") or {}
        current_day = game_time.get("day", 1)
        current_hour = game_time.get("hour", 8)
        day_int = int(current_day) if isinstance(current_day, (int, float)) else 1
        hour_int = int(current_hour) if isinstance(current_hour, (int, float)) else 8
        calendar = []
        for event in calendar_raw:
            normalized = cls._calendar_normalize_event(
                event,
                current_day=day_int,
                current_hour=hour_int,
            )
            if normalized is not None:
                calendar.append(normalized)

        # Remove named events.
        to_remove = calendar_update.get("remove")
        if isinstance(to_remove, list):
            remove_set = {str(n).strip().lower() for n in to_remove if n}
            context_text = " ".join(str(resolution_context or "").lower().split())
            allowed_remove_set = set()
            blocked_removals = []
            for event in calendar:
                name_raw = str(event.get("name", "")).strip()
                if not name_raw:
                    continue
                name_key = name_raw.lower()
                if name_key not in remove_set:
                    continue
                name_norm = re.sub(r"[^a-z0-9]+", " ", name_key).strip()
                name_tokens = [t for t in name_norm.split() if len(t) > 2]
                name_mentioned = (
                    name_norm in context_text
                    or any(t in context_text for t in name_tokens)
                )
                completion_cues = (
                    "completed",
                    "finished",
                    "resolved",
                    "result delivered",
                    "results delivered",
                    "outcome delivered",
                    "concluded",
                    "cancelled",
                    "abandoned",
                    "closed out",
                    "already departed",
                    "already left",
                    "already en route",
                    "already on the way",
                    "cleared from your schedule",
                    "off your schedule",
                    "no longer pending",
                    "overdue done",
                )
                cleanup_cues = (
                    "remove from calendar",
                    "remove it from calendar",
                    "take it off the calendar",
                    "take it off calendar",
                    "clear it from the calendar",
                    "clear from your schedule",
                    "overdue",
                    "already",
                    "done",
                )
                premature_cues = (
                    "arrives",
                    "arrived",
                    "in progress",
                    "processing",
                    "pending",
                    "awaiting",
                    "sample",
                    "blood drawn",
                    "not back yet",
                )
                has_completion = any(cue in context_text for cue in completion_cues)
                has_cleanup_intent = any(cue in context_text for cue in cleanup_cues)
                has_premature = any(cue in context_text for cue in premature_cues)
                fire_day = event.get("fire_day")
                fire_hour = event.get("fire_hour")
                event_is_past = False
                if isinstance(fire_day, (int, float)) and isinstance(fire_hour, (int, float)):
                    fire_day_int = int(fire_day)
                    fire_hour_int = int(fire_hour)
                    event_is_past = (
                        fire_day_int < day_int
                        or (fire_day_int == day_int and fire_hour_int <= hour_int)
                    )
                if event_is_past:
                    allowed_remove_set.add(name_key)
                elif (
                    name_mentioned
                    and not has_premature
                    and (has_completion or has_cleanup_intent)
                ):
                    allowed_remove_set.add(name_key)
                else:
                    blocked_removals.append(name_raw)
            calendar = [
                e for e in calendar
                if str(e.get("name", "")).strip().lower() not in allowed_remove_set
            ]
            if blocked_removals:
                cls._increment_auto_fix_counter(
                    campaign_state,
                    "calendar_remove_blocked",
                    amount=len(blocked_removals),
                )
                _zork_log(
                    "CALENDAR REMOVE BLOCKED",
                    f"blocked={blocked_removals} context={context_text[:220]}",
                )

        # Add new events.
        to_add = calendar_update.get("add")
        if isinstance(to_add, list):
            for entry in to_add:
                if not isinstance(entry, dict):
                    continue
                name = str(
                    entry.get("name")
                    or entry.get("title")
                    or entry.get("event_key")
                    or ""
                ).strip()
                if not name:
                    continue
                fire_day = entry.get("fire_day")
                fire_hour = entry.get("fire_hour")
                if not isinstance(fire_day, (int, float)) or isinstance(fire_day, bool):
                    day_alias = entry.get("day")
                    if isinstance(day_alias, (int, float)) and not isinstance(day_alias, bool):
                        fire_day = int(day_alias)
                if not isinstance(fire_hour, (int, float)) or isinstance(fire_hour, bool):
                    time_alias = str(entry.get("time") or "").strip()
                    if time_alias:
                        match = re.search(
                            r"(?:day\s*(?P<day>\d+)[,\s-]*)?(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>[ap]m)?",
                            time_alias,
                            re.IGNORECASE,
                        )
                        if match:
                            if not isinstance(fire_day, (int, float)) or isinstance(fire_day, bool):
                                day_group = match.group("day")
                                if day_group:
                                    fire_day = int(day_group)
                            parsed_hour = int(match.group("hour"))
                            ampm = str(match.group("ampm") or "").strip().lower()
                            if ampm == "pm" and parsed_hour < 12:
                                parsed_hour += 12
                            elif ampm == "am" and parsed_hour == 12:
                                parsed_hour = 0
                            fire_hour = parsed_hour
                if (
                    isinstance(fire_day, (int, float))
                    and not isinstance(fire_day, bool)
                    and isinstance(fire_hour, (int, float))
                    and not isinstance(fire_hour, bool)
                ):
                    resolved_fire_day = max(1, int(fire_day))
                    resolved_fire_hour = min(23, max(0, int(fire_hour)))
                elif isinstance(fire_day, (int, float)) and not isinstance(
                    fire_day, bool
                ):
                    resolved_fire_day = max(1, int(fire_day))
                    resolved_fire_hour = 23
                else:
                    resolved_fire_day, resolved_fire_hour = cls._calendar_resolve_fire_point(
                        current_day=day_int,
                        current_hour=hour_int,
                        time_remaining=entry.get("time_remaining", 1),
                        time_unit=entry.get("time_unit", "days"),
                    )
                event = {
                    "name": name,
                    "fire_day": resolved_fire_day,
                    "fire_hour": resolved_fire_hour,
                    "created_day": current_day,
                    "created_hour": current_hour,
                    "description": str(
                        entry.get("description")
                        or entry.get("notes")
                        or entry.get("details")
                        or ""
                    )[:200],
                    "known_by": cls._calendar_known_by_from_event(entry),
                }
                location_text = str(entry.get("location") or "").strip()
                if location_text and not str(event.get("description") or "").strip():
                    event["description"] = f"Location: {location_text}"[:200]
                elif location_text:
                    event["description"] = (
                        f"{str(event.get('description') or '').strip()} Location: {location_text}"
                    )[:200]
                target_players = cls._calendar_target_tokens_from_event(entry)
                visibility = str(entry.get("visibility") or "").strip().lower()
                if (
                    not target_players
                    and visibility == "private"
                    and actor_user_id is not None
                ):
                    target_players = [str(int(actor_user_id))]
                if target_players:
                    event["target_players"] = target_players
                calendar.append(event)

        # Allow re-adds to update existing events.
        if isinstance(to_add, list):
            seen_names = set()
            deduped = []
            for e in reversed(calendar):
                key = str(e.get("name", "")).strip().lower()
                if key in seen_names:
                    continue
                seen_names.add(key)
                deduped.append(e)
            calendar = list(reversed(deduped))

        # Enforce max 10 events (keep the newest).
        if len(calendar) > 10:
            calendar = calendar[-10:]

        campaign_state["calendar"] = calendar
        return campaign_state

    @classmethod
    def _extract_calendar_update_from_state_update(
        cls,
        state_update: object,
        calendar_update: object,
    ) -> Tuple[object, object]:
        if not isinstance(state_update, dict):
            return state_update, calendar_update
        merged_calendar = (
            dict(calendar_update) if isinstance(calendar_update, dict) else {}
        )
        merged_add = list(merged_calendar.get("add") or [])
        changed = False
        cleaned_state_update = dict(state_update)

        def _consume_events(raw_entry: object) -> None:
            nonlocal changed
            if isinstance(raw_entry, dict) and isinstance(raw_entry.get("events"), list):
                for item in raw_entry.get("events") or []:
                    if isinstance(item, dict):
                        merged_add.append(dict(item))
                        changed = True
            elif isinstance(raw_entry, list):
                for item in raw_entry:
                    if isinstance(item, dict):
                        merged_add.append(dict(item))
                        changed = True

        for legacy_key in ("calendar", "cal", "events"):
            raw_entry = cleaned_state_update.get(legacy_key)
            if raw_entry is None:
                continue
            before_count = len(merged_add)
            _consume_events(raw_entry)
            if len(merged_add) != before_count:
                cleaned_state_update.pop(legacy_key, None)

        if not changed:
            return state_update, calendar_update
        merged_calendar["add"] = merged_add
        return cleaned_state_update, merged_calendar

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
    def format_roster(cls, characters: Dict[str, dict]) -> str:
        """Format the character roster for display. Shared by intercepted and cog paths."""
        if not characters:
            return "No characters in the roster yet."
        lines = ["**Character Roster:**"]
        for slug, char in characters.items():
            name = char.get("name", slug)
            loc = char.get("location", "unknown")
            status = char.get("current_status", "")
            bg = char.get("background", "")
            origin = bg.split(".")[0].strip() if bg else ""
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
            lines.append(entry)
        return "\n".join(lines)

    @classmethod
    def _build_characters_for_prompt(
        cls,
        characters: Dict[str, dict],
        campaign_state: Dict[str, object],
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
        overlay_keys = {
            "location",
            "current_status",
            "room_title",
            "room_summary",
            "room_id",
        }

        nearby = []
        mentioned = []
        distant = []

        for slug, char in characters.items():
            effective_char = dict(char)
            raw_overlay = campaign_state.get(slug) if isinstance(campaign_state, dict) else None
            if isinstance(raw_overlay, dict):
                for key in overlay_keys:
                    if key in raw_overlay and raw_overlay.get(key) is not None:
                        effective_char[key] = raw_overlay.get(key)

            char_location = str(effective_char.get("location") or "").strip().lower()
            char_name = str(effective_char.get("name") or slug).strip().lower()
            is_deceased = bool(effective_char.get("deceased_reason"))

            if not is_deceased and player_location and char_location == player_location:
                # Full record for nearby characters (strip image_url — harness-managed).
                entry = {k: v for k, v in effective_char.items() if k != "image_url"}
                entry["_slug"] = slug
                nearby.append(entry)
            elif char_name in recent_lower or slug in recent_lower:
                # Condensed for recently mentioned.
                entry = {
                    "_slug": slug,
                    "name": effective_char.get("name", slug),
                    "speech_style": effective_char.get("speech_style"),
                    "literary_style": effective_char.get("literary_style"),
                    "location": effective_char.get("location"),
                    "current_status": effective_char.get("current_status"),
                    "allegiance": effective_char.get("allegiance"),
                }
                if is_deceased:
                    entry["deceased_reason"] = effective_char.get("deceased_reason")
                mentioned.append(entry)
            else:
                # Minimal for distant/deceased.
                entry = {"_slug": slug, "name": effective_char.get("name", slug)}
                if is_deceased:
                    entry["deceased_reason"] = effective_char.get("deceased_reason")
                else:
                    entry["location"] = effective_char.get("location")
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
    def _literary_styles_for_prompt(
        cls,
        campaign_state: dict,
        characters_for_prompt: list,
    ) -> Optional[str]:
        """Build the LITERARY_STYLES prompt section from campaign_state.

        Returns ``None`` if no styles are stored.
        """
        styles = campaign_state.get(cls.LITERARY_STYLES_STATE_KEY)
        if not isinstance(styles, dict) or not styles:
            return None

        # Collect actively-referenced style keys from characters in the scene.
        active_refs: set = set()
        for char in characters_for_prompt or []:
            if not isinstance(char, dict):
                continue
            ref = char.get("literary_style")
            if ref:
                active_refs.add(str(ref).strip())

        # Sort: actively-referenced first, then alphabetical.
        def _sort_key(key: str):
            return (0 if key in active_refs else 1, key)

        sorted_keys = sorted(styles.keys(), key=_sort_key)

        lines: list = []
        budget = cls.MAX_LITERARY_STYLES_PROMPT_CHARS
        for key in sorted_keys:
            entry = styles[key]
            if not isinstance(entry, dict):
                continue
            profile = str(entry.get("profile") or "").strip()
            if not profile:
                continue
            line = f"  {key}: {profile}"
            if len(line) > budget:
                break
            lines.append(line)
            budget -= len(line) + 1  # +1 for newline
            if budget <= 0:
                break

        return "\n".join(lines) if lines else None

    @classmethod
    def _puzzle_system_for_prompt(cls, campaign_state: dict) -> Optional[str]:
        """Build prompt sections for active puzzles, minigames, and dice checks."""
        parts: list = []

        puzzle_mode = campaign_state.get("puzzle_mode")
        if puzzle_mode and puzzle_mode != "none":
            parts.append(f"PUZZLE_CONFIG:\n  mode: {puzzle_mode}")

        active_puzzle = campaign_state.get("_active_puzzle")
        if isinstance(active_puzzle, dict):
            ap = active_puzzle
            lines = [
                "ACTIVE_PUZZLE:",
                f"  type: {ap.get('puzzle_type', '')}",
                f"  question: \"{ap.get('question', '')}\"",
                f"  attempts: {ap.get('attempts', 0)}/{ap.get('max_attempts', 3)}",
                f"  hint_available: {str(ap.get('hints_used', 0) < len(ap.get('hints', []))).lower()}",
                "  instruction: \"Player is attempting a puzzle. If their action looks like an answer, "
                "the harness has already checked — see PUZZLE_RESULT if present. "
                "Narrate the outcome accordingly.\"",
            ]
            parts.append("\n".join(lines))

        puzzle_result = campaign_state.get("_puzzle_result")
        if isinstance(puzzle_result, dict):
            lines = ["PUZZLE_RESULT:"]
            for k, v in puzzle_result.items():
                lines.append(f"  {k}: {v}")
            parts.append("\n".join(lines))

        active_minigame = campaign_state.get("_active_minigame")
        if isinstance(active_minigame, dict):
            am = active_minigame
            lines = [
                "ACTIVE_MINIGAME:",
                f"  type: {am.get('game_type', '')}",
                f"  opponent: {am.get('opponent_slug', '')}",
            ]
            if am.get("stakes"):
                lines.append(f"  stakes: \"{am['stakes']}\"")
            else:
                lines.append("  stakes: none")
            lines.append(f"  status: {am.get('status', 'player_turn')}")
            lines.append(
                "  instruction: \"Render the board in narration. "
                "Narrate the game action. Ask for the player's next move.\""
            )
            parts.append("\n".join(lines))

        minigame_result = campaign_state.get("_minigame_result")
        if isinstance(minigame_result, dict):
            lines = ["MINIGAME_RESULT:"]
            for k, v in minigame_result.items():
                lines.append(f"  {k}: {v}")
            parts.append("\n".join(lines))

        last_dice = campaign_state.get("_last_dice_check")
        if isinstance(last_dice, dict):
            attr = last_dice.get("attribute", "skill")
            roll_val = last_dice.get("roll", 0)
            mod = last_dice.get("modifier", 0)
            total = last_dice.get("total", 0)
            dc = last_dice.get("dc", 0)
            success = last_dice.get("success", False)
            context = last_dice.get("context", "")
            parts.append(
                f"LAST_DICE_CHECK:\n"
                f"  attribute: {attr}\n"
                f"  roll: {roll_val} + {mod} = {total} vs DC {dc}\n"
                f"  result: {'success' if success else 'failure'}\n"
                f"  context: \"{context}\""
            )

        return "\n\n".join(parts) if parts else None

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
    def normalize_difficulty(cls, value: object) -> str:
        text = " ".join(str(value or "").strip().lower().split())
        if text in cls.DIFFICULTY_LEVELS:
            return text
        aliases = {
            "default": "normal",
            "std": "normal",
            "story mode": "story",
            "easy mode": "easy",
            "medium mode": "medium",
            "normal mode": "normal",
            "hard mode": "hard",
            "impossible mode": "impossible",
        }
        return aliases.get(text, "normal")

    @classmethod
    def get_difficulty(cls, campaign: Optional[ZorkCampaign]) -> str:
        if campaign is None:
            return "normal"
        campaign_state = cls.get_campaign_state(campaign)
        return cls.normalize_difficulty(campaign_state.get("difficulty", "normal"))

    @classmethod
    def set_difficulty(
        cls, campaign: Optional[ZorkCampaign], difficulty: str
    ) -> bool:
        if campaign is None:
            return False
        normalized = cls.normalize_difficulty(difficulty)
        campaign_state = cls.get_campaign_state(campaign)
        campaign_state["difficulty"] = normalized
        campaign.state_json = cls._dump_json(campaign_state)
        campaign.updated = db.func.now()
        db.session.commit()
        return True

    @classmethod
    def _difficulty_response_note(cls, difficulty: object) -> str:
        normalized = cls.normalize_difficulty(difficulty)
        note = cls.DIFFICULTY_NOTES.get(normalized)
        if not note:
            return ""
        return (
            f"[SYSTEM NOTE: FOR THIS RESPONSE ONLY: difficulty={normalized}. {note}]"
        )

    @classmethod
    def _merge_system_notes(cls, *notes: object) -> str:
        parts: List[str] = []
        for raw_note in notes:
            text = str(raw_note or "").strip()
            if not text:
                continue
            if text.startswith("[SYSTEM NOTE: FOR THIS RESPONSE ONLY: ") and text.endswith("]"):
                text = text[len("[SYSTEM NOTE: FOR THIS RESPONSE ONLY: ") : -1].strip()
            elif text.startswith("[SYSTEM NOTE: ") and text.endswith("]"):
                text = text[len("[SYSTEM NOTE: ") : -1].strip()
            if text:
                parts.append(text)
        if not parts:
            return ""
        return f"[SYSTEM NOTE: FOR THIS RESPONSE ONLY: {' '.join(parts)}]"

    @classmethod
    def _turn_response_style_note(
        cls,
        difficulty: object,
        *,
        campaign: Optional[ZorkCampaign] = None,
        channel_id: Optional[int] = None,
    ) -> str:
        style_direction = cls._resolve_zork_style(
            campaign=campaign,
            channel_id=channel_id,
        )
        return cls._merge_system_notes(
            f"Style direction: {style_direction}.",
            cls.RESPONSE_STYLE_NOTE,
            cls._difficulty_response_note(difficulty),
            (
                'Return final JSON only. Include reasoning first. '
                'state_update is required and must include "game_time", "current_chapter", and "current_scene" explicitly. '
                'summary_update is required — one sentence capturing any lasting change or the scene\'s current dramatic state.'
            ),
        )

    @classmethod
    def _turn_stage_note(
        cls,
        difficulty: object,
        prompt_stage: str,
        *,
        campaign: Optional[ZorkCampaign] = None,
        channel_id: Optional[int] = None,
    ) -> str:
        stage = str(prompt_stage or cls.PROMPT_STAGE_FINAL).strip().lower()
        if stage == cls.PROMPT_STAGE_RESEARCH:
            return cls._merge_system_notes(
                f"Style direction: {cls._resolve_zork_style(campaign=campaign, channel_id=channel_id)}.",
                cls._difficulty_response_note(difficulty),
                (
                    "RECENT_TURNS is loaded. Do not call recent_turns again this turn."
                ),
                (
                    "Use memory/source/SMS/planning tools only when they materially improve continuity."
                ),
                (
                    'When research is sufficient, return ONLY {"tool_call": "ready_to_write"}. Do not narrate yet.'
                ),
            )
        return cls._turn_response_style_note(
            difficulty,
            campaign=campaign,
            channel_id=channel_id,
        )

    @classmethod
    def _build_turn_prompt_tail(
        cls,
        player: ZorkPlayer,
        player_state: Dict[str, object],
        action: str,
        turn_attachment_context: Optional[str],
        response_style_note: str,
        extra_lines: Optional[List[str]] = None,
    ) -> str:
        active_name = str(player_state.get("character_name") or "").strip()
        active_mention = f"<@{player.user_id}>" if getattr(player, "user_id", None) else ""
        if active_name and active_mention:
            action_label = f"PLAYER_ACTION {active_mention} ({active_name.upper()})"
        elif active_name:
            action_label = f"PLAYER_ACTION ({active_name.upper()})"
        else:
            action_label = "PLAYER_ACTION"
        parts = [f"{action_label}: {action}"]
        if turn_attachment_context:
            parts.append(f"TURN_ATTACHMENT_CONTEXT:\n{turn_attachment_context}")
        for line in extra_lines or []:
            text = str(line or "").strip()
            if text:
                parts.append(text)
        if response_style_note:
            parts.append(response_style_note)
        return "\n".join(parts)

    @classmethod
    def _recompose_prompt_with_tail(
        cls,
        prompt: str,
        turn_prompt_tail: str,
        *inserted_blocks: object,
    ) -> str:
        prompt_text = str(prompt or "")
        tail_text = str(turn_prompt_tail or "").strip()
        base = prompt_text
        if tail_text:
            with_newline = f"\n{tail_text}\n"
            without_newline = f"\n{tail_text}"
            if prompt_text.endswith(with_newline):
                base = prompt_text[: -len(with_newline)]
            elif prompt_text.endswith(without_newline):
                base = prompt_text[: -len(without_newline)]
            elif prompt_text.endswith(tail_text):
                base = prompt_text[: -len(tail_text)]
        tail_marker = "\nPLAYER_ACTION "
        marker_index = base.rfind(tail_marker)
        if marker_index == -1 and base.startswith("PLAYER_ACTION "):
            marker_index = 0
        if marker_index >= 0:
            base = base[:marker_index]
        parts = [base.rstrip("\n")]
        for block in inserted_blocks:
            text = str(block or "").strip()
            if text:
                parts.append(text)
        if tail_text:
            parts.append(tail_text)
        return "\n".join(part for part in parts if part)

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
    def _calendar_name_key(cls, value: object) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        return re.sub(r"[^a-z0-9]+", "", text)

    @classmethod
    def _calendar_known_by_from_event(cls, event: object) -> List[str]:
        if not isinstance(event, dict):
            return []
        raw_known_by = event.get("known_by")
        items: List[object]
        if isinstance(raw_known_by, list):
            items = raw_known_by
        elif isinstance(raw_known_by, str):
            if "," in raw_known_by:
                items = [chunk.strip() for chunk in raw_known_by.split(",")]
            else:
                items = [raw_known_by]
        else:
            items = []
        out: List[str] = []
        seen: set[str] = set()
        for item in items:
            name = str(item or "").strip()
            if not name:
                continue
            key = cls._calendar_name_key(name)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(name[:80])
            if len(out) >= 24:
                break
        return out

    @classmethod
    def _calendar_target_tokens_from_event(cls, event: object) -> List[str]:
        if not isinstance(event, dict):
            return []
        raw_values: List[object] = []
        for key in (
            "target_players",
            "target_player",
            "targets",
            "target",
            "players",
            "player",
            "player_id",
            "user_id",
            "target_user_id",
            "target_user_ids",
            "who",
        ):
            raw_value = event.get(key)
            if isinstance(raw_value, list):
                raw_values.extend(raw_value)
            elif raw_value is not None:
                raw_values.append(raw_value)
        out: List[str] = []
        seen: set[str] = set()
        for item in raw_values:
            text = str(item or "").strip()
            if not text:
                continue
            key = re.sub(r"\s+", " ", text.lower())[:160]
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(text[:160])
            if len(out) >= 12:
                break
        return out

    @classmethod
    def _calendar_player_aliases_from_registry_entry(
        cls,
        entry: Dict[str, object],
    ) -> set[str]:
        aliases: set[str] = set()

        def _add(raw: object) -> None:
            text = " ".join(str(raw or "").strip().lower().split())
            if text:
                aliases.add(text[:160])

        user_id = entry.get("user_id")
        if isinstance(user_id, int):
            _add(user_id)
            _add(f"<@{user_id}>")
            _add(f"<@!{user_id}>")
        name = str(entry.get("name") or "").strip()
        slug = str(entry.get("slug") or "").strip()
        mention = str(entry.get("discord_mention") or "").strip()
        if name:
            _add(name)
        if slug:
            _add(slug)
        if mention:
            _add(mention)
        if mention and name:
            _add(f"{mention} ({name})")
            _add(f"{mention} {name}")
        if name:
            normalized_name = cls._player_slug_key(name)
            if normalized_name:
                _add(normalized_name)
        return aliases

    @classmethod
    def _resolve_calendar_target_user_ids(
        cls,
        campaign_id: int,
        event: object,
    ) -> List[int]:
        tokens = cls._calendar_target_tokens_from_event(event)
        if not tokens:
            return []
        registry = cls._campaign_player_registry(campaign_id)
        by_user_id = registry.get("by_user_id", {})
        resolved: List[int] = []
        for raw_token in tokens:
            token = str(raw_token or "").strip()
            if not token:
                continue
            mention_match = re.search(r"<@!?(\d+)>", token)
            numeric_match = re.fullmatch(r"\d{4,32}", token)
            candidate_user_ids: List[int] = []
            if mention_match:
                candidate_user_ids.append(int(mention_match.group(1)))
            elif numeric_match:
                candidate_user_ids.append(int(token))
            normalized = " ".join(token.lower().split())
            normalized_slug = cls._player_slug_key(token)
            for user_id, entry in by_user_id.items():
                if not isinstance(user_id, int):
                    continue
                if user_id in candidate_user_ids:
                    continue
                aliases = cls._calendar_player_aliases_from_registry_entry(entry)
                if normalized in aliases or (normalized_slug and normalized_slug in aliases):
                    candidate_user_ids.append(user_id)
                    continue
                if normalized:
                    for alias in aliases:
                        if alias and (normalized in alias or alias in normalized):
                            candidate_user_ids.append(user_id)
                            break
            for user_id in candidate_user_ids:
                if user_id in by_user_id and user_id not in resolved:
                    resolved.append(user_id)
        return resolved[:8]

    @classmethod
    def _calendar_event_scope(cls, campaign_id: int, event: object) -> str:
        return "global" if not cls._resolve_calendar_target_user_ids(campaign_id, event) else "player"

    @classmethod
    def _calendar_event_notification_targets(
        cls,
        campaign_id: int,
        event: object,
    ) -> List[int]:
        explicit_targets = cls._resolve_calendar_target_user_ids(campaign_id, event)
        if explicit_targets:
            return explicit_targets
        return [
            int(row.user_id)
            for row in ZorkPlayer.query.filter_by(campaign_id=campaign_id).all()
            if getattr(row, "user_id", None) is not None
        ]

    @classmethod
    def _calendar_event_key(cls, event: Dict[str, object]) -> str:
        name = str(event.get("name", "")).strip().lower()
        slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")[:80] or "event"
        fire_day = cls._coerce_non_negative_int(event.get("fire_day", 1), default=1) or 1
        fire_hour = min(23, max(0, cls._coerce_non_negative_int(event.get("fire_hour", 23), default=23)))
        return f"{slug}:{fire_day}:{fire_hour}"

    @classmethod
    def _calendar_collect_fired_events(
        cls,
        campaign_id: int,
        campaign_state: Dict[str, object],
        *,
        from_time: Dict[str, int],
        to_time: Dict[str, int],
    ) -> List[Dict[str, object]]:
        if not isinstance(campaign_state, dict):
            return []
        raw_calendar = campaign_state.get("calendar")
        if not isinstance(raw_calendar, list) or not raw_calendar:
            return []
        current_day = cls._coerce_non_negative_int(to_time.get("day", 1), default=1) or 1
        current_hour = min(23, max(0, cls._coerce_non_negative_int(to_time.get("hour", 8), default=8)))
        from_abs = cls._game_time_to_total_minutes(from_time)
        to_abs = cls._game_time_to_total_minutes(to_time)
        if to_abs <= 0:
            return []
        notifications: List[Dict[str, object]] = []
        changed = False
        for raw_event in raw_calendar:
            if not isinstance(raw_event, dict):
                continue
            normalized = cls._calendar_normalize_event(
                raw_event,
                current_day=current_day,
                current_hour=current_hour,
            )
            if normalized is None:
                continue
            event_key = cls._calendar_event_key(normalized)
            if raw_event.get("fired_notice_key") == event_key:
                continue
            fire_day = cls._coerce_non_negative_int(normalized.get("fire_day", current_day), default=current_day)
            fire_hour = min(23, max(0, cls._coerce_non_negative_int(normalized.get("fire_hour", 23), default=23)))
            due_abs = (((max(1, fire_day) - 1) * 24) + fire_hour) * 60
            if due_abs > to_abs:
                continue
            if due_abs > from_abs:
                status = "fired"
            elif due_abs <= from_abs:
                status = "overdue"
            else:
                status = "fired"
            raw_event["fired_notice_key"] = event_key
            raw_event["fired_notice_day"] = current_day
            raw_event["fired_notice_hour"] = current_hour
            changed = True
            notifications.append(
                {
                    "name": str(normalized.get("name") or "Unknown event"),
                    "description": str(normalized.get("description") or "").strip(),
                    "fire_day": fire_day,
                    "fire_hour": fire_hour,
                    "status": status,
                    "scope": cls._calendar_event_scope(campaign_id, raw_event),
                    "target_user_ids": cls._calendar_event_notification_targets(campaign_id, raw_event),
                }
            )
        if changed:
            campaign_state["calendar"] = raw_calendar
        return notifications

    @classmethod
    def _calendar_event_notification_summary(
        cls,
        notification: Dict[str, object],
    ) -> str:
        name = str(notification.get("name") or "Unknown event").strip()
        fire_day = cls._coerce_non_negative_int(notification.get("fire_day", 1), default=1) or 1
        fire_hour = min(23, max(0, cls._coerce_non_negative_int(notification.get("fire_hour", 23), default=23)))
        status = str(notification.get("status") or "fired").strip().lower()
        description = " ".join(str(notification.get("description") or "").split())
        if status == "overdue":
            lead = f"Calendar event overdue: {name} (was due Day {fire_day}, {fire_hour:02d}:00)."
        else:
            lead = f"Calendar event fired: {name} (Day {fire_day}, {fire_hour:02d}:00)."
        if description:
            return cls._trim_text(f"{lead} {description}", 280)
        return lead

    @classmethod
    def _primary_campaign_channel_id(
        cls,
        campaign_id: int,
        preferred_channel_id: Optional[int] = None,
    ) -> Optional[int]:
        candidate_ids: List[int] = []
        if preferred_channel_id is not None:
            candidate_ids.append(int(preferred_channel_id))
        rows = ZorkChannel.query.filter_by(active_campaign_id=campaign_id).all()
        for row in rows:
            channel_id = getattr(row, "channel_id", None)
            if channel_id is None:
                continue
            channel_id = int(channel_id)
            if channel_id not in candidate_ids:
                candidate_ids.append(channel_id)
        recent_rows = (
            ZorkTurn.query.filter_by(campaign_id=campaign_id)
            .filter(ZorkTurn.channel_id.isnot(None))
            .order_by(ZorkTurn.id.desc())
            .limit(20)
            .all()
        )
        for row in recent_rows:
            channel_id = getattr(row, "channel_id", None)
            if channel_id is None:
                continue
            channel_id = int(channel_id)
            if channel_id not in candidate_ids:
                candidate_ids.append(channel_id)
        return candidate_ids[0] if candidate_ids else None

    @classmethod
    def _active_scene_character_names(
        cls,
        player_state: Dict[str, object],
        party_snapshot: List[Dict[str, object]],
        characters_for_prompt: List[Dict[str, object]],
    ) -> List[str]:
        names: List[str] = []
        seen: set[str] = set()

        def _add_name(raw_name: object) -> None:
            text = str(raw_name or "").strip()
            if not text:
                return
            key = cls._calendar_name_key(text)
            if not key or key in seen:
                return
            seen.add(key)
            names.append(text[:80])

        _add_name(player_state.get("character_name"))
        for entry in party_snapshot:
            if not isinstance(entry, dict):
                continue
            _add_name(entry.get("name"))

        for entry in characters_for_prompt:
            if not isinstance(entry, dict):
                continue
            if entry.get("deceased_reason"):
                continue
            char_name = entry.get("name") or entry.get("_slug")
            char_state = {
                "location": entry.get("location"),
                "room_title": entry.get("room_title"),
                "room_summary": entry.get("room_summary"),
                "room_id": entry.get("room_id"),
            }
            if cls._same_scene(player_state, char_state):
                _add_name(char_name)
        return names

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
        turn_attachment_context: Optional[str] = None,
        turn_visibility_default: str = "public",
        tail_extra_lines: Optional[List[str]] = None,
        prompt_stage: str = PROMPT_STAGE_FINAL,
        channel_id: Optional[int] = None,
    ) -> Tuple[str, str]:
        stage = str(prompt_stage or cls.PROMPT_STAGE_FINAL).strip().lower()
        if stage not in {
            cls.PROMPT_STAGE_BOOTSTRAP,
            cls.PROMPT_STAGE_RESEARCH,
            cls.PROMPT_STAGE_FINAL,
        }:
            stage = cls.PROMPT_STAGE_FINAL
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
        _player_registry = cls._campaign_player_registry(campaign.id)
        _player_slugs: Dict[int, str] = {}
        for raw_user_id, info in _player_registry.get("by_user_id", {}).items():
            try:
                user_id = int(raw_user_id)
            except (TypeError, ValueError):
                continue
            slug = str(info.get("slug") or "").strip()
            if slug:
                _player_slugs[user_id] = slug

        _viewer_slug = _player_slugs.get(player.user_id) or cls._player_slug_key(
            player_state.get("character_name")
        )
        _viewer_location_key = cls._room_key_from_player_state(player_state).lower()
        _stored_private_context = cls._active_private_context_from_state(player_state)
        if cls._action_leaves_private_context(action, _stored_private_context):
            _viewer_private_context = None
        else:
            _viewer_private_context = cls._derive_private_context_candidate(
                campaign,
                player,
                player_state,
                action,
            ) or _stored_private_context or cls._fallback_private_context_from_recent(
                turns,
                viewer_user_id=player.user_id,
                viewer_slug=_viewer_slug,
                viewer_location_key=_viewer_location_key,
            )
        _provisional_private_context_key = str(
            (_viewer_private_context or {}).get("context_key") or ""
        ).strip()
        _recent_private_contexts = cls._recent_private_contexts_for_prompt(
            turns,
            viewer_user_id=player.user_id,
            viewer_slug=_viewer_slug,
            active_context_key=_provisional_private_context_key,
            limit=3,
        )
        if not _viewer_private_context:
            _viewer_private_context = cls._fallback_private_context_from_rows(
                _recent_private_contexts,
                viewer_location_key=_viewer_location_key,
            )
        _viewer_private_context_key = str(
            (_viewer_private_context or {}).get("context_key") or ""
        ).strip()
        summary = cls._compose_world_summary(
            campaign,
            state,
            turns=turns,
            viewer_user_id=player.user_id,
            viewer_slug=_viewer_slug,
            viewer_location_key=_viewer_location_key,
            viewer_private_context_key=_viewer_private_context_key,
            max_chars=cls.MAX_SUMMARY_CHARS,
        )
        _recent_private_contexts = cls._recent_private_contexts_for_prompt(
            turns,
            viewer_user_id=player.user_id,
            viewer_slug=_viewer_slug,
            active_context_key=_viewer_private_context_key,
            limit=3,
        )
        recent_text = cls._recent_turns_text_for_viewer(
            campaign,
            turns,
            viewer_user_id=player.user_id,
            viewer_slug=_viewer_slug,
            viewer_location_key=_viewer_location_key,
            viewer_private_context_key=_viewer_private_context_key,
            requested_player_slugs=set(),
            requested_npc_slugs=set(),
        )
        rails_context = cls._build_rails_context(player_state, party_snapshot)

        characters = cls.get_campaign_characters(campaign)
        characters_for_prompt = cls._build_characters_for_prompt(
            characters, state, player_state, recent_text
        )
        characters_for_prompt = cls._fit_characters_to_budget(
            characters_for_prompt, cls.MAX_CHARACTERS_CHARS
        )

        story_context = cls._build_story_context(state)
        on_rails = bool(state.get("on_rails", False))
        _active_scene_names = cls._active_scene_character_names(
            player_state,
            party_snapshot,
            characters_for_prompt,
        )

        _game_time = state.get("game_time", {})
        _speed_mult = state.get("speed_multiplier", 1.0)
        _difficulty = cls.normalize_difficulty(state.get("difficulty", "normal"))
        _style_direction = cls._resolve_zork_style(
            campaign=campaign,
            channel_id=channel_id,
        )
        _stage_note = cls._turn_stage_note(
            _difficulty,
            stage,
            campaign=campaign,
        )
        _calendar_state_before = json.dumps(
            state.get("calendar") or [],
            ensure_ascii=True,
            sort_keys=True,
        )
        _calendar = cls._calendar_for_prompt(
            state,
            campaign_id=campaign.id,
            viewer_user_id=player.user_id,
        )
        _calendar_state_after = json.dumps(
            state.get("calendar") or [],
            ensure_ascii=True,
            sort_keys=True,
        )
        _calendar_reminder_state_before = json.dumps(
            state.get(cls.CALENDAR_REMINDER_STATE_KEY) or {},
            ensure_ascii=True,
            sort_keys=True,
        )
        _calendar_reminders = cls._calendar_reminder_text(
            _calendar,
            active_scene_names=_active_scene_names,
            campaign_state=state,
        )
        _calendar_reminder_state_after = json.dumps(
            state.get(cls.CALENDAR_REMINDER_STATE_KEY) or {},
            ensure_ascii=True,
            sort_keys=True,
        )
        if (
            _calendar_reminder_state_after != _calendar_reminder_state_before
            or _calendar_state_after != _calendar_state_before
        ):
            campaign.state_json = cls._dump_json(state)
        _currently_attentive = cls._build_currently_attentive_players_for_prompt(
            campaign.id
        )
        _campaign_players = cls._campaign_players_for_prompt(campaign.id)
        _source_payload = cls._source_material_prompt_payload(campaign.id)
        _memory_lookup_enabled = cls._memory_lookup_enabled_for_prompt(
            summary,
            source_material_available=bool(_source_payload.get("available")),
            action_text=action,
        )
        _viewer_player_slug = cls._player_slug_key(player_state.get("character_name"))
        _viewer_location_key = cls._room_key_from_player_state(player_state).lower()
        _active_scene_npc_slugs = cls._active_scene_npc_slugs(campaign, player_state)
        _active_plot_threads = cls._plot_threads_for_prompt(
            state,
            campaign=campaign,
            viewer_user_id=player.user_id,
            viewer_player_slug=_viewer_player_slug,
            viewer_location_key=_viewer_location_key,
            active_scene_npc_slugs=_active_scene_npc_slugs,
            limit=10,
        )
        _active_hints = cls._plot_hints_for_viewer(
            campaign,
            state,
            viewer_user_id=player.user_id,
            viewer_player_slug=_viewer_player_slug,
            viewer_location_key=_viewer_location_key,
            active_scene_npc_slugs=_active_scene_npc_slugs,
            limit=3,
        )
        _active_chapters = cls._chapters_for_prompt(
            state, active_only=True, limit=8
        )
        _latest_turn_id = 0
        if isinstance(turns, list):
            for turn in turns:
                try:
                    _latest_turn_id = max(
                        _latest_turn_id, int(getattr(turn, "id", 0) or 0)
                    )
                except (TypeError, ValueError):
                    continue
        _active_consequences = cls._consequences_for_prompt(
            state, current_turn=_latest_turn_id, limit=12
        )
        _active_location_mods = cls._active_location_modifications_for_prompt(
            state, player_state
        )
        _active_location_context = {
            "room_title": player_state.get("room_title"),
            "location": player_state.get("location"),
            "room_summary": player_state.get("room_summary"),
        }
        effective_turn_visibility_default = cls._default_prompt_turn_visibility(
            turn_visibility_default,
            player_state,
        )
        user_prompt = (
            f"CAMPAIGN: {campaign.name}\n"
            f"PLAYER_ID: {player.user_id}\n"
            f"IS_NEW_PLAYER: {str(is_new_player).lower()}\n"
            f"TURN_VISIBILITY_DEFAULT: {effective_turn_visibility_default}\n"
            f"GUARDRAILS_ENABLED: {str(guardrails_enabled).lower()}\n"
        )
        if _source_payload.get("available"):
            user_prompt += (
                f"SOURCE_MATERIAL_DOCS: {cls._dump_json(_source_payload.get('docs') or [])}\n"
                f"SOURCE_MATERIAL_KEYS: {cls._dump_json(_source_payload.get('keys') or [])}\n"
                f"SOURCE_MATERIAL_CHUNK_COUNT: {_source_payload.get('chunk_count')}\n"
            )
            _source_digests = _source_payload.get("digests") or {}
            if _source_digests:
                for _digest_key, _digest_text in _source_digests.items():
                    user_prompt += (
                        f"SOURCE_MATERIAL_DIGEST [{_digest_key}]:\n{_digest_text}\n"
                    )
        user_prompt += (
            f"CURRENT_GAME_TIME: {cls._dump_json(_game_time)}\n"
            f"SPEED_MULTIPLIER: {_speed_mult}\n"
            f"DIFFICULTY: {_difficulty}\n"
            f"ATTENTION_WINDOW_SECONDS: {cls.ATTENTION_WINDOW_SECONDS}\n"
            f"CURRENTLY_ATTENTIVE_PLAYERS: {cls._dump_json(_currently_attentive)}\n"
            f"CAMPAIGN_PLAYERS: {cls._dump_json(_campaign_players)}\n"
            f"ACTIVE_PLAYER_LOCATION: {cls._dump_json(_active_location_context)}\n"
            f"ACTIVE_PRIVATE_CONTEXT: {cls._dump_json(_viewer_private_context or {})}\n"
            f"RECENT_PRIVATE_CONTEXTS: {cls._dump_json(_recent_private_contexts)}\n"
            f"MEMORY_LOOKUP_ENABLED: {str(_memory_lookup_enabled).lower()}\n"
            "RECENT_TURNS_LOADED: false\n"
        )
        _literary_styles_text = cls._literary_styles_for_prompt(state, characters_for_prompt)
        if _literary_styles_text:
            user_prompt += f"LITERARY_STYLES:\n{_literary_styles_text}\n"
        _puzzle_text = cls._puzzle_system_for_prompt(state)
        if _puzzle_text:
            user_prompt += f"{_puzzle_text}\n"
        user_prompt += (
            f"WORLD_CHARACTERS: {cls._dump_json(characters_for_prompt)}\n"
            f"PLAYER_CARD: {cls._dump_json(player_card)}\n"
            f"PARTY_SNAPSHOT: {cls._dump_json(party_snapshot)}\n"
        )
        if stage in {cls.PROMPT_STAGE_RESEARCH, cls.PROMPT_STAGE_FINAL}:
            if story_context:
                user_prompt += f"STORY_CONTEXT:\n{story_context}\n"
            user_prompt += (
                f"WORLD_SUMMARY: {summary}\n"
                f"WORLD_STATE: {cls._dump_json(model_state)}\n"
                f"CALENDAR: {cls._dump_json(_calendar)}\n"
                f"CALENDAR_REMINDERS:\n{_calendar_reminders}\n"
            )
            if _active_plot_threads:
                user_prompt += (
                    f"ACTIVE_PLOT_THREADS: {cls._dump_json(_active_plot_threads)}\n"
                )
            if _active_hints:
                user_prompt += f"ACTIVE_HINTS: {cls._dump_json(_active_hints)}\n"
            if _active_chapters:
                user_prompt += f"ACTIVE_CHAPTERS: {cls._dump_json(_active_chapters)}\n"
            if _active_consequences:
                user_prompt += (
                    f"ACTIVE_CONSEQUENCES: {cls._dump_json(_active_consequences)}\n"
                )
            if _active_location_mods:
                user_prompt += (
                    f"ACTIVE_LOCATION_MODIFICATIONS: {cls._dump_json(_active_location_mods)}\n"
                )
        user_prompt += f"RAILS_CONTEXT: {cls._dump_json(rails_context)}\n"
        turn_prompt_tail = cls._build_turn_prompt_tail(
            player,
            player_state,
            action,
            turn_attachment_context,
            _stage_note,
            extra_lines=tail_extra_lines,
        )
        if turn_prompt_tail:
            user_prompt += f"{turn_prompt_tail}\n"
        if stage == cls.PROMPT_STAGE_BOOTSTRAP:
            _zork_log("WARNING", "PROMPT_STAGE_BOOTSTRAP reached unexpectedly; falling back to RESEARCH")
            stage = cls.PROMPT_STAGE_RESEARCH
        if stage == cls.PROMPT_STAGE_RESEARCH:
            system_prompt = (
                f"Current style direction: {_style_direction}. Treat it as the overall voice lens "
                "unless a specific character canonically speaks otherwise.\n"
                f"{cls.RESEARCH_SYSTEM_PROMPT}"
            )
            if guardrails_enabled:
                system_prompt = f"{system_prompt}{cls.GUARDRAILS_SYSTEM_PROMPT}"
            if on_rails:
                system_prompt = f"{system_prompt}{cls.ON_RAILS_SYSTEM_PROMPT}"
            if _memory_lookup_enabled:
                system_prompt = f"{system_prompt}{cls.MEMORY_TOOL_PROMPT}"
            else:
                system_prompt = f"{system_prompt}{cls.MEMORY_TOOL_DISABLED_PROMPT}"
            system_prompt = f"{system_prompt}{cls.SMS_TOOL_PROMPT}"
            if state.get("timed_events_enabled", True):
                system_prompt = f"{system_prompt}{cls.TIMER_TOOL_PROMPT}"
            if story_context:
                system_prompt = f"{system_prompt}{cls.STORY_OUTLINE_TOOL_PROMPT}"
            system_prompt = f"{system_prompt}{cls.PLOT_PLAN_TOOL_PROMPT}"
            if not on_rails:
                system_prompt = f"{system_prompt}{cls.CHAPTER_PLAN_TOOL_PROMPT}"
            system_prompt = f"{system_prompt}{cls.CONSEQUENCE_TOOL_PROMPT}"
            system_prompt = f"{system_prompt}{cls.CALENDAR_TOOL_PROMPT}"
            system_prompt = f"{system_prompt}{cls.ROSTER_PROMPT}"
            system_prompt = f"{system_prompt}{cls.READY_TO_WRITE_TOOL_PROMPT}"
        else:
            system_prompt = (
                f"Current style direction: {_style_direction}. Treat it as the overall voice lens "
                "unless a specific character canonically speaks otherwise.\n"
                f"{cls.SYSTEM_PROMPT}"
            )
            if guardrails_enabled:
                system_prompt = f"{system_prompt}{cls.GUARDRAILS_SYSTEM_PROMPT}"
            if on_rails:
                system_prompt = f"{system_prompt}{cls.ON_RAILS_SYSTEM_PROMPT}"
            _puzzle_mode = state.get("puzzle_mode")
            if _puzzle_mode and _puzzle_mode != "none":
                system_prompt = f"{system_prompt}{cls.PUZZLE_SYSTEM_PROMPT}"
        return system_prompt, user_prompt

    @staticmethod
    def _is_tool_call(payload: dict) -> bool:
        """Return True when *payload* is a tool invocation without narration."""
        return (
            isinstance(payload, dict)
            and "tool_call" in payload
            and "narration" not in payload
        )

    @staticmethod
    def _tool_call_signature(payload: dict) -> str:
        if not isinstance(payload, dict):
            return ""
        try:
            return json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        except Exception:
            return str(payload)

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
    def _repair_unquoted_json_string_fields(cls, text: str) -> str:
        """Fix missing opening quotes on JSON string values.

        Matches ``"key": SomeText...`` where the value starts with a letter
        but is NOT a JSON literal (true/false/null).  Wraps the bare value
        in quotes so ``json.loads`` can parse it.
        """
        if not text:
            return text
        pattern = re.compile(
            r'("(?P<key>[^"]+)"\s*:\s*)'
            r'(?!true\s*[,}\]]|false\s*[,}\]]|null\s*[,}\]])'
            r'(?=[A-Za-z])'
            r'(?P<value>.*?)'
            r'(?=,\s*"[^"]+"\s*:|,\s*\{|\s*[}\]])',
            re.DOTALL,
        )

        def _replace(match: re.Match[str]) -> str:
            prefix = match.group(1)
            raw_value = str(match.group("value") or "").strip()
            if not raw_value:
                return match.group(0)
            # Strip orphan trailing quote when model wrote closing " but no opening "
            if raw_value.endswith('"') and not raw_value.startswith('"'):
                raw_value = raw_value[:-1].strip()
            return f"{prefix}{json.dumps(raw_value, ensure_ascii=False)}"

        return pattern.sub(_replace, text)

    @classmethod
    def _repair_unquoted_json_keys(cls, text: str) -> str:
        if not text:
            return text
        pattern = re.compile(r'(?P<prefix>[{,]\s*)(?P<key>[A-Za-z_][A-Za-z0-9_-]*)(?P<suffix>\s*:)')

        def _replace(match: re.Match[str]) -> str:
            key = str(match.group("key") or "").strip()
            if not key:
                return match.group(0)
            return f'{match.group("prefix")}"{key}"{match.group("suffix")}'

        return pattern.sub(_replace, text)

    @classmethod
    def _repair_trailing_json_commas(cls, text: str) -> str:
        if not text:
            return text
        return re.sub(r",\s*([}\]])", r"\1", text)

    @classmethod
    def _repair_known_schema_string_fields(cls, text: str) -> str:
        if not text:
            return text
        field_names = "|".join(sorted(re.escape(name) for name in cls.KNOWN_JSON_STRING_FIELDS))
        pattern = re.compile(
            rf'("(?P<key>{field_names})"\s*:\s*)'
            r'(?!(?:"|\{|\[|true\s*[,}\]]|false\s*[,}\]]|null\s*[,}\]]|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\s*[,}\]]))'
            r'(?P<value>.*?)'
            r'(?=,\s*"[^"]+"\s*:|,\s*\{|\s*[}\]])',
            re.DOTALL,
        )

        def _replace(match: re.Match[str]) -> str:
            prefix = match.group(1)
            raw_value = str(match.group("value") or "").strip()
            if not raw_value:
                return match.group(0)
            if raw_value.endswith('"') and not raw_value.startswith('"'):
                raw_value = raw_value[:-1].strip()
            return f"{prefix}{json.dumps(raw_value, ensure_ascii=False)}"

        return pattern.sub(_replace, text)

    @classmethod
    def _repair_json_lenient_text(cls, text: str) -> str:
        repaired = str(text or "")
        repaired = cls._repair_unquoted_json_keys(repaired)
        repaired = cls._repair_trailing_json_commas(repaired)
        repaired = cls._repair_unquoted_json_string_fields(repaired)
        repaired = cls._repair_known_schema_string_fields(repaired)
        return repaired

    @classmethod
    def _parse_json_lenient(cls, text: str) -> dict:
        """Parse a JSON object from *text*, tolerating common LLM quirks.

        Tries in order:
        1. Standard json.loads
        1b. Apply common syntax/schema repairs, then json.loads
        2. Python dict literal (single quotes) via ast.literal_eval
        3. JSONL-style (multiple JSON objects) via raw_decode + merge
        """
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result
            return {}
        except json.JSONDecodeError as exc:
            repaired = cls._repair_json_lenient_text(text)
            if repaired != text:
                try:
                    result = json.loads(repaired)
                    if isinstance(result, dict):
                        return result
                except json.JSONDecodeError:
                    pass
            # Try coercing single-quoted Python dict before anything else.
            coerced = cls._coerce_python_dict(text)
            if coerced is not None:
                return coerced
            if repaired != text:
                coerced = cls._coerce_python_dict(repaired)
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
        cleaned = response.strip()
        json_text = cls._extract_json(cleaned)
        if json_text:
            return json_text
        # Repair common truncated-object case from the model:
        # starts with '{' but omitted the final closing brace.
        if cleaned.startswith("{") and not cleaned.endswith("}"):
            repaired = f"{cleaned}}}"
            try:
                parsed = cls._parse_json_lenient(repaired)
                if isinstance(parsed, dict) and parsed:
                    has_narration = bool(parsed.get("narration"))
                    has_tool_call = bool(parsed.get("tool_call"))
                    if has_narration or has_tool_call:
                        return repaired
            except Exception:
                pass
        return cleaned

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
    def _map_location_components(
        cls,
        location_value: object,
        room_title_value: object,
        room_summary_value: object,
    ) -> Dict[str, str]:
        location = str(location_value or "").strip()
        room_title = str(room_title_value or "").strip()
        room_summary = str(room_summary_value or "").strip()
        summary_first = room_summary.splitlines()[0].strip() if room_summary else ""
        if "." in summary_first:
            summary_first = summary_first.split(".", 1)[0].strip()
        display = room_title or location or summary_first
        display = re.sub(r"\s+", " ", display).strip()[:120]
        key_source = location or room_title or display
        key = re.sub(r"[^a-z0-9]+", "-", key_source.lower()).strip("-")[:80]
        hint = re.sub(r"\s+", " ", room_summary).strip()[:180]
        has_data = bool(location or room_title or room_summary)
        return {
            "key": key or ("unknown-location" if has_data else ""),
            "display": display or ("Unknown" if has_data else ""),
            "hint": hint,
        }

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
        pruned_keys: List[str] = []
        for key, value in update.items():
            if value is None:
                state.pop(key, None)
                pruned_keys.append(key)
            elif (
                isinstance(value, str)
                and value.strip().lower() in cls._COMPLETED_VALUES
            ):
                # Resolved entries don't need to stay in active state.
                state.pop(key, None)
                pruned_keys.append(key)
            else:
                state[key] = value
        if pruned_keys:
            cls._auto_resolve_stale_plot_threads(state, pruned_keys)
        return state

    @classmethod
    def _merge_state_update_with_conflict_resolution(
        cls,
        authoritative_state: Dict[str, object],
        delta_update: Dict[str, object],
    ) -> Dict[str, object]:
        """Deep-merge *delta_update* into *authoritative_state* with per-key
        conflict resolution suitable for concurrent Phase 3 commits.

        Rules:
        * ``game_time`` — monotonic max (keeps the later time).
        * ``current_chapter`` / ``current_scene`` — left for the normal
          advancement guards that run after this merge.
        * Nested dicts — shallow merge (``{**current[key], **delta[key]}``).
        * Scalar keys — last-writer-wins (delta overwrites).
        * ``None`` values — delete the key (existing ``_apply_state_update``
          behaviour).
        """
        if not isinstance(delta_update, dict):
            return authoritative_state

        # Handle game_time monotonic-max separately.
        delta_time = delta_update.get("game_time")
        if isinstance(delta_time, dict) and delta_time:
            current_time = authoritative_state.get("game_time", {})
            if isinstance(current_time, dict):
                delta_minutes = cls._game_time_to_total_minutes(delta_time)
                current_minutes = cls._game_time_to_total_minutes(current_time)
                if delta_minutes > current_minutes:
                    authoritative_state["game_time"] = delta_time
                # else: keep current — it's already further ahead.
            else:
                authoritative_state["game_time"] = delta_time
            delta_update = {k: v for k, v in delta_update.items() if k != "game_time"}

        # Merge remaining keys.
        pruned_keys: List[str] = []
        for key, value in delta_update.items():
            if value is None:
                authoritative_state.pop(key, None)
                pruned_keys.append(key)
            elif (
                isinstance(value, str)
                and value.strip().lower() in cls._COMPLETED_VALUES
            ):
                authoritative_state.pop(key, None)
                pruned_keys.append(key)
            elif isinstance(value, dict):
                existing = authoritative_state.get(key)
                if isinstance(existing, dict):
                    # Shallow merge: delta keys overwrite, existing keys retained.
                    merged = {**existing, **value}
                    # Honour None deletes within nested dict.
                    merged = {k: v for k, v in merged.items() if v is not None}
                    authoritative_state[key] = merged
                else:
                    authoritative_state[key] = value
            else:
                authoritative_state[key] = value

        if pruned_keys:
            cls._auto_resolve_stale_plot_threads(authoritative_state, pruned_keys)
        return authoritative_state

    @classmethod
    def _merge_character_updates(
        cls,
        existing_chars: Dict[str, object],
        delta_updates: Dict[str, object],
    ) -> Dict[str, object]:
        """Per-slug merge for character_updates with conflict resolution.

        * Same NPC updated by two turns: merge mutable fields (last-writer-wins
          per field).
        * Deletion from either turn: deletion wins.
        * New NPC from either turn: add.
        """
        if not isinstance(delta_updates, dict):
            return existing_chars
        for slug, delta_char in delta_updates.items():
            if delta_char is None:
                # Deletion wins.
                existing_chars.pop(slug, None)
                continue
            existing = existing_chars.get(slug)
            if existing is None or not isinstance(existing, dict):
                existing_chars[slug] = delta_char
            elif isinstance(delta_char, dict):
                # Per-field merge.
                for field_key, field_val in delta_char.items():
                    if field_val is None:
                        existing.pop(field_key, None)
                    else:
                        existing[field_key] = field_val
        return existing_chars

    @classmethod
    def _auto_resolve_stale_plot_threads(
        cls,
        campaign_state: Dict[str, object],
        pruned_keys: List[str],
    ) -> int:
        """Auto-resolve active plot threads whose key matches a pruned state key.

        When state_update sets a key to null or a completed value, the
        corresponding plot thread (if any) should not remain active —
        otherwise the LLM sees it in ACTIVE_PLOT_THREADS and keeps
        referencing a resolved storyline.

        Returns the number of threads auto-resolved.
        """
        threads = cls._plot_threads_from_state(campaign_state)
        if not threads:
            return 0
        pruned_slugs = set()
        for key in pruned_keys:
            slug = cls._plot_thread_key(key)
            if slug:
                pruned_slugs.add(slug)
        if not pruned_slugs:
            return 0
        resolved_count = 0
        for thread_key, thread in threads.items():
            if str(thread.get("status")) != "active":
                continue
            if thread_key in pruned_slugs:
                thread["status"] = "resolved"
                if not thread.get("resolution"):
                    thread["resolution"] = "auto-resolved: state key pruned"
                resolved_count += 1
        if resolved_count > 0:
            campaign_state[cls.PLOT_THREADS_STATE_KEY] = threads
        return resolved_count

    @classmethod
    def _push_contextual_turn_log_scope(
        cls,
        campaign: ZorkCampaign,
        *,
        channel_id: Optional[int] = None,
        user_id: Optional[int] = None,
        is_dm: bool = False,
    ) -> Optional[str]:
        dir_path = _zork_log_context_dir(
            guild_id=None if is_dm else getattr(campaign, "guild_id", None),
            channel_id=None if is_dm else channel_id,
            user_id=user_id if is_dm else None,
        )
        return _zork_log_push_path(_zork_log_next_turn_path(dir_path))

    @classmethod
    def _pop_contextual_turn_log_scope(cls, token: Optional[str]) -> None:
        _zork_log_pop_path(token)

    @classmethod
    async def _play_action_preflight(
        cls,
        ctx,
        action: str,
        campaign,
        player,
        player_state: Dict[str, Any],
        command_prefix: str = "!",
    ) -> "PreflightContext | str":
        """Phase 1 (preflight) logic extracted from play_action.

        Returns either a PreflightContext dataclass instance (for proceeding
        to Phase 2) or a str (an early-return message for intercepted commands,
        errors, etc.).

        The caller is responsible for holding the campaign lock and app context.
        """
        # Defensive guard: if still in setup mode, skip gameplay.
        if cls.is_in_setup_mode(campaign):
            return "Campaign setup is still in progress. Please complete setup first."

        action_clean = action.strip().lower()
        time_skip_request = cls._extract_time_skip_request(action)
        is_dm = getattr(ctx, "guild", None) is None

        if is_dm and time_skip_request is not None:
            return (
                "Time skips are disabled in private DMs. "
                "Use the main campaign thread or channel for shared time jumps."
            )

        # Intercepted commands that don't count as player actions
        # should not interrupt timers.
        _INTERCEPTED_COMMANDS = {
            "look", "l", "inventory", "inv", "i",
            "calendar", "cal", "events",
            "roster", "characters", "npcs",
        }
        _is_intercepted = action_clean in _INTERCEPTED_COMMANDS

        timer_interrupt_context = None
        if not _is_intercepted:
            pending = cls._pending_timers.get(campaign.id)
            can_interrupt = (
                pending is not None
                and pending.get("interruptible", True)
                and cls._timer_can_be_interrupted_by(pending, ctx.author.id)
            )
            if can_interrupt:
                cancelled_timer = cls.cancel_pending_timer(campaign.id)
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
                    interrupt_state = cls.get_campaign_state(campaign)
                    interrupt_turn_time = cls._extract_game_time_snapshot(
                        interrupt_state
                    )
                    timer_interrupt_turn = ZorkTurn(
                        campaign_id=campaign.id,
                        user_id=ctx.author.id,
                        kind="narrator",
                        content=interrupt_note,
                        channel_id=ctx.channel.id,
                        meta_json=cls._dump_json(
                            {
                                "game_time": interrupt_turn_time,
                                "visibility": cls._default_turn_visibility_meta(
                                    campaign,
                                    player,
                                    is_dm,
                                ),
                            }
                        ),
                    )
                    db.session.add(timer_interrupt_turn)
                    db.session.flush()
                    cls._record_turn_game_time(
                        interrupt_state,
                        timer_interrupt_turn.id,
                        interrupt_turn_time,
                    )
                    campaign.state_json = cls._dump_json(interrupt_state)
                    db.session.commit()
            # Non-interruptible timers or local timers from another player are left running.

        is_thread_channel = isinstance(ctx.channel, discord.Thread) or is_dm

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

        # --- Intercepted: look ---
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
            quick_state = cls.get_campaign_state(campaign)
            quick_time = cls._extract_game_time_snapshot(quick_state)
            quick_turn_meta = cls._dump_json(
                {
                    "game_time": quick_time,
                    "visibility": cls._default_turn_visibility_meta(
                        campaign,
                        player,
                        is_dm,
                    ),
                }
            )
            look_player_turn = ZorkTurn(
                campaign_id=campaign.id,
                user_id=ctx.author.id,
                kind="player",
                content=action,
                channel_id=ctx.channel.id,
                meta_json=quick_turn_meta,
            )
            look_narrator_turn = ZorkTurn(
                campaign_id=campaign.id,
                user_id=ctx.author.id,
                kind="narrator",
                content=narration,
                channel_id=ctx.channel.id,
                meta_json=quick_turn_meta,
            )
            db.session.add(look_player_turn)
            db.session.add(look_narrator_turn)
            db.session.flush()
            cls._record_turn_game_time(quick_state, look_player_turn.id, quick_time)
            cls._record_turn_game_time(quick_state, look_narrator_turn.id, quick_time)
            campaign.state_json = cls._dump_json(quick_state)
            campaign.last_narration = narration
            campaign.updated = db.func.now()
            db.session.commit()
            return narration

        # --- Intercepted: inventory ---
        if action_clean in ("inventory", "inv", "i"):
            narration = (
                cls._format_inventory(player_state) or "Inventory: empty"
            )
            narration = cls._trim_text(narration, cls.MAX_NARRATION_CHARS)
            quick_state = cls.get_campaign_state(campaign)
            quick_time = cls._extract_game_time_snapshot(quick_state)
            quick_turn_meta = cls._dump_json(
                {
                    "game_time": quick_time,
                    "visibility": cls._default_turn_visibility_meta(
                        campaign,
                        player,
                        is_dm,
                    ),
                }
            )
            inv_player_turn = ZorkTurn(
                campaign_id=campaign.id,
                user_id=ctx.author.id,
                kind="player",
                content=action,
                channel_id=ctx.channel.id,
                meta_json=quick_turn_meta,
            )
            inv_narrator_turn = ZorkTurn(
                campaign_id=campaign.id,
                user_id=ctx.author.id,
                kind="narrator",
                content=narration,
                channel_id=ctx.channel.id,
                meta_json=quick_turn_meta,
            )
            db.session.add(inv_player_turn)
            db.session.add(inv_narrator_turn)
            db.session.flush()
            cls._record_turn_game_time(quick_state, inv_player_turn.id, quick_time)
            cls._record_turn_game_time(quick_state, inv_narrator_turn.id, quick_time)
            campaign.state_json = cls._dump_json(quick_state)
            campaign.last_narration = narration
            campaign.updated = db.func.now()
            db.session.commit()
            return narration

        # --- Intercepted: calendar ---
        if action_clean in ("calendar", "cal", "events"):
            campaign_state = cls.get_campaign_state(campaign)
            game_time = campaign_state.get("game_time", {})
            calendar = cls._calendar_for_prompt(campaign_state)
            date_label = game_time.get(
                "date_label",
                f"Day {game_time.get('day', '?')}, {game_time.get('period', '?').title()}",
            )
            lines = [f"**Game Time:** {date_label}"]
            if calendar:
                lines.append("**Upcoming Events:**")
                for ev in calendar:
                    days_remaining = int(ev.get("_days_until", ev.get("days_remaining", 0)))
                    hours_remaining = int(
                        ev.get("_hours_until", ev.get("hours_remaining", days_remaining * 24))
                    )
                    fire_day = int(ev.get("fire_day", 1))
                    fire_hour = max(
                        0, min(23, int(ev.get("fire_hour", 23)))
                    )
                    desc = ev.get("description", "")
                    if hours_remaining < 0:
                        eta = f"overdue by {abs(hours_remaining)} hour(s)"
                    elif hours_remaining == 0:
                        eta = "fires now"
                    elif hours_remaining < 48:
                        eta = f"fires in {hours_remaining} hour(s)"
                    else:
                        eta_days = (hours_remaining + 23) // 24
                        eta = f"fires in {eta_days} day(s)"
                    lines.append(
                        (
                            f"- **{ev.get('name', 'Unknown')}** — "
                            f"Day {fire_day}, {fire_hour:02d}:00 ({eta})"
                        )
                        + (f" ({desc})" if desc else "")
                    )
            else:
                lines.append("No upcoming events.")
            narration = "\n".join(lines)
            return narration

        # --- Intercepted: roster ---
        if action_clean in ("roster", "characters", "npcs"):
            characters = cls.get_campaign_characters(campaign)
            return cls.format_roster(characters)

        # --- SMS intent detection ---
        sms_activity_detected = False
        sms_inline_draft: Optional[Tuple[str, str]] = None
        is_ooc_action = bool(
            re.match(r"\s*\[OOC\b", action or "", re.IGNORECASE)
        )
        if not is_ooc_action:
            sms_intent = cls._extract_inline_sms_intent(action)
            if sms_intent is not None:
                sms_activity_detected = True
                sms_inline_draft = sms_intent
                sms_recipient, sms_message = sms_intent
                sms_sender = str(
                    player_state.get("character_name")
                    or f"Player {ctx.author.id}"
                ).strip()[:80]
                thread_key = (
                    cls._sms_normalize_thread_key(sms_recipient)
                    or sms_recipient
                )
                _zork_log(
                    "SMS AUTO DRAFT",
                    f"thread={thread_key!r} sender={sms_sender!r} recipient={sms_recipient!r}",
                )

        # --- Context building ---
        campaign_state = cls.get_campaign_state(campaign)
        turns = cls.get_recent_turns(campaign.id)
        turn_attachment_context = await cls._build_turn_attachment_context(
            ctx
        )
        party_snapshot = cls._build_party_snapshot_for_prompt(
            campaign, player, player_state
        )
        viewer_slug = cls._player_slug_key(
            player_state.get("character_name")
        )
        viewer_location_key = cls._room_key_from_player_state(
            player_state
        ).lower()
        stored_private_context = cls._active_private_context_from_state(
            player_state
        )
        if cls._action_leaves_private_context(action, stored_private_context):
            viewer_private_context = None
        else:
            viewer_private_context = cls._derive_private_context_candidate(
                campaign,
                player,
                player_state,
                action,
            ) or stored_private_context or cls._fallback_private_context_from_recent(
                turns,
                viewer_user_id=player.user_id,
                viewer_slug=viewer_slug,
                viewer_location_key=viewer_location_key,
            )
        provisional_private_context_key = str(
            (viewer_private_context or {}).get("context_key") or ""
        ).strip()
        recent_private_contexts = cls._recent_private_contexts_for_prompt(
            turns,
            viewer_user_id=player.user_id,
            viewer_slug=viewer_slug,
            active_context_key=provisional_private_context_key,
            limit=3,
        )
        if not viewer_private_context:
            viewer_private_context = cls._fallback_private_context_from_rows(
                recent_private_contexts,
                viewer_location_key=viewer_location_key,
            )
        viewer_private_context_key = str(
            (viewer_private_context or {}).get("context_key") or ""
        ).strip()

        # --- Build turn tail extra lines ---
        turn_tail_extra_lines: List[str] = []
        if timer_interrupt_context:
            turn_tail_extra_lines.append(
                "TIMER_INTERRUPTED: The player acted before a timed event fired.\n"
                f'The interrupted event was: "{timer_interrupt_context}"\n'
                f'The player\'s action that interrupted it: "{action}"\n'
                "Incorporate the interruption naturally into your narration."
            )
        if time_skip_request is not None:
            skip_desc = str(
                time_skip_request.get("description") or ""
            ).strip()
            skip_minutes = cls._coerce_non_negative_int(
                time_skip_request.get("minutes", 60), default=60
            )
            turn_tail_extra_lines.append(
                "TIME_SKIP: The player requests a time skip. Fast-forward past "
                "any idle, repetitive, or low-stakes moments and jump ahead to "
                "the next meaningful story beat — a new encounter, discovery, "
                "twist, or decision point. Summarise skipped time in one brief "
                "sentence, then narrate the new moment in full.\n"
                f"- requested_skip_minutes={skip_minutes}\n"
                + (
                    f"- requested_skip_description={skip_desc}\n"
                    if skip_desc
                    else ""
                )
            )
        if sms_inline_draft is not None:
            sms_recipient, sms_message = sms_inline_draft
            sms_sender = str(
                player_state.get("character_name")
                or f"Player {ctx.author.id}"
            ).strip()[:80]
            sms_thread = (
                cls._sms_normalize_thread_key(sms_recipient)
                or sms_recipient
            )
            turn_tail_extra_lines.append(
                "SMS_DRAFT: The player's action looks like an attempted outgoing text.\n"
                f"- thread='{sms_thread}'\n"
                f"- from='{sms_sender}' to='{sms_recipient}'\n"
                f"- message='{sms_message}'\n"
                "Do NOT assume it has been sent yet. If it really sends in this scene, call sms_write. "
                "If the send fails, is interrupted, is reconsidered, or never actually happens, do not call sms_write."
            )

        # --- Prompt building ---
        prompt_difficulty = cls.normalize_difficulty(
            campaign_state.get("difficulty", "normal")
        )
        current_prompt_stage = cls.PROMPT_STAGE_RESEARCH
        receiver_hints = cls._recent_turn_receiver_hints(
            campaign,
            viewer_user_id=player.user_id,
            party_snapshot=party_snapshot,
            player_state=player_state,
        )
        first_payload = {
            "tool_call": "recent_turns",
            "player_slugs": receiver_hints.get("player_slugs") or [],
            "npc_slugs": receiver_hints.get("npc_slugs") or [],
        }
        system_prompt, user_prompt = cls.build_prompt(
            campaign,
            player,
            action,
            turns,
            party_snapshot=party_snapshot,
            is_new_player=is_new_player,
            turn_attachment_context=turn_attachment_context,
            turn_visibility_default="public",
            tail_extra_lines=turn_tail_extra_lines,
            prompt_stage=current_prompt_stage,
            channel_id=getattr(ctx.channel, "id", None),
        )
        base_user_prompt = user_prompt
        turn_prompt_tail = cls._build_turn_prompt_tail(
            player,
            player_state,
            action,
            turn_attachment_context,
            cls._turn_stage_note(
                prompt_difficulty,
                current_prompt_stage,
                campaign=campaign,
                channel_id=getattr(ctx.channel, "id", None),
            ),
            extra_lines=turn_tail_extra_lines,
        )
        memory_lookup_enabled = (
            "memory_lookup_enabled: true" in user_prompt.lower()
        )
        gpt = cls._new_gpt(
            campaign=campaign,
            channel_id=getattr(ctx.channel, "id", None),
        )
        _zork_log(
            f"TURN START campaign={campaign.id}",
            f"heuristic preload: recent_turns with receivers={receiver_hints}\n"
            f"--- SYSTEM PROMPT ---\n{system_prompt}\n\n--- USER PROMPT ---\n{user_prompt}",
        )

        # --- Snapshot state_version from campaign_state ---
        state_version = campaign_state.get("_state_version", 0)

        # --- Build and return PreflightContext ---
        return PreflightContext(
            campaign_id=campaign.id,
            player_id=player.id,
            user_id=ctx.author.id,
            campaign_state=campaign_state,
            player_state=player_state,
            state_version=state_version,
            pre_turn_game_time={},  # populated in Phase 2
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            gpt=gpt,
            action=action,
            turns=turns,
            party_snapshot=party_snapshot,
            base_user_prompt=base_user_prompt,
            turn_prompt_tail=turn_prompt_tail,
            turn_tail_extra_lines=turn_tail_extra_lines,
            prompt_difficulty=prompt_difficulty,
            current_prompt_stage=current_prompt_stage,
            first_payload=first_payload,
            memory_lookup_enabled=memory_lookup_enabled,
            is_new_player=is_new_player,
            turn_attachment_context=turn_attachment_context,
            viewer_slug=viewer_slug,
            viewer_location_key=viewer_location_key,
            viewer_private_context_key=viewer_private_context_key,
            viewer_private_context=viewer_private_context,
            recent_private_contexts=recent_private_contexts,
            private_context_candidate=viewer_private_context,
            time_skip_request=time_skip_request,
            timer_interrupt_context=timer_interrupt_context,
            sms_activity_detected=sms_activity_detected,
            sms_inline_draft=sms_inline_draft,
            is_thread_channel=is_thread_channel,
            is_dm=is_dm,
            channel_id=getattr(ctx.channel, "id", 0),
            command_prefix=command_prefix,
            player_attributes_json=player.attributes_json,
            player_level=player.level,
            player_xp=player.xp,
            campaign_name=campaign.name or "",
            campaign_summary=campaign.summary,
            on_rails=bool(campaign_state.get("on_rails")),
            is_timed_events_enabled=bool(campaign_state.get("timed_events_enabled")),
            receiver_hints=receiver_hints,
            auto_forced_memory_search=False,
        )

    @classmethod
    async def _play_action_commit(
        cls,
        ctx,
        preflight: "PreflightContext",
        delta: "TurnDelta",
        campaign,
    ) -> str:
        """
        Phase 3 — Commit phase.

        The caller holds the commit_lock and app_context. This method performs
        all state writes atomically:
        1. Re-reads authoritative state from DB
        2. Checks state version for concurrent modifications
        3. Merges state_update using conflict resolution
        4. Applies deferred tool writes
        5. Applies remaining state processing
        6. Persists turns and increments state version
        7. Triggers post-commit side effects (memory embedding, images, timers, notifications)

        Returns the display narration string.
        """
        # --- 1. Re-read authoritative state from DB ---
        campaign_state = cls.get_campaign_state(campaign)
        player = cls.get_or_create_player(campaign.id, preflight.user_id, campaign=campaign)
        player_state = cls.get_player_state(player)

        # --- 2. Check state version for concurrent modifications ---
        current_version = campaign_state.get("_state_version", 0)
        if current_version != delta.state_version_at_read:
            _zork_log(
                f"CONCURRENT MODIFICATION campaign={campaign.id}",
                f"expected_version={delta.state_version_at_read} current_version={current_version}",
            )

        # --- 3. Merge state_update using conflict resolution ---
        # game_time uses monotonic max; nested dicts get shallow-merged;
        # None deletes; completed values auto-resolve plot threads.
        cls._merge_state_update_with_conflict_resolution(
            campaign_state, delta.state_update
        )

        # --- 4. Apply deferred tool writes ---
        current_turn_hint = int(preflight.turns[-1].id) if preflight.turns else 0
        for entry in delta.deferred_tool_writes:
            tool_name = entry.get("tool") or entry.get("tool_name", "")
            payload = entry.get("payload", {})

            if tool_name == "plot_plan":
                cls._apply_plot_plan_tool(
                    campaign_state,
                    payload,
                    current_turn=current_turn_hint,
                )
            elif tool_name == "chapter_plan":
                cls._apply_chapter_plan_tool(
                    campaign_state,
                    payload,
                    current_turn=current_turn_hint,
                    on_rails=bool(campaign_state.get("on_rails", False)),
                )
            elif tool_name == "consequence_log":
                cls._apply_consequence_log_tool(
                    campaign_state,
                    payload,
                    current_turn=current_turn_hint,
                )
            elif tool_name == "sms_write":
                post_turn_game_time = cls._extract_game_time_snapshot(campaign_state)
                cls._sms_write(
                    campaign_state,
                    thread=entry.get("thread", ""),
                    sender=entry.get("sender", ""),
                    recipient=entry.get("recipient", ""),
                    message=entry.get("message", ""),
                    game_time=post_turn_game_time,
                    turn_id=current_turn_hint,
                )
            elif tool_name == "sms_read":
                cls._sms_mark_threads_read(
                    campaign_state,
                    actor_id=preflight.user_id,
                    player_state=player_state,
                    thread_markers=entry.get("thread_markers", {}),
                )
            elif tool_name == "sms_schedule":
                cls._schedule_sms_delivery(
                    campaign_id=campaign.id,
                    delay_seconds=entry.get("delay_seconds", 60),
                    thread=entry.get("thread", ""),
                    sender=entry.get("sender", ""),
                    recipient=entry.get("recipient", ""),
                    message=entry.get("message", ""),
                )

        # --- 5. Apply remaining state processing ---
        # Unpack delta fields
        state_update = delta.state_update
        player_state_update = delta.player_state_update
        character_updates = delta.character_updates
        summary_update = delta.summary_update
        calendar_update = delta.calendar_update
        give_item = delta.give_item
        xp_awarded = delta.xp_awarded
        turn_visibility = delta.turn_visibility
        story_progression = delta.story_progression
        scene_image_prompt = delta.scene_image_prompt
        scene_output_raw = delta.scene_output_raw
        narration = delta.narration
        raw_narration = delta.raw_narration
        reasoning = delta.reasoning
        scene_output = delta.scene_output
        forced_planning_payload = delta.forced_planning_payload
        ephemeral_notices = delta.ephemeral_notices
        sms_activity_detected = delta.sms_activity_detected
        private_phone_redacted = delta.private_phone_redacted
        stored_player_action = delta.stored_player_action
        suppress_recent_context = delta.suppress_recent_context
        auto_forced_memory_search = delta.auto_forced_memory_search
        empty_response_repair_count = delta.empty_response_repair_count
        anti_echo_retry_count = delta.anti_echo_retry_count
        action = preflight.action

        # Ensure minimum state update contract
        state_update = cls._ensure_minimum_state_update_contract(
            campaign_state,
            state_update,
        )
        state_update, calendar_update = cls._extract_calendar_update_from_state_update(
            state_update,
            calendar_update,
        )

        # Turn visibility normalization
        turn_visibility = cls._normalize_turn_visibility(
            campaign,
            player,
            turn_visibility,
            is_private_context=(getattr(ctx, "guild", None) is None),
        )
        turn_visibility = cls._promote_player_npc_slugs(
            turn_visibility, campaign.id,
        )

        # Private context handling
        private_context_candidate = cls._derive_private_context_candidate(
            campaign,
            player,
            player_state,
            action,
        )
        if private_context_candidate is not None:
            turn_visibility = cls._apply_private_context_candidate(
                turn_visibility,
                private_context_candidate,
            )

        if sms_activity_detected or private_phone_redacted:
            turn_visibility = cls._force_private_visibility_for_phone_activity(
                turn_visibility,
                actor_slug=str(
                    turn_visibility.get("actor_player_slug")
                    or cls._player_slug_key(
                        player_state.get("character_name")
                    )
                    or ""
                ).strip(),
                actor_user_id=ctx.author.id,
            )
            cls._increment_auto_fix_counter(
                campaign_state,
                "private_phone_redacted",
            )

        cls._persist_private_context_state(
            player_state,
            turn_visibility,
            action,
            private_context_candidate,
        )

        # Room state rescue and split
        state_update, player_state_update = cls._rescue_misplaced_room_state(
            state_update, player_state_update
        )
        state_update, player_state_update = cls._split_room_state(
            state_update, player_state_update
        )

        # Apply state update to campaign_state
        pre_turn_game_time = preflight.pre_turn_game_time
        campaign_state = cls._apply_state_update(
            campaign_state, state_update
        )
        campaign_state = cls._ensure_game_time_progress(
            campaign_state,
            pre_turn_game_time,
            action_text=action,
            narration_text=raw_narration,
        )

        # Forced planning payload application
        if isinstance(forced_planning_payload, dict):
            _planning_tool_name = str(
                forced_planning_payload.get("tool_call") or ""
            ).strip()
            if _planning_tool_name == "plot_plan":
                _plan_result = cls._apply_plot_plan_tool(
                    campaign_state,
                    forced_planning_payload,
                    current_turn=current_turn_hint,
                )
                _zork_log(
                    "FORCED PLOT PLAN APPLIED",
                    json.dumps(_plan_result, ensure_ascii=True),
                )
                cls._increment_auto_fix_counter(
                    campaign_state, "forced_planning_tool"
                )
            elif _planning_tool_name == "chapter_plan":
                _plan_result = cls._apply_chapter_plan_tool(
                    campaign_state,
                    forced_planning_payload,
                    current_turn=current_turn_hint,
                    on_rails=bool(campaign_state.get("on_rails", False)),
                )
                _zork_log(
                    "FORCED CHAPTER PLAN APPLIED",
                    json.dumps(_plan_result, ensure_ascii=True),
                )
                cls._increment_auto_fix_counter(
                    campaign_state, "forced_planning_tool"
                )
            elif _planning_tool_name == "consequence_log":
                _cons_result = cls._apply_consequence_log_tool(
                    campaign_state,
                    forced_planning_payload,
                    current_turn=current_turn_hint,
                )
                _zork_log(
                    "FORCED CONSEQUENCE LOG APPLIED",
                    json.dumps(_cons_result, ensure_ascii=True),
                )
                cls._increment_auto_fix_counter(
                    campaign_state, "forced_planning_tool"
                )

        # Auto-fix counter increments
        if auto_forced_memory_search:
            cls._increment_auto_fix_counter(
                campaign_state, "forced_memory_search"
            )
        if empty_response_repair_count > 0:
            cls._increment_auto_fix_counter(
                campaign_state,
                "empty_response_repair_retry",
                amount=empty_response_repair_count,
            )
        if anti_echo_retry_count > 0:
            cls._increment_auto_fix_counter(
                campaign_state,
                "anti_echo_retry",
                amount=anti_echo_retry_count,
            )

        campaign_state = cls._scrub_inventory_from_state(campaign_state)
        campaign.state_json = cls._dump_json(campaign_state)

        # Chapter/scene advancement
        story_outline = campaign_state.get("story_outline")
        if isinstance(story_outline, dict):
            chapters = story_outline.get("chapters", [])
            old_ch = cls._coerce_non_negative_int(
                campaign_state.get("current_chapter", 0), default=0
            )
            if isinstance(chapters, list) and chapters:
                old_ch = min(old_ch, len(chapters) - 1)

            new_ch_raw = state_update.get("current_chapter")
            new_sc_raw = state_update.get("current_scene")

            new_ch = None
            if new_ch_raw is not None:
                new_ch = cls._coerce_non_negative_int(new_ch_raw, default=old_ch)
                if isinstance(chapters, list) and chapters:
                    new_ch = min(new_ch, len(chapters) - 1)

            scene_ch_idx = new_ch if new_ch is not None else old_ch
            new_sc = None
            if new_sc_raw is not None:
                new_sc = cls._coerce_non_negative_int(new_sc_raw, default=0)
                if isinstance(chapters, list) and 0 <= scene_ch_idx < len(chapters):
                    scene_list = chapters[scene_ch_idx].get("scenes", [])
                    if isinstance(scene_list, list) and scene_list:
                        new_sc = min(new_sc, len(scene_list) - 1)
                    else:
                        new_sc = 0

            if new_ch is not None and new_ch != old_ch:
                if isinstance(chapters, list) and 0 <= old_ch < len(chapters):
                    chapters[old_ch]["completed"] = True
                campaign_state["current_chapter"] = new_ch
                if new_sc is None:
                    campaign_state["current_scene"] = 0

            if new_sc is not None:
                campaign_state["current_scene"] = new_sc
            state_update.pop("current_chapter", None)
            state_update.pop("current_scene", None)
            campaign.state_json = cls._dump_json(campaign_state)

        story_progressed = cls._apply_story_progression_hint(
            campaign_state,
            story_progression,
            state_update if isinstance(state_update, dict) else {},
        )
        if story_progressed:
            _zork_log(
                "STORY PROGRESSION APPLIED",
                json.dumps(
                    {
                        "current_chapter": campaign_state.get("current_chapter"),
                        "current_scene": campaign_state.get("current_scene"),
                        "story_progression": story_progression,
                    },
                    ensure_ascii=True,
                ),
            )
            cls._increment_auto_fix_counter(
                campaign_state, "story_progression_hint"
            )

        auto_story_advanced = False
        if not story_progressed:
            auto_story_advanced = cls._auto_advance_on_rails_story_context(
                campaign_state,
                action_text=action,
                narration=raw_narration,
                summary_update=summary_update,
                state_update=state_update if isinstance(state_update, dict) else {},
                player_state_update=player_state_update if isinstance(player_state_update, dict) else {},
                character_updates=character_updates if isinstance(character_updates, dict) else {},
                calendar_update=calendar_update,
            )
        if auto_story_advanced:
            _zork_log(
                "AUTO STORY ADVANCE",
                json.dumps(
                    {
                        "current_chapter": campaign_state.get("current_chapter"),
                        "current_scene": campaign_state.get("current_scene"),
                        "action": str(action or "")[:160],
                    },
                    ensure_ascii=True,
                ),
            )
            cls._increment_auto_fix_counter(
                campaign_state, "auto_story_advance"
            )

        # Character updates application
        _on_rails = bool(campaign_state.get("on_rails", False))
        if character_updates and isinstance(character_updates, dict):
            existing_chars = cls.get_campaign_characters(campaign)
            _pre_slugs = set(existing_chars.keys())
            existing_chars = cls._apply_character_updates(
                existing_chars,
                character_updates,
                on_rails=_on_rails,
                campaign_id=campaign.id,
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

        # Calendar update application
        if calendar_update and isinstance(calendar_update, dict):
            campaign_state = cls._apply_calendar_update(
                campaign_state,
                calendar_update,
                resolution_context=(
                    f"{action}\n{raw_narration}\n{summary_update or ''}"
                ),
                actor_user_id=ctx.author.id,
            )
            campaign.state_json = cls._dump_json(campaign_state)

        _turn_is_public = (
            str((turn_visibility or {}).get("scope") or "").strip().lower()
            in {"public", "local"}
        )

        # Player state update application
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
        player_state_update = cls._sync_game_time_to_player_state(
            state_update, player_state_update
        )
        turn_visibility = cls._sanitize_turn_awareness_for_scene(
            campaign,
            player_state,
            turn_visibility,
        )
        turn_visibility["location_key"] = (
            cls._room_key_from_player_state(player_state).strip() or None
        )
        player.state_json = cls._dump_json(player_state)

        # Location syncing
        cls._sync_main_party_room_state(
            campaign.id,
            player.user_id,
            player_state,
        )
        _active_char_sync_count = cls._sync_active_player_character_location(
            campaign,
            player_state=player_state,
        )
        _state_loc_sync_count = cls._auto_sync_companion_locations(
            campaign_state,
            player_state=player_state,
            narration_text=raw_narration,
        )
        _char_loc_sync_count = cls._auto_sync_character_locations(
            campaign,
            player_state=player_state,
            narration_text=raw_narration,
        )
        _roster_overlay_sync_count = cls._sync_npc_locations_from_state_to_roster(
            campaign, state_update
        )
        if _active_char_sync_count or _state_loc_sync_count or _char_loc_sync_count or _roster_overlay_sync_count:
            cls._increment_auto_fix_counter(
                campaign_state,
                "location_auto_sync_active_character",
                amount=_active_char_sync_count,
            )
            cls._increment_auto_fix_counter(
                campaign_state,
                "location_auto_sync_state_entities",
                amount=_state_loc_sync_count,
            )
            cls._increment_auto_fix_counter(
                campaign_state,
                "location_auto_sync_world_characters",
                amount=_char_loc_sync_count,
            )
            cls._increment_auto_fix_counter(
                campaign_state,
                "location_auto_sync_roster_overlay",
                amount=_roster_overlay_sync_count,
            )
            _zork_log(
                f"LOCATION AUTO-SYNC campaign={campaign.id}",
                (
                    f"active_player_character={_active_char_sync_count} "
                    f"state_entities={_state_loc_sync_count} "
                    f"world_characters={_char_loc_sync_count} "
                    f"roster_overlay={_roster_overlay_sync_count}"
                ),
            )
        _player_entity_sync_count = cls._sync_player_states_from_campaign_entities(
            campaign,
            campaign_state,
            skip_user_id=player.user_id,
        )
        _co_located_player_sync_count = cls._sync_marked_co_located_players(
            campaign.id,
            player.user_id,
            player_state,
            delta.co_located_player_slugs,
        )
        if _player_entity_sync_count:
            cls._increment_auto_fix_counter(
                campaign_state,
                "location_auto_sync_other_players",
                amount=_player_entity_sync_count,
            )
            _zork_log(
                f"PLAYER ENTITY AUTO-SYNC campaign={campaign.id}",
                f"other_players={_player_entity_sync_count}",
            )
        if _co_located_player_sync_count:
            cls._increment_auto_fix_counter(
                campaign_state,
                "location_auto_sync_co_located_players",
                amount=_co_located_player_sync_count,
            )
            _zork_log(
                f"CO-LOCATED PLAYER AUTO-SYNC campaign={campaign.id}",
                f"other_players={_co_located_player_sync_count}",
            )
        campaign.state_json = cls._dump_json(campaign_state)

        # --- give_item processing ---
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
                    _mention_re = re.compile(r"<@!?(\d+)>")
                    for _m in _mention_re.finditer(raw_narration or ""):
                        _target_uid = int(_m.group(1))
                        if _target_uid != player.user_id:
                            _inferred_item = _removed[0] if len(_removed) == 1 else None
                            if _inferred_item is None:
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
            gi_target_uid = None
            if gi_mention.startswith("<@") and gi_mention.endswith(">"):
                try:
                    gi_target_uid = int(gi_mention.strip("<@!>"))
                except (ValueError, TypeError):
                    pass
            if gi_item_name and gi_target_uid and gi_target_uid != player.user_id:
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
                        if giver_has_now:
                            player_state["inventory"] = cls._apply_inventory_delta(
                                giver_inv, [], [gi_item_name], origin_hint=""
                            )
                            player.state_json = cls._dump_json(player_state)
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

        # XP award
        if isinstance(xp_awarded, int) and xp_awarded > 0:
            player.xp += xp_awarded

        # Scene output normalization
        scene_output = cls._normalize_scene_output(
            campaign,
            scene_output_raw,
            fallback_narration=narration,
            turn_visibility=turn_visibility,
            fallback_location_key=cls._room_key_from_player_state(player_state),
            actor_user_id=ctx.author.id,
            actor_player_slug=str(
                turn_visibility.get("actor_player_slug") or ""
            ).strip(),
        )
        if sms_activity_detected or private_phone_redacted:
            scene_output = cls._scrub_scene_output_npc_awareness(
                scene_output
            )
        rendered_scene_output = cls._scene_output_rendered_text(
            scene_output
        )
        if rendered_scene_output:
            narration = rendered_scene_output
            raw_narration = rendered_scene_output
        if scene_output:
            _zork_log(
                f"SCENE OUTPUT campaign={campaign.id}",
                json.dumps(scene_output, indent=2, ensure_ascii=False),
            )

        # Summary update
        if summary_update:
            summary_update = summary_update.strip()
            summary_update = cls._strip_inventory_mentions(summary_update)
            if not _turn_is_public:
                _zork_log(
                    f"SUMMARY FILTERED (non-public turn) campaign={campaign.id}",
                    summary_update,
                )
            elif not cls._scene_output_is_summary_public_safe(scene_output):
                _zork_log(
                    f"SUMMARY FILTERED (non-public beat) campaign={campaign.id}",
                    summary_update,
                )
            elif cls._should_keep_summary_update(
                summary_update,
                state_update=state_update,
                player_state_update=player_state_update,
                character_updates=character_updates,
                calendar_update=calendar_update,
            ):
                campaign.summary = cls._append_summary(
                    campaign.summary, summary_update
                )
            else:
                _zork_log(
                    f"SUMMARY FILTERED campaign={campaign.id}",
                    summary_update,
                )

        clean_narration = cls._strip_prompt_artifacts(
            cls._strip_ephemeral_context_lines(
                cls._strip_narration_footer(narration)
            )
        )
        persisted_narration = clean_narration
        display_narration = clean_narration

        post_turn_game_time = cls._extract_game_time_snapshot(campaign_state)
        calendar_event_notifications = cls._calendar_collect_fired_events(
            campaign.id,
            campaign_state,
            from_time=pre_turn_game_time,
            to_time=post_turn_game_time,
        )
        sms_notice = cls._sms_unread_hourly_notification(
            campaign_state,
            actor_id=ctx.author.id,
            player_state=player_state,
            game_time=post_turn_game_time,
        )
        if sms_notice:
            display_narration = f"{display_narration}\n\n{sms_notice}"
            cls._increment_auto_fix_counter(
                campaign_state, "sms_unread_notice"
            )
            sms_summary = cls._sms_unread_summary_for_player(
                campaign_state,
                actor_id=ctx.author.id,
                player_state=player_state,
            )
            thread_markers = (
                sms_summary.get("thread_markers")
                if isinstance(sms_summary, dict)
                else {}
            )
            if isinstance(thread_markers, dict) and thread_markers:
                read_changed = cls._sms_mark_threads_read(
                    campaign_state,
                    actor_id=ctx.author.id,
                    player_state=player_state,
                    thread_markers=thread_markers,
                )
                if read_changed:
                    cls._increment_auto_fix_counter(
                        campaign_state, "sms_auto_mark_read"
                    )

        # Timer scheduling
        timer_scheduled_delay = delta.timer_scheduled_delay
        timer_scheduled_event = delta.timer_scheduled_event
        timer_scheduled_interruptible = delta.timer_scheduled_interruptible
        timer_scheduled_interrupt_scope = delta.timer_scheduled_interrupt_scope

        if timer_scheduled_delay is not None:
            cls.cancel_pending_timer(campaign.id)
            cls._schedule_timer(
                campaign.id,
                ctx.channel.id,
                timer_scheduled_delay,
                timer_scheduled_event or "Something happens",
                interruptible=timer_scheduled_interruptible,
                interrupt_action=delta.timer_scheduled_interrupt_action,
                interrupt_scope=timer_scheduled_interrupt_scope,
                interrupt_user_id=delta.timer_scheduled_interrupt_user_id or ctx.author.id,
            )
            expiry_ts = int(time.time()) + timer_scheduled_delay
            event_hint = timer_scheduled_event or "Something happens"
            if timer_scheduled_interruptible:
                if timer_scheduled_interrupt_scope == "local":
                    interrupt_hint = "acting player can prevent"
                else:
                    interrupt_hint = "act to prevent!"
            else:
                interrupt_hint = "unavoidable"
            display_narration = (
                f"{display_narration}\n\n"
                f"\u23f0 <t:{expiry_ts}:R>: {event_hint} ({interrupt_hint})"
            )

        # Time jump notifications
        time_jump_notification = None
        if _turn_is_public and getattr(ctx, "guild", None) is not None:
            delta_minutes = max(
                0,
                cls._game_time_to_total_minutes(post_turn_game_time)
                - cls._game_time_to_total_minutes(pre_turn_game_time),
            )
            if delta_minutes >= cls.PRIVATE_DM_TIME_JUMP_NOTIFY_MINUTES:
                recipients = cls._recent_private_dm_notification_targets(
                    campaign.id,
                    exclude_user_id=ctx.author.id,
                )
                if recipients:
                    time_jump_notification = {
                        "campaign_name": campaign.name,
                        "recipient_user_ids": recipients,
                        "from_time": pre_turn_game_time,
                        "to_time": post_turn_game_time,
                        "delta_minutes": delta_minutes,
                        "event_summary": cls._brief_event_summary(
                            action_text=action,
                            summary_update=summary_update,
                            narration_text=raw_narration,
                        ),
                    }

        campaign.last_narration = persisted_narration
        campaign.updated = db.func.now()
        player.updated = db.func.now()
        cls._set_turn_ephemeral_notices(
            campaign.id,
            ctx.author.id,
            ephemeral_notices,
        )

        # Turn persistence (ZorkTurn rows)
        player_turn_meta = cls._dump_json(
            {
                "game_time": pre_turn_game_time,
                "visibility": turn_visibility,
                "location_key": cls._room_key_from_player_state(player_state),
                "context_key": turn_visibility.get("context_key"),
                "suppress_context": suppress_recent_context,
            }
        )
        narrator_turn_meta_payload = {
            "game_time": post_turn_game_time,
            "visibility": turn_visibility,
            "actor_player_slug": cls._player_slug_key(
                player_state.get("character_name")
            ),
            "location_key": cls._room_key_from_player_state(player_state),
            "context_key": turn_visibility.get("context_key"),
            "suppress_context": suppress_recent_context,
        }
        if reasoning:
            narrator_turn_meta_payload["reasoning"] = reasoning
        if summary_update:
            narrator_turn_meta_payload["summary_update"] = summary_update
        if scene_output:
            narrator_turn_meta_payload["scene_output"] = scene_output
            rendered_scene_text = cls._scene_output_rendered_text(
                scene_output
            )
            if rendered_scene_text:
                narrator_turn_meta_payload["scene_output_rendered"] = (
                    rendered_scene_text
                )
            narrator_turn_meta_payload["scene_output_jsonl"] = cls._scene_output_jsonl(
                turn_id=None,
                game_time=post_turn_game_time,
                scene_output=scene_output,
                turn_visibility=turn_visibility,
            )
        narrator_turn_meta = cls._dump_json(narrator_turn_meta_payload)

        # Don't store OOC meta-messages in turn history.
        _is_ooc = bool(re.match(r"\s*\[OOC\b", action, re.IGNORECASE))
        player_turn = None
        if not _is_ooc and stored_player_action:
            player_turn = ZorkTurn(
                campaign_id=campaign.id,
                user_id=ctx.author.id,
                kind="player",
                content=stored_player_action,
                channel_id=ctx.channel.id,
                meta_json=player_turn_meta,
            )
            db.session.add(player_turn)
        narrator_turn = ZorkTurn(
            campaign_id=campaign.id,
            user_id=ctx.author.id,
            kind="narrator",
            content=persisted_narration,
            channel_id=ctx.channel.id,
            meta_json=narrator_turn_meta,
        )
        db.session.add(narrator_turn)
        db.session.flush()
        if scene_output:
            narrator_turn_meta_payload["scene_output_jsonl"] = cls._scene_output_jsonl(
                turn_id=narrator_turn.id,
                game_time=post_turn_game_time,
                scene_output=scene_output,
                turn_visibility=turn_visibility,
            )
            narrator_turn.meta_json = cls._dump_json(
                narrator_turn_meta_payload
            )
        if player_turn is not None:
            cls._record_turn_game_time(
                campaign_state,
                player_turn.id,
                pre_turn_game_time,
            )
        cls._record_turn_game_time(
            campaign_state,
            narrator_turn.id,
            post_turn_game_time,
        )

        # State version increment
        campaign_state["_state_version"] = campaign_state.get("_state_version", 0) + 1
        campaign.state_json = cls._dump_json(campaign_state)
        db.session.commit()

        cls._create_snapshot(narrator_turn, campaign)

        # Fire-and-forget: embed the narrator turn for memory search.
        try:
            if not suppress_recent_context:
                ZorkMemory.store_turn_embedding(
                    narrator_turn.id,
                    campaign.id,
                    ctx.author.id,
                    "narrator",
                    persisted_narration,
                    metadata=cls._turn_embedding_metadata(
                        visibility=turn_visibility,
                        actor_player_slug=player_state.get("character_name"),
                        location_key=cls._room_key_from_player_state(player_state),
                        channel_id=ctx.channel.id,
                    ),
                )
        except Exception:
            logger.debug(
                "Zork memory embedding skipped for turn %s",
                narrator_turn.id,
                exc_info=True,
            )

        # Scene image enqueue
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

        # Time jump notifications
        if isinstance(time_jump_notification, dict):
            asyncio.ensure_future(
                cls._send_private_dm_time_jump_notifications(
                    campaign_name=str(
                        time_jump_notification.get("campaign_name") or campaign.name
                    ),
                    recipient_user_ids=list(
                        time_jump_notification.get("recipient_user_ids") or []
                    ),
                    from_time=dict(
                        time_jump_notification.get("from_time") or {}
                    ),
                    to_time=dict(
                        time_jump_notification.get("to_time") or {}
                    ),
                    delta_minutes=int(
                        time_jump_notification.get("delta_minutes") or 0
                    ),
                    event_summary=str(
                        time_jump_notification.get("event_summary")
                        or "Shared time advanced."
                    ),
                )
            )

        # Calendar event notifications
        if calendar_event_notifications:
            asyncio.ensure_future(
                cls._send_calendar_event_notifications(
                    campaign_id=campaign.id,
                    campaign_name=campaign.name,
                    notifications=calendar_event_notifications,
                    preferred_channel_id=(
                        int(ctx.channel.id)
                        if getattr(ctx, "guild", None) is not None
                        else None
                    ),
                )
            )

        return display_narration

    @classmethod
    async def _play_action_llm(
        cls,
        preflight: "PreflightContext",
        campaign,
        campaign_state: Dict[str, Any],
    ) -> "TurnDelta":
        """
        Phase 2: LLM execution. Runs WITHOUT holding any lock.

        Handles the tool loop, response parsing, and validation retries.
        Returns a TurnDelta with all extracted data.

        For deferred tools (plot_plan, chapter_plan, consequence_log, sms_write,
        sms_read), changes are applied to the local campaign_state dict and
        recorded in delta.deferred_tool_writes for Phase 3 to commit.

        For timers (set_timer, sms_schedule), params are recorded in
        delta.timer_scheduled_* fields for Phase 3 to schedule.
        """
        # --- Unpack preflight context ---
        system_prompt = preflight.system_prompt
        user_prompt = preflight.user_prompt
        gpt = preflight.gpt
        action = preflight.action
        turns = preflight.turns
        party_snapshot = preflight.party_snapshot
        base_user_prompt = preflight.base_user_prompt
        turn_prompt_tail = preflight.turn_prompt_tail
        turn_tail_extra_lines = list(preflight.turn_tail_extra_lines)
        prompt_difficulty = preflight.prompt_difficulty
        current_prompt_stage = preflight.current_prompt_stage
        first_payload = preflight.first_payload
        memory_lookup_enabled = preflight.memory_lookup_enabled
        is_new_player = preflight.is_new_player
        turn_attachment_context = preflight.turn_attachment_context
        viewer_slug = preflight.viewer_slug
        viewer_location_key = preflight.viewer_location_key
        viewer_private_context_key = preflight.viewer_private_context_key
        viewer_private_context = preflight.viewer_private_context
        private_context_candidate = preflight.private_context_candidate
        sms_activity_detected = preflight.sms_activity_detected
        is_dm = preflight.is_dm
        channel_id = preflight.channel_id
        on_rails = preflight.on_rails
        is_timed_events_enabled = preflight.is_timed_events_enabled
        state_version = preflight.state_version
        player_state = dict(preflight.player_state)
        user_id = preflight.user_id
        player_attributes_json = preflight.player_attributes_json
        player_level = preflight.player_level
        player_xp = preflight.player_xp

        # --- Initialize TurnDelta ---
        delta = TurnDelta()
        delta.state_version_at_read = state_version
        delta.sms_activity_detected = sms_activity_detected

        # --- Local tracking variables ---
        auto_forced_memory_search = preflight.auto_forced_memory_search
        recent_turns_loaded = False
        memory_recall_help_emitted = False
        empty_response_repair_count = 0
        anti_echo_retry_count = 0
        used_tool_names: set = set()
        seen_tool_signatures: set = set()
        forced_planning_payload = None

        # Support chained tool calls
        tool_prompt_blocks: List[str] = []
        tool_augmented_prompt = user_prompt

        def _rebuild_tool_prompt() -> None:
            nonlocal tool_augmented_prompt
            tool_augmented_prompt = cls._recompose_prompt_with_tail(
                base_user_prompt,
                turn_prompt_tail,
                *tool_prompt_blocks,
            )

        def _append_tool_prompt(*blocks: object) -> None:
            nonlocal tool_augmented_prompt
            for block in blocks:
                text = str(block or "").strip()
                if text:
                    tool_prompt_blocks.append(text)
            tool_prompt_blocks[:] = [
                block
                for block in tool_prompt_blocks
                if not str(block or "").strip().startswith("TOOL_BUDGET:")
            ]
            if current_prompt_stage != cls.PROMPT_STAGE_FINAL:
                tool_prompt_blocks.append(_tool_budget_note())
            _rebuild_tool_prompt()
            _zork_log(
                "TURN AUGMENTED PROMPT",
                f"--- SYSTEM PROMPT ---\n{system_prompt}\n\n--- USER PROMPT ---\n{tool_augmented_prompt}",
            )

        async def _turn_model_call(
            prompt_text: str,
            *,
            thinking_enabled: bool,
            temperature: float = 0.8,
            max_tokens: int = 2048,
        ):
            return await gpt.turbo_completion(
                system_prompt,
                prompt_text,
                temperature=temperature,
                max_tokens=max_tokens,
                thinking_enabled=thinking_enabled,
            )

        _raw_turbo_completion = gpt.turbo_completion
        first_research_completion_pending = True

        async def _stage_aware_turbo_completion(role, prompt, **kwargs):
            nonlocal first_research_completion_pending
            explicit_thinking = kwargs.pop("thinking_enabled", None)
            if current_prompt_stage == cls.PROMPT_STAGE_RESEARCH:
                if first_research_completion_pending:
                    first_research_completion_pending = False
                    if explicit_thinking is None:
                        explicit_thinking = True
            elif current_prompt_stage == cls.PROMPT_STAGE_FINAL:
                if explicit_thinking is None:
                    explicit_thinking = True
            else:
                if explicit_thinking is None:
                    explicit_thinking = False
            return await _raw_turbo_completion(
                role,
                prompt,
                thinking_enabled=bool(explicit_thinking),
                **kwargs,
            )

        gpt.turbo_completion = _stage_aware_turbo_completion

        def _tool_budget_note() -> str:
            remaining = max(0, max_tool_chain_steps - tool_chain_steps)
            if remaining <= 0:
                return f"TOOL_BUDGET: 0 call(s) remaining. Return final JSON now (no tool_call)."
            if remaining == 1:
                if current_prompt_stage == cls.PROMPT_STAGE_FINAL:
                    return f"TOOL_BUDGET: 1 call(s) remaining. Return final JSON now (no tool_call)."
                return f"TOOL_BUDGET: 1 call(s) remaining. Return ready_to_write or final JSON now."
            return f"TOOL_BUDGET: {remaining} call(s) remaining."

        def _switch_prompt_stage(next_stage: str) -> None:
            nonlocal current_prompt_stage
            nonlocal system_prompt
            nonlocal user_prompt
            nonlocal base_user_prompt
            nonlocal turn_prompt_tail
            if next_stage == current_prompt_stage:
                return
            current_prompt_stage = next_stage
            _player_ns = SimpleNamespace(
                user_id=user_id,
                state_json=cls._dump_json(player_state),
                attributes_json=player_attributes_json,
                level=player_level,
                xp=player_xp,
            )
            system_prompt, user_prompt = cls.build_prompt(
                campaign,
                _player_ns,
                action,
                turns,
                party_snapshot=party_snapshot,
                is_new_player=is_new_player,
                turn_attachment_context=turn_attachment_context,
                turn_visibility_default="public",
                tail_extra_lines=turn_tail_extra_lines,
                prompt_stage=current_prompt_stage,
                channel_id=channel_id,
            )
            base_user_prompt = user_prompt
            turn_prompt_tail = cls._build_turn_prompt_tail(
                _player_ns,
                player_state,
                action,
                turn_attachment_context,
                cls._turn_stage_note(
                    prompt_difficulty,
                    current_prompt_stage,
                    campaign=campaign,
                    channel_id=channel_id,
                ),
                extra_lines=turn_tail_extra_lines,
            )
            _rebuild_tool_prompt()

        tool_chain_steps = 0
        max_tool_chain_steps = 6

        # Auto-forced memory search logic
        if (
            current_prompt_stage != cls.PROMPT_STAGE_BOOTSTRAP
            and not (first_payload and cls._is_tool_call(first_payload))
            and memory_lookup_enabled
            and cls._should_force_auto_memory_search(action)
        ):
            forced_queries = cls._derive_auto_memory_queries(
                action,
                player_state,
                party_snapshot,
                limit=4,
            )
            if forced_queries:
                first_payload = {
                    "tool_call": "memory_search",
                    "queries": forced_queries,
                }
                auto_forced_memory_search = True
                _zork_log(
                    "FORCED MEMORY SEARCH",
                    f"queries={forced_queries}",
                )

        # --- Main tool loop ---
        response = ""
        while (
            first_payload
            and cls._is_tool_call(first_payload)
            and tool_chain_steps < max_tool_chain_steps
        ):
            tool_chain_steps += 1
            tool_name = str(first_payload.get("tool_call") or "").strip()
            if tool_name:
                used_tool_names.add(tool_name)
            tool_signature = cls._tool_call_signature(first_payload)
            if tool_signature and tool_signature in seen_tool_signatures:
                _zork_log(
                    "TOOL DEDUP SKIP",
                    f"tool={tool_name!r} payload={tool_signature}",
                )
                tool_result_block = (
                    "TOOL_DEDUP_RESULT: duplicate tool_call payload already executed this turn. "
                    "Skipped duplicate execution.\n"
                    "Do NOT repeat identical tool calls. Use a distinct tool/payload or return final JSON (no tool_call)."
                )
                _append_tool_prompt(tool_result_block)
                response = await _turn_model_call(
                    tool_augmented_prompt,
                    thinking_enabled=False,
                )
                if not response:
                    response = "A hollow silence answers. Try again."
                else:
                    response = cls._clean_response(response)
                _zork_log("TOOL DEDUP AUGMENTED RESPONSE", response)
                json_text_tc = cls._extract_json(response)
                if not json_text_tc:
                    first_payload = None
                    break
                try:
                    first_payload = cls._parse_json_lenient(json_text_tc)
                except Exception:
                    first_payload = None
                    break
                continue
            if tool_signature:
                seen_tool_signatures.add(tool_signature)

            if (
                not memory_lookup_enabled
                and tool_name
                in {
                    "memory_search",
                    "memory_terms",
                    "memory_turn",
                    "memory_store",
                }
            ):
                tool_result_block = (
                    "MEMORY_TOOLS_DISABLED: Long-term memory lookup is currently disabled for this turn "
                    "(early campaign context is still within prompt budget). "
                    "Do NOT call memory_* tools; continue with direct context or use non-memory tools."
                )
                _append_tool_prompt(tool_result_block)
                response = await _turn_model_call(
                    tool_augmented_prompt,
                    thinking_enabled=False,
                )
                if not response:
                    response = "A hollow silence answers. Try again."
                else:
                    response = cls._clean_response(response)
                _zork_log("MEMORY TOOL DISABLED AUGMENTED RESPONSE", response)
                json_text_tc = cls._extract_json(response)
                if not json_text_tc:
                    first_payload = None
                    break
                try:
                    first_payload = cls._parse_json_lenient(json_text_tc)
                except Exception:
                    first_payload = None
                    break
                continue

            if tool_name == "recent_turns":
                recent_turns_loaded = True
                raw_player_slugs = first_payload.get("player_slugs")
                if isinstance(raw_player_slugs, list):
                    requested_player_slugs = {
                        cls._player_slug_key(item)
                        for item in raw_player_slugs
                        if cls._player_slug_key(item)
                    }
                elif isinstance(raw_player_slugs, str):
                    requested_player_slugs = {
                        cls._player_slug_key(raw_player_slugs)
                    } if cls._player_slug_key(raw_player_slugs) else set()
                else:
                    requested_player_slugs = set()
                raw_npc_slugs = first_payload.get("npc_slugs")
                if isinstance(raw_npc_slugs, list):
                    requested_npc_slugs = {
                        str(item or "").strip()
                        for item in raw_npc_slugs
                        if str(item or "").strip()
                    }
                elif isinstance(raw_npc_slugs, str):
                    requested_npc_slugs = {
                        str(raw_npc_slugs).strip()
                    } if str(raw_npc_slugs).strip() else set()
                else:
                    requested_npc_slugs = set()
                try:
                    recent_limit = max(
                        1,
                        min(
                            48,
                            int(first_payload.get("limit") or cls.MAX_RECENT_TURNS),
                        ),
                    )
                except (TypeError, ValueError):
                    recent_limit = cls.MAX_RECENT_TURNS
                recent_turns = cls.get_recent_turns(
                    campaign.id,
                    limit=recent_limit,
                )
                recent_text = cls._recent_turns_text_for_viewer(
                    campaign,
                    recent_turns,
                    viewer_user_id=user_id,
                    viewer_slug=viewer_slug,
                    viewer_location_key=viewer_location_key,
                    viewer_private_context_key=viewer_private_context_key,
                    requested_player_slugs=requested_player_slugs,
                    requested_npc_slugs=requested_npc_slugs,
                )
                recent_location_hint = cls._recent_turns_location_hint(
                    recent_turns,
                    viewer_user_id=user_id,
                    viewer_slug=viewer_slug,
                    viewer_location_key=viewer_location_key,
                    viewer_private_context_key=viewer_private_context_key,
                )
                rt_meta = cls._compute_recent_turns_metadata(
                    recent_turns,
                    viewer_user_id=user_id,
                    viewer_slug=viewer_slug,
                )
                rt_note_lines = [
                    "RECENT_TURNS_NOTE: Immediate visible continuity for the acting player.",
                    "Local continuity is room-scoped; older local turns from other rooms may be missing.",
                ]
                rt_note_lines.append(
                    f"Loaded {rt_meta['turn_count']} turns spanning ~{rt_meta['time_span_minutes']} real minutes."
                )
                if rt_meta["active_speakers"]:
                    rt_note_lines.append(
                        f"Active speakers: {', '.join(rt_meta['active_speakers'])}."
                    )
                if rt_meta["active_listeners"]:
                    listener_strs = [
                        f"{name}({count})" for name, count in rt_meta["active_listeners"]
                    ]
                    rt_note_lines.append(
                        f"Frequent listeners: {', '.join(listener_strs)}."
                    )
                if rt_meta["private_turn_count"]:
                    rt_note_lines.append(
                        f"{rt_meta['private_turn_count']} turn(s) contain private/limited exchanges."
                    )
                if rt_meta["viewer_last_turn_ago"] is not None:
                    rt_note_lines.append(
                        f"Viewer's last turn: {rt_meta['viewer_last_turn_ago']} turn(s) ago."
                    )
                tool_result_block = (
                    "RECENT_TURNS_LOADED: true\n"
                    + "\n".join(rt_note_lines) + "\n"
                    f"RECENT_TURNS_LOCATIONS: current={recent_location_hint.get('current_location_key')} "
                    f"last_other={recent_location_hint.get('last_other_location_key')}\n"
                    f"RECENT_TURNS_RECEIVERS: players={sorted(requested_player_slugs)} npcs={sorted(requested_npc_slugs)}\n"
                    f"RECENT_TURNS:\n{recent_text}\n"
                )
                _switch_prompt_stage(cls.PROMPT_STAGE_RESEARCH)
                _zork_log("RECENT TURNS BLOCK", tool_result_block)
                _append_tool_prompt(tool_result_block)
                response = await _turn_model_call(
                    tool_augmented_prompt,
                    thinking_enabled=True,
                )
                if not response:
                    response = "A hollow silence answers. Try again."
                else:
                    response = cls._clean_response(response)
                _zork_log("RECENT TURNS AUGMENTED RESPONSE", response)

            elif tool_name == "ready_to_write":
                _switch_prompt_stage(cls.PROMPT_STAGE_FINAL)
                tool_result_block = (
                    "RESEARCH_COMPLETE: Context gathering is complete.\n"
                    "Do NOT call any more tools now. Return final narration/state JSON directly.\n"
                    "REQUIRED fields: reasoning, scene_output, narration, state_update (with game_time/current_chapter/current_scene), summary_update.\n"
                    + cls.WRITING_CRAFT_PROMPT
                )
                _zork_log("READY TO WRITE", tool_result_block)
                _append_tool_prompt(tool_result_block)
                response = await _turn_model_call(
                    tool_augmented_prompt,
                    thinking_enabled=True,
                )
                if not response:
                    response = "A hollow silence answers. Try again."
                else:
                    response = cls._clean_response(response)
                _zork_log("FINALIZATION RESPONSE", response)

            elif tool_name == "communication_rules":
                raw_keys = first_payload.get("keys") or []
                if isinstance(raw_keys, str):
                    raw_keys = [raw_keys]
                requested_keys = [
                    str(key or "").strip().upper()
                    for key in raw_keys
                    if str(key or "").strip()
                ][:8]
                requested_set = set(requested_keys)
                lines = [
                    f"{rule_key}: {rule_text}"
                    for rule_key, rule_text in cls.DEFAULT_GM_COMMUNICATION_RULES.items()
                    if rule_key in requested_set
                ]
                if lines:
                    tool_result_block = (
                        "COMMUNICATION_RULES_RESULT:\n"
                        + "\n".join(lines)
                    )
                else:
                    tool_result_block = (
                        "COMMUNICATION_RULES_RESULT: no matching keys found. "
                        f"Available keys: {', '.join(cls.COMMUNICATION_RULE_KEYS)}"
                    )
                _zork_log("COMMUNICATION RULES BLOCK", tool_result_block)
                _append_tool_prompt(tool_result_block)
                response = await _turn_model_call(
                    tool_augmented_prompt,
                    thinking_enabled=False,
                )
                if not response:
                    response = "A hollow silence answers. Try again."
                else:
                    response = cls._clean_response(response)
                _zork_log("COMMUNICATION RULES AUGMENTED RESPONSE", response)

            elif tool_name == "memory_search":
                # Support both "queries": [...] and legacy "query": "..."
                raw_queries = first_payload.get("queries") or []
                if not raw_queries:
                    legacy = str(first_payload.get("query") or "").strip()
                    if legacy:
                        raw_queries = [legacy]
                queries = [
                    str(q).strip()
                    for q in raw_queries
                    if str(q).strip()
                ]
                try:
                    source_before_lines = int(
                        first_payload.get("before_lines", 0)
                    )
                except (TypeError, ValueError):
                    source_before_lines = 0
                try:
                    source_after_lines = int(
                        first_payload.get("after_lines", 0)
                    )
                except (TypeError, ValueError):
                    source_after_lines = 0
                source_before_lines = max(0, min(50, source_before_lines))
                source_after_lines = max(0, min(50, source_after_lines))
                category_scope = " ".join(
                    str(first_payload.get("category") or "").strip().lower().split()
                )
                interaction_participant_slug = None
                awareness_npc_slug = None
                visibility_scope_filter = None
                structured_turn_scope = False
                if category_scope in {"interaction", "interactions"}:
                    structured_turn_scope = True
                elif category_scope.startswith("interaction:"):
                    structured_turn_scope = True
                    interaction_participant_slug = cls._player_slug_key(
                        category_scope.split(":", 1)[1]
                    )
                elif category_scope.startswith("awareness:"):
                    structured_turn_scope = True
                    awareness_npc_slug = str(
                        category_scope.split(":", 1)[1] or ""
                    ).strip()
                elif category_scope.startswith("visibility:"):
                    structured_turn_scope = True
                    visibility_scope_filter = str(
                        category_scope.split(":", 1)[1] or ""
                    ).strip().lower()
                source_docs = ZorkMemory.list_source_material_documents(
                    campaign.id,
                    limit=cls.SOURCE_MATERIAL_MAX_DOCS_IN_PROMPT,
                )
                has_source_material = bool(source_docs)
                source_total_chunks = 0
                for row in source_docs:
                    try:
                        source_total_chunks += int(row.get("chunk_count") or 0)
                    except (TypeError, ValueError):
                        continue
                source_scope = False
                source_scope_key = None
                if category_scope in (
                    cls.SOURCE_MATERIAL_CATEGORY,
                    "source-material",
                ):
                    source_scope = True
                elif category_scope.startswith(
                    f"{cls.SOURCE_MATERIAL_CATEGORY}:"
                ):
                    source_scope = True
                    source_scope_key = ZorkMemory._normalize_source_document_key(
                        category_scope.split(":", 1)[1]
                    )
                roster_hints = (
                    cls._record_memory_search_usage_and_hints(campaign, queries)
                    if queries
                    else []
                )
                recall_records: List[Dict[str, object]] = []
                if queries:
                    _zork_log(
                        "MEMORY SEARCH",
                        f"queries={queries}\ncategory={category_scope or '(none)'}",
                    )
                    try:
                        backfilled = ZorkMemory.backfill_campaign(campaign.id)
                    except Exception:
                        backfilled = 0
                    if backfilled:
                        _zork_log(
                            "MEMORY BACKFILL",
                            f"campaign={campaign.id} refreshed_turns={backfilled}",
                        )
                    seen_turn_ids: set = set()
                    seen_source_hits: set = set()
                    for query in queries:
                        logger.info(
                            "Zork memory search requested: campaign=%s query=%r",
                            campaign.id,
                            query,
                        )
                        results = ZorkMemory.search(
                            query,
                            campaign.id,
                            top_k=5,
                            viewer_user_id=user_id,
                            viewer_player_slug=cls._player_slug_key(
                                player_state.get("character_name")
                            ),
                            viewer_location_key=cls._room_key_from_player_state(
                                player_state
                            ),
                            participant_slug=interaction_participant_slug,
                            aware_npc_slug=awareness_npc_slug,
                            visibility_scope=visibility_scope_filter,
                        )
                        if results:
                            _zork_log(
                                f"MEMORY SCORES query={query!r}",
                                "\n".join(
                                    "  turn="
                                    f"{int(row.get('turn_id') or 0)} "
                                    f"score={float(row.get('score') or 0.0):.3f} "
                                    f"scope={str(row.get('visibility_scope') or 'public')} "
                                    f"actor={str(row.get('actor_player_slug') or '-') or '-'} "
                                    f"{str(row.get('content') or '')[:80]}"
                                    for row in results
                                ),
                            )
                        # Keep only results above relevance threshold.
                        relevant = [
                            row
                            for row in results
                            if float(row.get("score") or 0.0) >= 0.35
                            and int(row.get("turn_id") or 0) not in seen_turn_ids
                        ]
                        # Sort chronologically so the model sees events in order.
                        relevant.sort(key=lambda row: int(row.get("turn_id") or 0))
                        query_records: List[Dict[str, object]] = []
                        for row in relevant:
                            turn_id = int(row.get("turn_id") or 0)
                            kind = str(row.get("kind") or "")
                            content = str(row.get("content") or "")
                            score = float(row.get("score") or 0.0)
                            actor_slug = str(row.get("actor_player_slug") or "").strip()
                            turn_scope = str(row.get("visibility_scope") or "public").strip()
                            location_key = str(row.get("location_key") or "").strip()
                            seen_turn_ids.add(turn_id)
                            meta_bits = [f"relevance {score:.2f}"]
                            if actor_slug:
                                meta_bits.append(f"actor {actor_slug}")
                            if turn_scope and turn_scope != "public":
                                meta_bits.append(f"visibility {turn_scope}")
                            if location_key:
                                meta_bits.append(f"location {location_key}")
                            query_records.append(
                                {
                                    "kind": "memory_hit",
                                    "query": query,
                                    "memory_type": "turn",
                                    "turn_id": turn_id,
                                    "turn_kind": kind,
                                    "relevance": round(score, 3),
                                    "actor_player_slug": actor_slug or None,
                                    "visibility_scope": turn_scope or "public",
                                    "location_key": location_key or None,
                                    "text": cls._memory_tool_text_value(
                                        content,
                                        max_chars=300,
                                    ),
                                }
                            )
                        manual_records: List[Dict[str, object]] = []
                        if category_scope and not source_scope and not structured_turn_scope:
                            manual_hits = ZorkMemory.search_manual_memories(
                                query,
                                campaign.id,
                                category=category_scope,
                                top_k=5,
                            )
                            for mem_category, mem_content, mem_score in manual_hits:
                                if mem_score < 0.35:
                                    continue
                                manual_records.append(
                                    {
                                        "kind": "memory_hit",
                                        "query": query,
                                        "memory_type": "manual",
                                        "category": mem_category,
                                        "relevance": round(float(mem_score or 0.0), 3),
                                        "text": cls._memory_tool_text_value(
                                            mem_content,
                                            max_chars=300,
                                        ),
                                    }
                                )
                        source_records: List[Dict[str, object]] = []
                        if has_source_material and (
                            source_scope or not category_scope
                        ):
                            source_hits = ZorkMemory.search_source_material(
                                query,
                                campaign.id,
                                document_key=source_scope_key,
                                top_k=10 if source_scope else 6,
                                before_lines=source_before_lines,
                                after_lines=source_after_lines,
                            )
                            for (
                                source_doc_key,
                                source_doc_label,
                                source_chunk_index,
                                source_chunk_text,
                                source_score,
                            ) in source_hits:
                                if source_score < 0.40:
                                    continue
                                source_hit_key = (
                                    str(source_doc_key or "").strip(),
                                    int(source_chunk_index or 0),
                                )
                                if source_hit_key in seen_source_hits:
                                    continue
                                seen_source_hits.add(source_hit_key)
                                source_text_lines = [
                                    line.strip()
                                    for line in str(source_chunk_text or "").splitlines()
                                    if line.strip()
                                ]
                                source_text = (
                                    "\n    ".join(source_text_lines)
                                    if source_text_lines
                                    else str(source_chunk_text or "").strip()
                                )
                                if len(source_text) > 4000:
                                    source_text = (
                                        source_text[:4000]
                                        .rsplit(" ", 1)[0]
                                        .strip()
                                        + "..."
                                    )
                                source_records.append(
                                    {
                                        "kind": "memory_hit",
                                        "query": query,
                                        "memory_type": "source",
                                        "document_key": source_doc_key,
                                        "document_label": source_doc_label,
                                        "chunk_index": int(source_chunk_index or 0),
                                        "relevance": round(float(source_score or 0.0), 3),
                                        "text": cls._memory_tool_text_value(
                                            source_text,
                                            max_chars=4000,
                                        ),
                                    }
                                )
                        if query_records or manual_records or source_records:
                            recall_records.append(
                                {
                                    "kind": "memory_query_result",
                                    "query": query,
                                    "category": category_scope or None,
                                    "hit_count": len(query_records)
                                    + len(manual_records)
                                    + len(source_records),
                                }
                            )
                            recall_records.extend(query_records)
                            recall_records.extend(manual_records)
                            recall_records.extend(source_records)
                        else:
                            recall_records.append(
                                {
                                    "kind": "memory_query_result",
                                    "query": query,
                                    "category": category_scope or None,
                                    "hit_count": 0,
                                }
                            )
                if recall_records:
                    tool_result_block = (
                        "MEMORY_RECALL:\n"
                        + cls._memory_tool_jsonl(recall_records)
                    )
                elif queries:
                    if category_scope:
                        if source_scope:
                            source_scope_label = (
                                f"'{cls.SOURCE_MATERIAL_CATEGORY}:{source_scope_key}'"
                                if source_scope_key
                                else f"'{cls.SOURCE_MATERIAL_CATEGORY}'"
                            )
                            tool_result_block = (
                                "MEMORY_RECALL: No relevant memories found "
                                f"in source material category {source_scope_label}."
                            )
                        else:
                            tool_result_block = (
                                "MEMORY_RECALL: No relevant memories found "
                                f"(including manual category '{category_scope}')."
                            )
                    else:
                        tool_result_block = "MEMORY_RECALL: No relevant memories found."
                else:
                    tool_result_block = "MEMORY_RECALL: No valid search queries were provided."
                emit_full_memory_help = not memory_recall_help_emitted
                if has_source_material and emit_full_memory_help:
                    source_index_lines = [
                        cls._memory_tool_jsonl(
                            [
                                {
                                    "kind": "source_index_meta",
                                    "document_count": len(source_docs),
                                    "snippet_count": source_total_chunks,
                                }
                            ]
                        )
                    ]
                    for row in source_docs[:5]:
                        source_format = cls._source_material_format_heuristic(
                            str(row.get("sample_chunk") or "")
                        )
                        source_index_lines.append(
                            cls._memory_tool_jsonl(
                                [
                                    {
                                        "kind": "source_index_entry",
                                        "document_key": row.get("document_key"),
                                        "document_label": row.get("document_label"),
                                        "format": source_format,
                                        "snippet_count": int(row.get("chunk_count") or 0),
                                    }
                                ]
                            )
                        )
                    tool_result_block = (
                        f"{tool_result_block}\n"
                        + "\n".join(source_index_lines)
                    )
                if emit_full_memory_help:
                    tool_result_block = (
                        f"{tool_result_block}"
                        "\nMEMORY_RECALL_NEXT_ACTIONS:\n"
                        "- To retrieve FULL text for a specific hit turn number:\n"
                        '  {"tool_call": "memory_turn", "turn_id": 1234}\n'
                        "- To discover curated memory categories/terms before narrowing search:\n"
                        '  {"tool_call": "memory_terms", "wildcard": "char:*"}\n'
                        "- To search inside one curated category after term discovery:\n"
                        '  {"tool_call": "memory_search", "category": "char:character-slug", "queries": ["keyword1", "keyword2"]}\n'
                        "- To search narrator memories for interactions involving a player slug:\n"
                        '  {"tool_call": "memory_search", "category": "interaction:player-slug", "queries": ["argument", "kiss", "deal"]}\n'
                        "- To search for turns noticed by a specific NPC slug:\n"
                        '  {"tool_call": "memory_search", "category": "awareness:npc-slug", "queries": ["overheard", "promise", "threat"]}\n'
                        "- To restrict narrator-memory recall by visibility scope:\n"
                        '  {"tool_call": "memory_search", "category": "visibility:private", "queries": ["secret meeting"]}\n'
                        "- To inspect off-scene SMS communications:\n"
                        '  {"tool_call": "sms_list", "wildcard": "*"}\n'
                        '  {"tool_call": "sms_read", "thread": "contact-slug", "limit": 20}\n'
                        "- To schedule a delayed incoming SMS (hidden until it arrives):\n"
                        '  {"tool_call": "sms_schedule", "thread": "contact-slug", "from": "NPC", "to": "Player", "message": "...", "delay_seconds": 120}\n'
                    )
                    if has_source_material:
                        tool_result_block = (
                            f"{tool_result_block}"
                            "- To query indexed source-canon snippets (faithful adaptation):\n"
                            '  {"tool_call": "memory_search", "category": "source", "queries": ["character", "location", "event"]}\n'
                            "- To scope one source document only:\n"
                            '  {"tool_call": "memory_search", "category": "source:document-key", "queries": ["keyword1", "keyword2"]}\n'
                            "- To request expanded source context windows (default before_lines/after_lines are 0):\n"
                            '  {"tool_call": "memory_search", "category": "source:document-key", "queries": ["keyword1"], "before_lines": 5, "after_lines": 5}\n'
                            "- To browse all keys in a rulebook-format source document (list before drilling in):\n"
                            '  {"tool_call": "source_browse", "document_key": "document-key"}\n'
                            "- To filter rulebook keys by wildcard:\n"
                            '  {"tool_call": "source_browse", "document_key": "document-key", "wildcard": "keyword*"}\n'
                        )
                    memory_recall_help_emitted = True
                else:
                    tool_result_block = (
                        f"{tool_result_block}\n"
                        "MEMORY_RECALL_NEXT_ACTIONS: Use memory_turn for one hit, refine memory_search, or return final JSON."
                    )
                if roster_hints and emit_full_memory_help:
                    hint_lines = []
                    for hint in roster_hints[:6]:
                        term = str(hint.get("term") or hint.get("slug") or "").strip() or "unknown-term"
                        slug = str(hint.get("slug") or "").strip() or "character-slug"
                        try:
                            count = int(hint.get("count") or 0)
                        except (TypeError, ValueError):
                            count = 0
                        hint_lines.append(
                            "- You have searched for "
                            f"'{term}' {count} times and it is not in WORLD_CHARACTERS. "
                            "If this is stable/non-stale information and you confirm it, "
                            f"store it via character_updates using slug '{slug}'."
                        )
                    if hint_lines:
                        tool_result_block = (
                            f"{tool_result_block}\n"
                            "MEMORY_RECALL_ROSTER_RECOMMENDATIONS:\n"
                            + "\n".join(hint_lines)
                        )
                _zork_log("MEMORY RECALL BLOCK", tool_result_block)
                _append_tool_prompt(tool_result_block)
                response = await gpt.turbo_completion(
                    system_prompt,
                    tool_augmented_prompt,
                    temperature=0.8,
                    max_tokens=2048,
                )
                if not response:
                    response = "A hollow silence answers. Try again."
                else:
                    response = cls._clean_response(response)
                _zork_log("AUGMENTED API RESPONSE", response)

            elif tool_name == "memory_turn":
                turn_id_raw = (
                    first_payload.get("turn_id")
                    or first_payload.get("id")
                    or first_payload.get("turn")
                )
                try:
                    turn_id = int(turn_id_raw)
                except (TypeError, ValueError):
                    turn_id = 0
                if turn_id > 0:
                    target_turn = ZorkTurn.query.filter_by(
                        campaign_id=campaign.id,
                        id=turn_id,
                    ).first()
                else:
                    target_turn = None
                if target_turn is None:
                    tool_result_block = (
                        "MEMORY_TURN_RESULT:\n"
                        + cls._memory_tool_jsonl(
                            [
                                {
                                    "kind": "memory_turn_result",
                                    "status": "not_found",
                                    "turn_id": turn_id or None,
                                }
                            ]
                        )
                    )
                elif not cls._turn_visible_to_viewer(
                    target_turn,
                    user_id,
                    cls._player_slug_key(player_state.get("character_name")),
                    cls._room_key_from_player_state(player_state).lower(),
                ):
                    tool_result_block = (
                        "MEMORY_TURN_RESULT:\n"
                        + cls._memory_tool_jsonl(
                            [
                                {
                                    "kind": "memory_turn_result",
                                    "status": "not_visible",
                                    "turn_id": int(target_turn.id or 0) or None,
                                }
                            ]
                        )
                    )
                else:
                    full_text = (target_turn.content or "").strip()
                    if not full_text:
                        full_text = "(empty turn content)"
                    tool_result_block = (
                        "MEMORY_TURN_RESULT:\n"
                        + cls._memory_tool_jsonl(
                            [
                                {
                                    "kind": "memory_turn_result",
                                    "status": "ok",
                                    "turn_id": int(target_turn.id or 0) or None,
                                    "turn_kind": str(target_turn.kind or ""),
                                    "user_id": int(target_turn.user_id or 0) or None,
                                    "full_text": cls._memory_tool_text_value(
                                        full_text,
                                        max_chars=12000,
                                    ),
                                }
                            ]
                        )
                    )
                _zork_log("MEMORY TURN BLOCK", tool_result_block)
                _append_tool_prompt(tool_result_block)
                response = await gpt.turbo_completion(
                    system_prompt,
                    tool_augmented_prompt,
                    temperature=0.8,
                    max_tokens=2048,
                )
                if not response:
                    response = "A hollow silence answers. Try again."
                else:
                    response = cls._clean_response(response)
                _zork_log("MEMORY TURN AUGMENTED RESPONSE", response)

            elif tool_name == "memory_terms":
                wildcard = str(
                    first_payload.get("wildcard")
                    or first_payload.get("query")
                    or first_payload.get("term")
                    or "%"
                ).strip()[:80]
                terms = ZorkMemory.list_manual_memory_terms(
                    campaign.id,
                    wildcard=wildcard or "%",
                    limit=20,
                )
                if terms:
                    records = []
                    for row in terms:
                        records.append(
                            {
                                "kind": "memory_term",
                                "term": row.get("term"),
                                "category": row.get("category"),
                                "count": int(row.get("count") or 0),
                                "last_at": row.get("last_at"),
                            }
                        )
                    tool_result_block = (
                        f"MEMORY_TERMS_RESULT (wildcard={wildcard or '%'!r}):\n"
                        + cls._memory_tool_jsonl(records)
                    )
                else:
                    tool_result_block = (
                        f"MEMORY_TERMS_RESULT (wildcard={wildcard or '%'!r}): none"
                    )
                _zork_log("MEMORY TERMS BLOCK", tool_result_block)
                _append_tool_prompt(tool_result_block)
                response = await gpt.turbo_completion(
                    system_prompt,
                    tool_augmented_prompt,
                    temperature=0.8,
                    max_tokens=2048,
                )
                if not response:
                    response = "A hollow silence answers. Try again."
                else:
                    response = cls._clean_response(response)
                _zork_log("MEMORY TERMS AUGMENTED RESPONSE", response)

            elif tool_name == "memory_store":
                category = " ".join(
                    str(first_payload.get("category") or "").strip().lower().split()
                )[:120]
                term = " ".join(
                    str(first_payload.get("term") or "").strip().lower().split()
                )[:80]
                memory_text = str(
                    first_payload.get("memory")
                    or first_payload.get("content")
                    or ""
                ).strip()[:1200]
                wildcard = str(
                    first_payload.get("wildcard")
                    or term
                    or category
                    or "%"
                ).strip()[:80]
                pre_terms = ZorkMemory.list_manual_memory_terms(
                    campaign.id,
                    wildcard=wildcard or "%",
                    limit=20,
                )
                stored_ok = False
                store_reason = "missing_fields"
                if category and memory_text:
                    stored_ok, store_reason = ZorkMemory.store_manual_memory(
                        campaign.id,
                        category=category,
                        term=term or category,
                        content=memory_text,
                    )
                post_terms = ZorkMemory.list_manual_memory_terms(
                    campaign.id,
                    wildcard=wildcard or "%",
                    limit=20,
                )
                term_lines = []
                for row in pre_terms[:15]:
                    term_lines.append(
                        f"- term='{row.get('term')}' category='{row.get('category')}' count={row.get('count')}"
                    )
                if not term_lines:
                    term_lines.append("- none")
                status_text = (
                    "stored"
                    if stored_ok
                    else f"skipped ({store_reason})"
                )
                tool_result_block = (
                    "MEMORY_STORE_RESULT:\n"
                    f"- requested_category: {category or '(missing)'}\n"
                    f"- requested_term: {term or '(defaulted)'}\n"
                    f"- pre_store_wildcard: {wildcard or '%'}\n"
                    "- existing_terms_before:\n"
                    + "\n".join(term_lines)
                    + "\n"
                    f"- store_status: {status_text}\n"
                    f"- existing_terms_after_count: {len(post_terms)}"
                )
                _zork_log("MEMORY STORE BLOCK", tool_result_block)
                _append_tool_prompt(tool_result_block)
                response = await gpt.turbo_completion(
                    system_prompt,
                    tool_augmented_prompt,
                    temperature=0.8,
                    max_tokens=2048,
                )
                if not response:
                    response = "A hollow silence answers. Try again."
                else:
                    response = cls._clean_response(response)
                _zork_log("MEMORY STORE AUGMENTED RESPONSE", response)

            elif tool_name == "source_browse":
                browse_doc_key = str(
                    first_payload.get("document_key")
                    or first_payload.get("document")
                    or ""
                ).strip()[:120]
                browse_wildcard_raw = first_payload.get("wildcard")
                browse_wildcard = (
                    str(browse_wildcard_raw).strip()[:120]
                    if browse_wildcard_raw is not None
                    else ""
                )
                browse_wildcard_specified = bool(browse_wildcard)
                browse_wildcard = browse_wildcard or "%"
                wildcard_meta = f"wildcard={browse_wildcard!r}"
                if not browse_wildcard_specified:
                    wildcard_meta = "wildcard=(omitted)"
                browse_limit = 255
                try:
                    browse_limit = max(1, min(255, int(first_payload.get("limit") or 255)))
                except (TypeError, ValueError):
                    pass
                lines = ZorkMemory.browse_source_keys(
                    campaign.id,
                    document_key=browse_doc_key or None,
                    wildcard=browse_wildcard,
                    limit=browse_limit,
                )
                if lines:
                    tool_result_block = (
                        f"SOURCE_BROWSE_RESULT "
                        f"(document_key={browse_doc_key or '*'!r}, "
                        f"{wildcard_meta}, "
                        f"showing {len(lines)}):\n"
                        + "\n".join(lines)
                    )
                else:
                    tool_result_block = (
                        f"SOURCE_BROWSE_RESULT "
                        f"(document_key={browse_doc_key or '*'!r}, "
                        f"{wildcard_meta}): no entries found"
                    )
                _zork_log("SOURCE BROWSE BLOCK", tool_result_block)
                _append_tool_prompt(tool_result_block)
                response = await gpt.turbo_completion(
                    system_prompt,
                    tool_augmented_prompt,
                    temperature=0.8,
                    max_tokens=2048,
                )
                if not response:
                    response = "A hollow silence answers. Try again."
                else:
                    response = cls._clean_response(response)
                _zork_log("SOURCE BROWSE AUGMENTED RESPONSE", response)

            elif tool_name == "name_generate":
                raw_origins = first_payload.get("origins") or []
                if isinstance(raw_origins, str):
                    raw_origins = [raw_origins]
                origins = [
                    str(o).strip().lower()
                    for o in raw_origins
                    if str(o or "").strip()
                ][:4]
                ng_gender = str(
                    first_payload.get("gender") or "both"
                ).strip().lower()
                ng_count = 5
                try:
                    ng_count = max(1, min(6, int(first_payload.get("count") or 5)))
                except (TypeError, ValueError):
                    pass
                ng_context = str(
                    first_payload.get("context") or ""
                ).strip()[:300]
                names = cls._fetch_random_names(
                    origins=origins or None,
                    gender=ng_gender,
                    count=ng_count,
                )
                if names:
                    tool_result_block = (
                        f"NAME_GENERATE_RESULT "
                        f"(origins={origins or 'any'}, "
                        f"gender={ng_gender}, "
                        f"count={len(names)}):\n"
                        + "\n".join(f"- {n}" for n in names)
                        + "\n\nEvaluate these against your character concept"
                    )
                    if ng_context:
                        tool_result_block += (
                            f" ({ng_context})"
                        )
                    tool_result_block += (
                        ". Pick the best fit, or call name_generate again "
                        "with different origins/gender for more options."
                    )
                else:
                    tool_result_block = (
                        f"NAME_GENERATE_RESULT "
                        f"(origins={origins or 'any'}): "
                        "no names returned — try broader origins "
                        "or fewer filters."
                    )
                _zork_log("NAME GENERATE BLOCK", tool_result_block)
                _append_tool_prompt(tool_result_block)
                response = await gpt.turbo_completion(
                    system_prompt,
                    tool_augmented_prompt,
                    temperature=0.8,
                    max_tokens=2048,
                )
                if not response:
                    response = "A hollow silence answers. Try again."
                else:
                    response = cls._clean_response(response)
                _zork_log("NAME GENERATE AUGMENTED RESPONSE", response)

            elif tool_name == "plot_plan":
                # DEFERRED: Apply to local campaign_state, record in delta
                campaign_state_plot = dict(campaign_state)
                latest_turn_id = 0
                if isinstance(turns, list):
                    for turn in turns:
                        try:
                            latest_turn_id = max(
                                latest_turn_id, int(getattr(turn, "id", 0) or 0)
                            )
                        except (TypeError, ValueError):
                            continue
                plan_result = cls._apply_plot_plan_tool(
                    campaign_state_plot,
                    first_payload,
                    current_turn=latest_turn_id,
                )
                # Update local campaign_state
                campaign_state.update(campaign_state_plot)
                # Record deferred write
                delta.deferred_tool_writes.append({
                    "tool": "plot_plan",
                    "payload": first_payload,
                    "state_snapshot": dict(campaign_state_plot),
                })
                active_plans = list(plan_result.get("active") or [])
                lines = [
                    "PLOT_PLAN_RESULT:",
                    f"- updated: {int(plan_result.get('updated', 0) or 0)}",
                    f"- removed: {int(plan_result.get('removed', 0) or 0)}",
                    f"- total_threads: {int(plan_result.get('total', 0) or 0)}",
                    f"- active_threads: {len(active_plans)}",
                ]
                for row in active_plans[:8]:
                    lines.append(
                        "- "
                        f"thread='{row.get('thread')}' target_turns={row.get('target_turns')} "
                        f"setup=\"{row.get('setup')}\" payoff=\"{row.get('intended_payoff')}\""
                    )
                tool_result_block = "\n".join(lines)
                _zork_log("PLOT PLAN BLOCK", tool_result_block)
                _append_tool_prompt(tool_result_block)
                response = await gpt.turbo_completion(
                    system_prompt,
                    tool_augmented_prompt,
                    temperature=0.8,
                    max_tokens=2048,
                )
                if not response:
                    response = "A hollow silence answers. Try again."
                else:
                    response = cls._clean_response(response)
                _zork_log("PLOT PLAN AUGMENTED RESPONSE", response)

            elif tool_name == "chapter_plan":
                # DEFERRED: Apply to local campaign_state, record in delta
                campaign_state_chapter = dict(campaign_state)
                latest_turn_id = 0
                if isinstance(turns, list):
                    for turn in turns:
                        try:
                            latest_turn_id = max(
                                latest_turn_id, int(getattr(turn, "id", 0) or 0)
                            )
                        except (TypeError, ValueError):
                            continue
                plan_result = cls._apply_chapter_plan_tool(
                    campaign_state_chapter,
                    first_payload,
                    current_turn=latest_turn_id,
                    on_rails=bool(campaign_state_chapter.get("on_rails", False)),
                )
                if not bool(plan_result.get("ignored")):
                    # Update local campaign_state
                    campaign_state.update(campaign_state_chapter)
                    # Record deferred write
                    delta.deferred_tool_writes.append({
                        "tool": "chapter_plan",
                        "payload": first_payload,
                        "state_snapshot": dict(campaign_state_chapter),
                    })
                active_chapters = list(plan_result.get("active") or [])
                if bool(plan_result.get("ignored")):
                    tool_result_block = (
                        "CHAPTER_PLAN_RESULT: ignored.\n"
                        f"- reason: {plan_result.get('reason')}"
                    )
                else:
                    lines = [
                        "CHAPTER_PLAN_RESULT:",
                        f"- updated: {int(plan_result.get('updated', 0) or 0)}",
                        f"- total_chapters: {int(plan_result.get('total', 0) or 0)}",
                        f"- active_chapters: {len(active_chapters)}",
                    ]
                    for row in active_chapters[:8]:
                        lines.append(
                            "- "
                            f"chapter='{row.get('slug')}' title='{row.get('title')}' "
                            f"current_scene='{row.get('current_scene')}'"
                        )
                    tool_result_block = "\n".join(lines)
                _zork_log("CHAPTER PLAN BLOCK", tool_result_block)
                _append_tool_prompt(tool_result_block)
                response = await gpt.turbo_completion(
                    system_prompt,
                    tool_augmented_prompt,
                    temperature=0.8,
                    max_tokens=2048,
                )
                if not response:
                    response = "A hollow silence answers. Try again."
                else:
                    response = cls._clean_response(response)
                _zork_log("CHAPTER PLAN AUGMENTED RESPONSE", response)

            elif tool_name == "consequence_log":
                # DEFERRED: Apply to local campaign_state, record in delta
                campaign_state_cons = dict(campaign_state)
                latest_turn_id = 0
                if isinstance(turns, list):
                    for turn in turns:
                        try:
                            latest_turn_id = max(
                                latest_turn_id,
                                int(getattr(turn, "id", 0) or 0),
                            )
                        except (TypeError, ValueError):
                            continue
                cons_result = cls._apply_consequence_log_tool(
                    campaign_state_cons,
                    first_payload,
                    current_turn=latest_turn_id,
                )
                # Update local campaign_state
                campaign_state.update(campaign_state_cons)
                # Record deferred write
                delta.deferred_tool_writes.append({
                    "tool": "consequence_log",
                    "payload": first_payload,
                    "state_snapshot": dict(campaign_state_cons),
                })
                active_rows = list(cons_result.get("active") or [])
                lines = [
                    "CONSEQUENCE_LOG_RESULT:",
                    f"- added: {int(cons_result.get('added', 0) or 0)}",
                    f"- updated: {int(cons_result.get('updated', 0) or 0)}",
                    f"- resolved: {int(cons_result.get('resolved', 0) or 0)}",
                    f"- removed: {int(cons_result.get('removed', 0) or 0)}",
                    f"- total: {int(cons_result.get('total', 0) or 0)}",
                    f"- active: {len(active_rows)}",
                ]
                for row in active_rows[:8]:
                    expires = cls._coerce_non_negative_int(
                        row.get("expires_at_turn", 0), default=0
                    )
                    exp_text = (
                        f"expires@turn{expires}" if expires > 0 else "no-expiry"
                    )
                    lines.append(
                        "- "
                        f"id='{row.get('id')}' severity='{row.get('severity')}' {exp_text} "
                        f"consequence=\"{row.get('consequence')}\""
                    )
                tool_result_block = "\n".join(lines)
                _zork_log("CONSEQUENCE LOG BLOCK", tool_result_block)
                _append_tool_prompt(tool_result_block)
                response = await gpt.turbo_completion(
                    system_prompt,
                    tool_augmented_prompt,
                    temperature=0.8,
                    max_tokens=2048,
                )
                if not response:
                    response = "A hollow silence answers. Try again."
                else:
                    response = cls._clean_response(response)
                _zork_log("CONSEQUENCE LOG AUGMENTED RESPONSE", response)

            elif tool_name == "sms_list":
                sms_activity_detected = True
                wildcard = str(
                    first_payload.get("wildcard")
                    or first_payload.get("query")
                    or "*"
                ).strip()[:80] or "*"
                campaign_state_sms = dict(campaign_state)
                threads = cls._sms_list_threads(
                    campaign_state_sms, wildcard=wildcard, limit=20
                )
                if threads:
                    lines = []
                    for row in threads:
                        lines.append(
                            f"- thread='{row.get('thread')}' label='{row.get('label')}' "
                            f"count={row.get('count')} last_from='{row.get('last_from')}' "
                            f"last='Day {int(row.get('day', 0))} {int(row.get('hour', 0)):02d}:{int(row.get('minute', 0)):02d}' "
                            f"preview=\"{row.get('last_preview')}\""
                        )
                    tool_result_block = (
                        f"SMS_LIST_RESULT (wildcard={wildcard!r}):\n"
                        + "\n".join(lines)
                    )
                else:
                    tool_result_block = (
                        f"SMS_LIST_RESULT (wildcard={wildcard!r}): none"
                    )
                _zork_log("SMS LIST BLOCK", tool_result_block)
                _append_tool_prompt(tool_result_block)
                response = await gpt.turbo_completion(
                    system_prompt,
                    tool_augmented_prompt,
                    temperature=0.8,
                    max_tokens=2048,
                )
                if not response:
                    response = "A hollow silence answers. Try again."
                else:
                    response = cls._clean_response(response)
                _zork_log("SMS LIST AUGMENTED RESPONSE", response)

            elif tool_name == "sms_read":
                # DEFERRED: Apply to local campaign_state, record in delta
                sms_activity_detected = True
                thread = str(
                    first_payload.get("thread")
                    or first_payload.get("contact")
                    or first_payload.get("conversation")
                    or ""
                ).strip()[:80]
                raw_limit = first_payload.get("limit", 20)
                try:
                    read_limit = int(raw_limit)
                except (TypeError, ValueError):
                    read_limit = 20
                read_limit = max(1, min(40, read_limit))
                campaign_state_sms = dict(campaign_state)
                thread_key, thread_label, messages = cls._sms_read_thread(
                    campaign_state_sms, thread=thread, limit=read_limit
                )
                thread_markers: Dict[str, int] = {}
                if messages:
                    for msg in messages:
                        if not isinstance(msg, dict):
                            continue
                        msg_thread = cls._sms_normalize_thread_key(
                            msg.get("thread") or thread_key or thread
                        )
                        if not msg_thread:
                            continue
                        seq = cls._coerce_non_negative_int(
                            msg.get("seq", 0), default=0
                        )
                        turn_id_msg = cls._coerce_non_negative_int(
                            msg.get("turn_id", 0), default=0
                        )
                        marker = seq if seq > 0 else turn_id_msg
                        if marker <= 0:
                            continue
                        prev_marker = cls._coerce_non_negative_int(
                            thread_markers.get(msg_thread, 0), default=0
                        )
                        if marker > prev_marker:
                            thread_markers[msg_thread] = marker
                if thread_markers:
                    read_changed = cls._sms_mark_threads_read(
                        campaign_state_sms,
                        actor_id=user_id,
                        player_state=player_state,
                        thread_markers=thread_markers,
                    )
                    if read_changed:
                        # Update local campaign_state
                        campaign_state.update(campaign_state_sms)
                        # Record deferred write
                        delta.deferred_tool_writes.append({
                            "tool": "sms_read",
                            "thread_markers": thread_markers,
                        })
                if thread_key is None:
                    tool_result_block = (
                        f"SMS_READ_RESULT: thread not found for query '{thread or '(empty)'}'."
                    )
                elif not messages:
                    tool_result_block = (
                        f"SMS_READ_RESULT: thread '{thread_label}' has no messages."
                    )
                else:
                    lines = []
                    for msg in messages:
                        lines.append(
                            f"- [Day {int(msg.get('day', 0))} {int(msg.get('hour', 0)):02d}:{int(msg.get('minute', 0)):02d}] "
                            f"{msg.get('from')} -> {msg.get('to')}: {msg.get('message')}"
                        )
                    tool_result_block = (
                        f"SMS_READ_RESULT (thread='{thread_key}', label='{thread_label}'):\n"
                        + "\n".join(lines)
                    )
                _zork_log("SMS READ BLOCK", tool_result_block)
                _append_tool_prompt(tool_result_block)
                response = await gpt.turbo_completion(
                    system_prompt,
                    tool_augmented_prompt,
                    temperature=0.8,
                    max_tokens=2048,
                )
                if not response:
                    response = "A hollow silence answers. Try again."
                else:
                    response = cls._clean_response(response)
                _zork_log("SMS READ AUGMENTED RESPONSE", response)

            elif tool_name == "sms_write":
                # DEFERRED: Apply to local campaign_state, record in delta
                sms_activity_detected = True
                thread = str(
                    first_payload.get("thread")
                    or first_payload.get("contact")
                    or first_payload.get("conversation")
                    or ""
                ).strip()[:80]
                sender = str(
                    first_payload.get("from")
                    or first_payload.get("sender")
                    or player_state.get("character_name")
                    or f"Player {user_id}"
                ).strip()[:80]
                recipient = str(
                    first_payload.get("to")
                    or first_payload.get("recipient")
                    or thread
                ).strip()[:80]
                message_text = str(
                    first_payload.get("message")
                    or first_payload.get("content")
                    or first_payload.get("text")
                    or ""
                ).strip()
                if not message_text:
                    tool_result_block = (
                        "SMS_WRITE_RESULT: skipped (missing message text)."
                    )
                else:
                    campaign_state_sms = dict(campaign_state)
                    game_time_sms = cls._extract_game_time_snapshot(
                        campaign_state_sms
                    )
                    thread_key, thread_label, entry = cls._sms_write(
                        campaign_state_sms,
                        thread=thread or recipient or sender,
                        sender=sender,
                        recipient=recipient,
                        message=message_text,
                        game_time=game_time_sms,
                        turn_id=0,
                    )
                    # Update local campaign_state
                    campaign_state.update(campaign_state_sms)
                    # Record deferred write
                    delta.deferred_tool_writes.append({
                        "tool": "sms_write",
                        "thread": thread_key,
                        "entry": entry,
                        "state_snapshot": dict(campaign_state_sms),
                    })
                    tool_result_block = (
                        "SMS_WRITE_RESULT: stored.\n"
                        f"- thread='{thread_key}' label='{thread_label}'\n"
                        f"- at Day {int(entry.get('day', 0))} {int(entry.get('hour', 0)):02d}:{int(entry.get('minute', 0)):02d}\n"
                        f"- {entry.get('from')} -> {entry.get('to')}: {entry.get('message')}"
                    )
                _zork_log("SMS WRITE BLOCK", tool_result_block)
                _append_tool_prompt(tool_result_block)
                response = await gpt.turbo_completion(
                    system_prompt,
                    tool_augmented_prompt,
                    temperature=0.8,
                    max_tokens=2048,
                )
                if not response:
                    response = "A hollow silence answers. Try again."
                else:
                    response = cls._clean_response(response)
                _zork_log("SMS WRITE AUGMENTED RESPONSE", response)

            elif tool_name == "sms_schedule":
                # Record timer params in delta for Phase 3 to schedule
                sms_activity_detected = True
                thread = str(
                    first_payload.get("thread")
                    or first_payload.get("contact")
                    or first_payload.get("conversation")
                    or ""
                ).strip()[:80]
                sender = str(
                    first_payload.get("from")
                    or first_payload.get("sender")
                    or thread
                ).strip()[:80]
                recipient = str(
                    first_payload.get("to")
                    or first_payload.get("recipient")
                    or player_state.get("character_name")
                    or f"Player {user_id}"
                ).strip()[:80]
                message_text = str(
                    first_payload.get("message")
                    or first_payload.get("content")
                    or first_payload.get("text")
                    or ""
                ).strip()
                raw_delay_seconds = first_payload.get(
                    "delay_seconds",
                    first_payload.get("delay"),
                )
                raw_delay_minutes = first_payload.get("delay_minutes")
                if raw_delay_seconds is None and raw_delay_minutes is not None:
                    try:
                        raw_delay_seconds = int(raw_delay_minutes) * 60
                    except (TypeError, ValueError):
                        raw_delay_seconds = None
                try:
                    delay_seconds = int(raw_delay_seconds)
                except (TypeError, ValueError):
                    delay_seconds = 90
                _speed = cls.get_speed_multiplier(campaign)
                if _speed > 0:
                    delay_seconds = int(delay_seconds / _speed)
                delay_seconds = max(15, min(86_400, delay_seconds))

                if not thread or not sender or not recipient or not message_text:
                    tool_result_block = (
                        "SMS_SCHEDULE_RESULT: skipped (missing thread/from/to/message)."
                    )
                else:
                    # Record for Phase 3 to schedule
                    delta.deferred_tool_writes.append({
                        "tool": "sms_schedule",
                        "thread": thread,
                        "sender": sender,
                        "recipient": recipient,
                        "message": message_text,
                        "delay_seconds": delay_seconds,
                    })
                    tool_result_block = (
                        "SMS_SCHEDULE_RESULT: scheduled.\n"
                        f"- thread='{thread}'\n"
                        f"- from='{sender}' to='{recipient}'\n"
                        f"- delay_seconds={delay_seconds}\n"
                        "- delivery_visibility=hidden_until_delivery\n"
                        "- interruptible=false\n"
                        "Do NOT narrate this delayed SMS as already received in the current scene."
                    )
                _zork_log("SMS SCHEDULE BLOCK", tool_result_block)
                _append_tool_prompt(tool_result_block)
                response = await gpt.turbo_completion(
                    system_prompt,
                    tool_augmented_prompt,
                    temperature=0.8,
                    max_tokens=2048,
                )
                if not response:
                    response = "A hollow silence answers. Try again."
                else:
                    response = cls._clean_response(response)
                _zork_log("SMS SCHEDULE AUGMENTED RESPONSE", response)

            elif (
                tool_name == "set_timer"
                and is_timed_events_enabled
            ):
                # Record timer params in delta for Phase 3 to schedule
                raw_delay = first_payload.get("delay_seconds", 60)
                try:
                    delay_seconds = int(raw_delay)
                except (TypeError, ValueError):
                    delay_seconds = 60
                _speed = cls.get_speed_multiplier(campaign)
                if _speed > 0:
                    delay_seconds = int(delay_seconds / _speed)
                delay_seconds = max(15, min(300, delay_seconds))
                delay_seconds = cls._compress_realtime_timer_delay(
                    delay_seconds
                )
                event_description = str(
                    first_payload.get("event_description")
                    or "Something happens."
                ).strip()[:500]
                interruptible = bool(
                    first_payload.get(
                        "interruptible",
                        first_payload.get("set_timer_interruptible", True),
                    )
                )
                interrupt_action = first_payload.get(
                    "interrupt_action",
                    first_payload.get("set_timer_interrupt_action"),
                )
                if isinstance(interrupt_action, str):
                    interrupt_action = interrupt_action.strip()[:500] or None
                else:
                    interrupt_action = None
                interrupt_scope = cls._normalize_timer_interrupt_scope(
                    first_payload.get("interrupt_scope")
                    or first_payload.get("set_timer_interrupt_scope")
                    or "global"
                )

                # Record timer params in delta
                delta.timer_scheduled_delay = delay_seconds
                delta.timer_scheduled_event = event_description
                delta.timer_scheduled_interruptible = interruptible
                delta.timer_scheduled_interrupt_scope = interrupt_scope
                delta.timer_scheduled_interrupt_action = interrupt_action
                delta.timer_scheduled_interrupt_user_id = user_id

                logger.info(
                    "Zork timer recorded in delta: campaign=%s delay=%ds event=%r interruptible=%s scope=%s",
                    campaign.id,
                    delay_seconds,
                    event_description,
                    interruptible,
                    interrupt_scope,
                )
                tool_result_block = (
                    "TIMER_SET (system confirmation): A timed event has been scheduled.\n"
                    f'In {delay_seconds} seconds, if the player has not acted: "{event_description}".\n'
                    "Now narrate the current scene. Hint at urgency narratively but do NOT include "
                    "countdowns, timestamps, emoji clocks, or explicit seconds — the system adds its own countdown."
                )
                _zork_log(
                    "TIMER TOOL CALL",
                    (
                        f"delay={delay_seconds}s event={event_description!r} "
                        f"interruptible={interruptible} scope={interrupt_scope}"
                    ),
                )
                _append_tool_prompt(tool_result_block)
                response = await gpt.turbo_completion(
                    system_prompt,
                    tool_augmented_prompt,
                    temperature=0.8,
                    max_tokens=2048,
                )
                if not response:
                    response = "A hollow silence answers. Try again."
                else:
                    response = cls._clean_response(response)

            elif tool_name == "story_outline":
                chapter_slug = str(first_payload.get("chapter") or "").strip()
                _zork_log(
                    "STORY OUTLINE TOOL CALL",
                    f"chapter={chapter_slug!r}",
                )
                outline_result = ""
                campaign_state_so = dict(campaign_state)
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
                tool_result_block = (
                    f"STORY_OUTLINE_RESULT (chapter={chapter_slug}):\n{outline_result}\n"
                )
                _append_tool_prompt(tool_result_block)
                response = await gpt.turbo_completion(
                    system_prompt,
                    tool_augmented_prompt,
                    temperature=0.8,
                    max_tokens=2048,
                )
                if not response:
                    response = "A hollow silence answers. Try again."
                else:
                    response = cls._clean_response(response)
                _zork_log("STORY OUTLINE AUGMENTED RESPONSE", response)

            else:
                _zork_log(
                    "UNKNOWN TOOL CALL",
                    f"tool={tool_name!r} payload={json.dumps(first_payload, ensure_ascii=True)}",
                )
                break

            json_text_tc = cls._extract_json(response)
            if not json_text_tc:
                first_payload = None
                break
            try:
                first_payload = cls._parse_json_lenient(json_text_tc)
            except Exception:
                first_payload = None
                break

        # Hard-stop infinite tool loops
        if first_payload and cls._is_tool_call(first_payload) and tool_chain_steps >= max_tool_chain_steps:
            _switch_prompt_stage(cls.PROMPT_STAGE_FINAL)
            _append_tool_prompt(
                "TOOL_CHAIN_LIMIT_REACHED (0 remaining): Stop calling tools now. Return final narration/state JSON directly.\n"
                "REQUIRED fields: reasoning, scene_output, narration, state_update (with game_time/current_chapter/current_scene), summary_update."
            )
            response = await gpt.turbo_completion(
                system_prompt,
                tool_augmented_prompt,
                temperature=0.8,
                max_tokens=2048,
            )
            if not response:
                response = "A hollow silence answers. Try again."
            else:
                response = cls._clean_response(response)
            _zork_log("TOOL CHAIN LIMIT FINAL RESPONSE", response)
            json_text_tc = cls._extract_json(response)
            if json_text_tc:
                try:
                    first_payload = cls._parse_json_lenient(json_text_tc)
                except Exception:
                    first_payload = None

        # Last-resort guard: if we're still holding a bare tool_call payload,
        # force a final non-tool narration/state response
        if first_payload and cls._is_tool_call(first_payload):
            unresolved_tool = str(first_payload.get("tool_call") or "unknown")
            _switch_prompt_stage(cls.PROMPT_STAGE_FINAL)
            _append_tool_prompt(
                f"UNRESOLVED_TOOL_CALL: {unresolved_tool}\n"
                "Do NOT call any tools now. Return final narration/state JSON directly, including reasoning.\n"
                "REQUIRED fields: reasoning, scene_output, narration, state_update (with game_time/current_chapter/current_scene), summary_update."
            )
            response = await gpt.turbo_completion(
                system_prompt,
                tool_augmented_prompt,
                temperature=0.8,
                max_tokens=2048,
            )
            if not response:
                response = "A hollow silence answers. Try again."
            else:
                response = cls._clean_response(response)
            _zork_log("UNRESOLVED TOOL FINAL RESPONSE", response)
            json_text_tc = cls._extract_json(response)
            first_payload = None
            if json_text_tc:
                try:
                    first_payload = cls._parse_json_lenient(json_text_tc)
                except Exception:
                    first_payload = None

        # Fallback: LLM returned set_timer alongside narration
        if (
            first_payload
            and isinstance(first_payload, dict)
            and str(first_payload.get("tool_call") or "").strip()
            == "set_timer"
            and "narration" in first_payload
            and is_timed_events_enabled
        ):
            raw_delay = first_payload.get("delay_seconds", 60)
            try:
                delay_seconds = int(raw_delay)
            except (TypeError, ValueError):
                delay_seconds = 60
            delay_seconds = max(30, min(300, delay_seconds))
            delay_seconds = cls._compress_realtime_timer_delay(
                delay_seconds
            )
            event_description = str(
                first_payload.get("event_description")
                or "Something happens."
            ).strip()[:500]
            interruptible = bool(
                first_payload.get(
                    "interruptible",
                    first_payload.get("set_timer_interruptible", True),
                )
            )
            interrupt_action = first_payload.get(
                "interrupt_action",
                first_payload.get("set_timer_interrupt_action"),
            )
            if isinstance(interrupt_action, str):
                interrupt_action = interrupt_action.strip()[:500] or None
            else:
                interrupt_action = None
            interrupt_scope = cls._normalize_timer_interrupt_scope(
                first_payload.get("interrupt_scope")
                or first_payload.get("set_timer_interrupt_scope")
                or "global"
            )

            # Record timer params in delta
            delta.timer_scheduled_delay = delay_seconds
            delta.timer_scheduled_event = event_description
            delta.timer_scheduled_interruptible = interruptible
            delta.timer_scheduled_interrupt_scope = interrupt_scope
            delta.timer_scheduled_interrupt_action = interrupt_action
            delta.timer_scheduled_interrupt_user_id = user_id

            logger.info(
                "Zork timer recorded in delta (with narration): campaign=%s delay=%ds event=%r interruptible=%s scope=%s",
                campaign.id,
                delay_seconds,
                event_description,
                interruptible,
                interrupt_scope,
            )

        # --- Parse final response ---
        narration = response.strip()
        reasoning = None
        state_update: Dict[str, Any] = {}
        summary_update = None
        xp_awarded = 0
        player_state_update: Dict[str, Any] = {}
        co_located_player_slugs: List[str] = []
        story_progression = None
        turn_visibility = None
        scene_output_raw = None
        scene_image_prompt = None
        character_updates: Dict[str, Any] = {}
        give_item = None
        calendar_update = None

        json_text = cls._extract_json(response)
        if json_text:
            try:
                payload = cls._parse_json_lenient(json_text)
                scene_output_raw = payload.get("scene_output")
                narration_candidate = str(
                    payload.get("narration") or ""
                ).strip()
                if not narration_candidate:
                    narration_candidate = cls._scene_output_text_from_raw(
                        scene_output_raw
                    )
                if not narration_candidate:
                    narration_candidate = cls._fallback_narration_from_payload(
                        payload
                    )
                if narration_candidate:
                    narration = narration_candidate
                reasoning = cls._sanitize_reasoning(
                    payload.get("reasoning")
                )
                state_update = payload.get("state_update", {}) or {}
                summary_update = payload.get("summary_update")
                xp_awarded = payload.get("xp_awarded", 0) or 0
                player_state_update = (
                    payload.get("player_state_update", {}) or {}
                )
                co_located_player_slugs = cls._normalize_co_located_player_slugs(
                    payload.get("co_located_player_slugs"),
                    actor_slug=player_state.get("character_name"),
                )
                story_progression = cls._normalize_story_progression(
                    payload.get("story_progression")
                )
                turn_visibility = payload.get("turn_visibility")
                scene_image_prompt = payload.get("scene_image_prompt")
                character_updates = (
                    payload.get("character_updates", {}) or {}
                )
                give_item = payload.get("give_item")
                calendar_update = payload.get("calendar_update")

                # Inline timed event fields
                inline_timer_delay = payload.get("set_timer_delay")
                inline_timer_event = payload.get("set_timer_event")
                if (
                    inline_timer_delay is not None
                    and inline_timer_event
                    and is_timed_events_enabled
                    and delta.timer_scheduled_delay is None  # Don't override if already set
                ):
                    try:
                        t_delay = int(inline_timer_delay)
                    except (TypeError, ValueError):
                        t_delay = 60
                    _speed = cls.get_speed_multiplier(campaign)
                    if _speed > 0:
                        t_delay = int(t_delay / _speed)
                    t_delay = max(15, min(300, t_delay))
                    t_delay = cls._compress_realtime_timer_delay(
                        t_delay
                    )
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
                    t_interrupt_scope = cls._normalize_timer_interrupt_scope(
                        payload.get("set_timer_interrupt_scope", "global")
                    )
                    # Record timer params in delta
                    delta.timer_scheduled_delay = t_delay
                    delta.timer_scheduled_event = t_event
                    delta.timer_scheduled_interruptible = t_interruptible
                    delta.timer_scheduled_interrupt_scope = t_interrupt_scope
                    delta.timer_scheduled_interrupt_action = t_interrupt_action
                    delta.timer_scheduled_interrupt_user_id = user_id
                    _zork_log(
                        f"TIMER RECORDED campaign={campaign.id}",
                        f"delay={t_delay}s event={t_event!r} "
                        f"interruptible={t_interruptible} "
                        f"scope={t_interrupt_scope}",
                    )
                    logger.info(
                        "Zork timer recorded (inline): campaign=%s delay=%ds event=%r interruptible=%s scope=%s",
                        campaign.id,
                        t_delay,
                        t_event,
                        t_interruptible,
                        t_interrupt_scope,
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
                            scene_output_raw = payload.get("scene_output")
                            narration_candidate = str(
                                payload.get("narration") or ""
                            ).strip()
                            if not narration_candidate:
                                narration_candidate = cls._scene_output_text_from_raw(
                                    scene_output_raw
                                )
                            if not narration_candidate:
                                narration_candidate = (
                                    cls._fallback_narration_from_payload(
                                        payload
                                    )
                                )
                            if narration_candidate:
                                narration = narration_candidate
                            reasoning = cls._sanitize_reasoning(
                                payload.get("reasoning")
                            )
                            state_update = (
                                payload.get("state_update", {}) or {}
                            )
                            summary_update = payload.get("summary_update")
                            xp_awarded = payload.get("xp_awarded", 0) or 0
                            player_state_update = (
                                payload.get("player_state_update", {}) or {}
                            )
                            co_located_player_slugs = cls._normalize_co_located_player_slugs(
                                payload.get("co_located_player_slugs"),
                                actor_slug=player_state.get("character_name"),
                            )
                            story_progression = cls._normalize_story_progression(
                                payload.get("story_progression")
                            )
                            turn_visibility = payload.get("turn_visibility")
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

        # Safety: if narration still looks like raw JSON
        if narration.lstrip().startswith("{"):
            try:
                salvage = json.loads(cls._extract_json(narration) or "{}")
                if isinstance(salvage, dict) and salvage:
                    scene_output_raw = salvage.get("scene_output")
                    narration = str(salvage.get("narration", "")).strip()
                    reasoning = cls._sanitize_reasoning(
                        salvage.get("reasoning")
                    )
                    if not narration:
                        narration = cls._scene_output_text_from_raw(
                            scene_output_raw
                        )
                    if not narration:
                        narration = cls._fallback_narration_from_payload(
                            salvage
                        )
                    if not narration:
                        narration = "The world shifts, but nothing clear emerges."
            except (json.JSONDecodeError, Exception):
                narration = "The world shifts, but nothing clear emerges."
        if not str(narration or "").strip():
            narration = cls._fallback_narration_from_payload(
                {
                    "summary_update": summary_update,
                    "state_update": state_update,
                    "player_state_update": player_state_update,
                    "character_updates": character_updates,
                    "calendar_update": calendar_update,
                }
            ) or "The world shifts, but nothing clear emerges."

        # Guard: model sometimes returns chain-of-thought planning text
        if narration and cls._looks_like_reasoning(narration):
            _zork_log("REASONING LEAK RETRY", narration[:300])
            _retry_prompt = (
                f"{tool_augmented_prompt}\n"
                "Your previous response was internal reasoning, NOT player-facing narration. "
                "Return the actual in-character narration and JSON state now. "
                "Include a concise reasoning field.\n"
            )
            _retry_resp = await gpt.turbo_completion(
                system_prompt, _retry_prompt, temperature=0.8, max_tokens=2048,
            )
            if _retry_resp:
                _retry_resp = cls._clean_response(_retry_resp)
                if not cls._looks_like_reasoning(_retry_resp):
                    response = _retry_resp
                    narration = response.strip()
                    _rj = cls._extract_json(response)
                    if _rj:
                        try:
                            _rp = cls._parse_json_lenient(_rj)
                            scene_output_raw = _rp.get("scene_output")
                            _rn = str(_rp.get("narration") or "").strip()
                            if not _rn:
                                _rn = cls._scene_output_text_from_raw(
                                    scene_output_raw
                                )
                            if not _rn:
                                _rn = cls._fallback_narration_from_payload(_rp)
                            if _rn:
                                narration = _rn
                            reasoning = cls._sanitize_reasoning(
                                _rp.get("reasoning")
                            )
                            state_update = _rp.get("state_update", {}) or {}
                            summary_update = _rp.get("summary_update")
                            xp_awarded = _rp.get("xp_awarded", 0) or 0
                            player_state_update = _rp.get("player_state_update", {}) or {}
                            co_located_player_slugs = cls._normalize_co_located_player_slugs(
                                _rp.get("co_located_player_slugs"),
                                actor_slug=player_state.get("character_name"),
                            )
                            story_progression = cls._normalize_story_progression(
                                _rp.get("story_progression")
                            )
                            turn_visibility = _rp.get("turn_visibility")
                            scene_image_prompt = _rp.get("scene_image_prompt")
                            character_updates = _rp.get("character_updates", {}) or {}
                            give_item = _rp.get("give_item")
                            calendar_update = _rp.get("calendar_update")
                        except Exception:
                            pass

        # Empty payload retry
        if cls._is_emptyish_turn_payload(
            narration=narration,
            state_update=state_update
            if isinstance(state_update, dict)
            else {},
            player_state_update=player_state_update
            if isinstance(player_state_update, dict)
            else {},
            summary_update=summary_update,
            xp_awarded=xp_awarded,
            scene_image_prompt=scene_image_prompt,
            character_updates=character_updates
            if isinstance(character_updates, dict)
            else {},
            calendar_update=calendar_update,
        ):
            empty_response_repair_count += 1
            _zork_log(
                "EMPTY PAYLOAD RETRY",
                (
                    f"narration={str(narration or '')[:220]!r}\n"
                    f"state_keys={list((state_update or {}).keys()) if isinstance(state_update, dict) else []}\n"
                    f"player_state_keys={list((player_state_update or {}).keys()) if isinstance(player_state_update, dict) else []}"
                ),
            )
            _repair_prompt = (
                f"{tool_augmented_prompt}\n"
                "OUTPUT_VALIDATION_FAILED: previous response was too empty.\n"
                "Return final JSON now (no tool_call), including:\n"
                "- reasoning string grounded in evidence/context used\n"
                "- narration with one concrete scene development\n"
                '- state_update object with "game_time", "current_chapter", and "current_scene" explicitly included\n'
                "- summary_update string (REQUIRED — one sentence of lasting change or current dramatic state)\n"
                "Advance game_time plausibly and restate current_chapter/current_scene even if unchanged.\n"
            )
            _repair_response = await gpt.turbo_completion(
                system_prompt,
                _repair_prompt,
                temperature=0.75,
                max_tokens=2048,
            )
            if _repair_response:
                _repair_response = cls._clean_response(_repair_response)
                _repair_json = cls._extract_json(_repair_response)
                if _repair_json:
                    try:
                        _repair_payload = cls._parse_json_lenient(
                            _repair_json
                        )
                        scene_output_raw = _repair_payload.get(
                            "scene_output"
                        )
                        _repair_narration = str(
                            _repair_payload.get("narration") or ""
                        ).strip()
                        if not _repair_narration:
                            _repair_narration = cls._scene_output_text_from_raw(
                                scene_output_raw
                            )
                        if not _repair_narration:
                            _repair_narration = (
                                cls._fallback_narration_from_payload(
                                    _repair_payload
                                )
                            )
                        if _repair_narration:
                            narration = _repair_narration
                        reasoning = cls._sanitize_reasoning(
                            _repair_payload.get("reasoning")
                        )
                        state_update = (
                            _repair_payload.get("state_update", {}) or {}
                        )
                        summary_update = _repair_payload.get(
                            "summary_update"
                        )
                        xp_awarded = (
                            _repair_payload.get("xp_awarded", 0) or 0
                        )
                        player_state_update = (
                            _repair_payload.get("player_state_update", {})
                            or {}
                        )
                        co_located_player_slugs = cls._normalize_co_located_player_slugs(
                            _repair_payload.get("co_located_player_slugs"),
                            actor_slug=player_state.get("character_name"),
                        )
                        story_progression = cls._normalize_story_progression(
                            _repair_payload.get("story_progression")
                        )
                        turn_visibility = _repair_payload.get("turn_visibility")
                        scene_image_prompt = _repair_payload.get(
                            "scene_image_prompt"
                        )
                        character_updates = (
                            _repair_payload.get("character_updates", {})
                            or {}
                        )
                        give_item = _repair_payload.get("give_item")
                        calendar_update = _repair_payload.get(
                            "calendar_update"
                        )
                        _zork_log(
                            "EMPTY PAYLOAD REPAIR RESPONSE",
                            _repair_response[:1200],
                        )
                    except Exception:
                        pass

        # Clock drift retry
        if (
            cls._narration_has_explicit_clock_time(narration)
            and not cls._action_requests_clock_time(action)
        ):
            _zork_log("CLOCK DRIFT RETRY", narration[:260])
            _clock_retry_prompt = (
                f"{tool_augmented_prompt}\n"
                "OUTPUT_VALIDATION_FAILED: Do not invent explicit HH:MM clock stamps. "
                "Use canonical CURRENT_GAME_TIME only, or avoid exact times in narration.\n"
                'Return final JSON (no tool_call) with reasoning and a state_update containing "game_time", "current_chapter", and "current_scene".\n'
                "Advance game_time plausibly and restate current_chapter/current_scene even if unchanged.\n"
            )
            _clock_retry_resp = await gpt.turbo_completion(
                system_prompt,
                _clock_retry_prompt,
                temperature=0.75,
                max_tokens=2048,
            )
            if _clock_retry_resp:
                _clock_retry_resp = cls._clean_response(_clock_retry_resp)
                _clock_retry_json = cls._extract_json(_clock_retry_resp)
                if _clock_retry_json:
                    try:
                        _clock_payload = cls._parse_json_lenient(
                            _clock_retry_json
                        )
                        scene_output_raw = _clock_payload.get(
                            "scene_output"
                        )
                        _clock_narration = str(
                            _clock_payload.get("narration") or ""
                        ).strip()
                        if not _clock_narration:
                            _clock_narration = cls._scene_output_text_from_raw(
                                scene_output_raw
                            )
                        if not _clock_narration:
                            _clock_narration = cls._fallback_narration_from_payload(
                                _clock_payload
                            )
                        if _clock_narration:
                            narration = _clock_narration
                        reasoning = cls._sanitize_reasoning(
                            _clock_payload.get("reasoning")
                        )
                        state_update = _clock_payload.get("state_update", {}) or {}
                        summary_update = _clock_payload.get("summary_update")
                        xp_awarded = _clock_payload.get("xp_awarded", 0) or 0
                        player_state_update = (
                            _clock_payload.get("player_state_update", {}) or {}
                        )
                        co_located_player_slugs = cls._normalize_co_located_player_slugs(
                            _clock_payload.get("co_located_player_slugs"),
                            actor_slug=player_state.get("character_name"),
                        )
                        story_progression = cls._normalize_story_progression(
                            _clock_payload.get("story_progression")
                        )
                        turn_visibility = _clock_payload.get("turn_visibility")
                        scene_image_prompt = _clock_payload.get("scene_image_prompt")
                        character_updates = _clock_payload.get("character_updates", {}) or {}
                        give_item = _clock_payload.get("give_item")
                        calendar_update = _clock_payload.get("calendar_update")
                        empty_response_repair_count += 1
                    except Exception:
                        pass

        # Anti-echo retry
        _anti_echo_retry, _anti_echo_reason = cls._anti_echo_retry_decision(
            action,
            narration,
        )
        if _anti_echo_retry:
            _zork_log("ANTI-ECHO RETRY", _anti_echo_reason)
            _anti_echo_prompt = (
                f"{tool_augmented_prompt}\n"
                "OUTPUT_VALIDATION_FAILED: previous narration echoed/paraphrased player wording.\n"
                "Do NOT restate player phrasing. NPC first line should add new information, a decision, "
                "a demand, or a consequence. A direct question is valid only if the NPC truly still lacks enough to react.\n"
                'Return final JSON (no tool_call) with reasoning and a state_update containing "game_time", "current_chapter", and "current_scene".\n'
                "Advance game_time plausibly and restate current_chapter/current_scene even if unchanged.\n"
            )
            _anti_echo_resp = await gpt.turbo_completion(
                system_prompt,
                _anti_echo_prompt,
                temperature=0.75,
                max_tokens=2048,
            )
            if _anti_echo_resp:
                _anti_echo_resp = cls._clean_response(_anti_echo_resp)
                _anti_echo_json = cls._extract_json(_anti_echo_resp)
                if _anti_echo_json:
                    try:
                        _anti_payload = cls._parse_json_lenient(
                            _anti_echo_json
                        )
                        scene_output_raw = _anti_payload.get(
                            "scene_output"
                        )
                        _anti_narration = str(
                            _anti_payload.get("narration") or ""
                        ).strip()
                        if not _anti_narration:
                            _anti_narration = cls._scene_output_text_from_raw(
                                scene_output_raw
                            )
                        if not _anti_narration:
                            _anti_narration = cls._fallback_narration_from_payload(
                                _anti_payload
                            )
                        if _anti_narration:
                            narration = _anti_narration
                        reasoning = cls._sanitize_reasoning(
                            _anti_payload.get("reasoning")
                        )
                        state_update = (
                            _anti_payload.get("state_update", {}) or {}
                        )
                        summary_update = _anti_payload.get("summary_update")
                        xp_awarded = _anti_payload.get("xp_awarded", 0) or 0
                        player_state_update = (
                            _anti_payload.get("player_state_update", {}) or {}
                        )
                        co_located_player_slugs = cls._normalize_co_located_player_slugs(
                            _anti_payload.get("co_located_player_slugs"),
                            actor_slug=player_state.get("character_name"),
                        )
                        story_progression = cls._normalize_story_progression(
                            _anti_payload.get("story_progression")
                        )
                        turn_visibility = _anti_payload.get("turn_visibility")
                        scene_image_prompt = _anti_payload.get("scene_image_prompt")
                        character_updates = _anti_payload.get("character_updates", {}) or {}
                        give_item = _anti_payload.get("give_item")
                        calendar_update = _anti_payload.get("calendar_update")
                        anti_echo_retry_count += 1
                    except Exception:
                        pass

        # --- Post-processing ---
        state_update = cls._ensure_minimum_state_update_contract(
            campaign_state,
            state_update,
        )
        state_update, calendar_update = cls._extract_calendar_update_from_state_update(
            state_update,
            calendar_update,
        )

        # Normalize turn visibility
        turn_visibility = cls._normalize_turn_visibility(
            campaign,
            SimpleNamespace(user_id=user_id, state_json=cls._dump_json(player_state)),
            turn_visibility,
            is_private_context=is_dm,
        )
        turn_visibility = cls._promote_player_npc_slugs(
            turn_visibility, campaign.id,
        )
        private_context_candidate = cls._derive_private_context_candidate(
            campaign,
            SimpleNamespace(user_id=user_id, state_json=cls._dump_json(player_state)),
            player_state,
            action,
        )
        if private_context_candidate is not None:
            turn_visibility = cls._apply_private_context_candidate(
                turn_visibility,
                private_context_candidate,
            )
        stored_player_action, private_phone_redacted = (
            cls._redact_private_phone_command_lines(action)
        )
        if sms_activity_detected or private_phone_redacted:
            turn_visibility = cls._force_private_visibility_for_phone_activity(
                turn_visibility,
                actor_slug=str(
                    turn_visibility.get("actor_player_slug")
                    or cls._player_slug_key(
                        player_state.get("character_name")
                    )
                    or ""
                ).strip(),
                actor_user_id=user_id,
            )
        suppress_recent_context = bool(
            sms_activity_detected or private_phone_redacted
        )
        ephemeral_notices: List[str] = []
        if (
            private_context_candidate is not None
            and bool(private_context_candidate.get("warning"))
        ):
            ephemeral_notices.append(
                "Warning: if you include the real whisper/private content in the same setup message, it may leak before the aside is fully established. Use one short setup turn first, then continue once the reply keeps it private."
            )
        cls._persist_private_context_state(
            player_state,
            turn_visibility,
            action,
            private_context_candidate,
        )

        _planning_tools_used = bool(
            {"plot_plan", "chapter_plan", "consequence_log"}
            & set(used_tool_names)
        )
        raw_narration = narration

        narration = cls._trim_text(narration, cls.MAX_NARRATION_CHARS)
        narration = cls._strip_inventory_from_narration(narration)
        if sms_activity_detected or private_phone_redacted:
            turn_visibility["aware_npc_slugs"] = []
        else:
            inferred_aware_npc_slugs = cls._infer_aware_npc_slugs(
                campaign,
                player_state,
                turn_visibility,
                narration_text=raw_narration,
                summary_update=summary_update,
                private_context_candidate=private_context_candidate,
            )
            if inferred_aware_npc_slugs:
                turn_visibility["aware_npc_slugs"] = inferred_aware_npc_slugs
        turn_visibility = cls._sanitize_turn_awareness_for_scene(
            campaign,
            player_state,
            turn_visibility,
        )
        _resolved_turn_keys = cls._resolved_turn_visibility_keys(
            turn_visibility,
            scene_output_raw=scene_output_raw,
            player_state_update=player_state_update,
            fallback_location_key=cls._room_key_from_player_state(
                player_state
            ),
        )
        turn_visibility["location_key"] = _resolved_turn_keys.get(
            "location_key"
        )
        turn_visibility["context_key"] = _resolved_turn_keys.get(
            "context_key"
        )

        _zork_log(
            f"TURN RESULT campaign={campaign.id}",
            f"--- REASONING ---\n{reasoning}\n\n"
            f"--- NARRATION ---\n{narration}\n\n"
            f"--- STATE UPDATE ---\n{json.dumps(state_update, indent=2)}\n\n"
            f"--- PLAYER STATE UPDATE ---\n{json.dumps(player_state_update, indent=2)}\n\n"
            f"--- CO-LOCATED PLAYER SLUGS ---\n{json.dumps(co_located_player_slugs, indent=2)}\n\n"
            f"--- STORY PROGRESSION ---\n{json.dumps(story_progression, indent=2)}\n\n"
            f"--- TURN VISIBILITY ---\n{json.dumps(turn_visibility, indent=2)}\n\n"
            f"--- SUMMARY UPDATE ---\n{summary_update}\n\n"
            f"--- XP AWARDED ---\n{xp_awarded}\n"
            f"--- SCENE IMAGE PROMPT ---\n{scene_image_prompt}\n",
        )

        state_update, player_state_update = cls._rescue_misplaced_room_state(
            state_update, player_state_update
        )
        state_update, player_state_update = cls._split_room_state(
            state_update, player_state_update
        )
        player_state_update = cls._sync_game_time_to_player_state(
            state_update, player_state_update
        )
        state_update = cls._scrub_inventory_from_state(state_update)

        # --- Build and return TurnDelta ---
        delta.narration = narration
        delta.raw_narration = raw_narration
        delta.scene_output = scene_output_raw
        delta.scene_output_raw = scene_output_raw
        delta.reasoning = reasoning
        delta.state_update = state_update
        delta.player_state_update = player_state_update
        delta.co_located_player_slugs = co_located_player_slugs
        delta.character_updates = character_updates
        delta.summary_update = summary_update
        delta.calendar_update = calendar_update
        delta.give_item = give_item
        delta.xp_awarded = xp_awarded
        delta.turn_visibility = turn_visibility
        delta.story_progression = story_progression
        delta.scene_image_prompt = scene_image_prompt
        delta.ephemeral_notices = ephemeral_notices
        delta.used_tool_names = used_tool_names
        delta.forced_planning_payload = forced_planning_payload
        delta.auto_forced_memory_search = auto_forced_memory_search
        delta.empty_response_repair_count = empty_response_repair_count
        delta.anti_echo_retry_count = anti_echo_retry_count
        delta.sms_activity_detected = sms_activity_detected
        delta.private_phone_redacted = private_phone_redacted
        delta.stored_player_action = stored_player_action
        delta.suppress_recent_context = suppress_recent_context

        return delta

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
        log_scope_token: Optional[str] = None
        if campaign_id is None:
            campaign_id, error_text = await cls.begin_turn(
                ctx, command_prefix=command_prefix
            )
            if error_text is not None:
                return error_text
            if campaign_id is None:
                return None
            should_clear_claim = True
        conc = cls._get_concurrency(campaign_id)

        try:
            # === Phase 1: PREFLIGHT (holds commit_lock — <100ms) ===
            async with conc.commit_lock:
                with app.app_context():
                    campaign = ZorkCampaign.query.get(campaign_id)
                    player = cls.get_or_create_player(
                        campaign_id, ctx.author.id, campaign=campaign
                    )
                    log_scope_token = cls._push_contextual_turn_log_scope(
                        campaign,
                        channel_id=(
                            None
                            if getattr(ctx, "guild", None) is None
                            else int(getattr(ctx.channel, "id", 0) or 0)
                        ),
                        user_id=ctx.author.id,
                        is_dm=getattr(ctx, "guild", None) is None,
                    )
                    cls._set_turn_ephemeral_notices(campaign_id, ctx.author.id, [])
                    cls.record_player_message(player, channel=ctx.channel)
                    player.last_active = db.func.now()
                    player.updated = db.func.now()
                    db.session.commit()

                    player_state = cls.get_player_state(player)
                    preflight = await cls._play_action_preflight(
                        ctx, action, campaign, player, player_state,
                        command_prefix=command_prefix,
                    )
                    if isinstance(preflight, str):
                        return preflight  # intercepted command / error / onboarding

                    # Snapshot campaign state for Phase 2 (local copy)
                    campaign_state_snapshot = dict(cls.get_campaign_state(campaign))
                    conc.inflight_count += 1

            # === Phase 2: LLM EXECUTION (NO lock held — 5-30s) ===
            # App context is needed for DB reads (stale reads OK), but the
            # commit_lock is NOT held so other turns can proceed concurrently.
            try:
                with app.app_context():
                    delta = await cls._play_action_llm(preflight, campaign, campaign_state_snapshot)
            finally:
                conc.inflight_count -= 1

            # === Phase 3: COMMIT (holds commit_lock — <100ms) ===
            async with conc.commit_lock:
                with app.app_context():
                    campaign = ZorkCampaign.query.get(campaign_id)
                    return await cls._play_action_commit(ctx, preflight, delta, campaign)
        finally:
            cls._pop_contextual_turn_log_scope(log_scope_token)
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
        interrupt_scope: str = "global",
        interrupt_user_id: Optional[int] = None,
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
            "interrupt_scope": cls._normalize_timer_interrupt_scope(interrupt_scope),
            "interrupt_user_id": str(interrupt_user_id) if interrupt_user_id is not None else None,
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
        preferred_user_id = None
        if timer_ctx is not None:
            raw_user_id = timer_ctx.get("interrupt_user_id")
            try:
                preferred_user_id = int(raw_user_id) if raw_user_id is not None else None
            except (TypeError, ValueError):
                preferred_user_id = None
        try:
            await cls._execute_timed_event(
                campaign_id,
                channel_id,
                event_description,
                preferred_user_id=preferred_user_id,
            )
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
        preferred_user_id: Optional[int] = None,
    ):
        app = AppConfig.get_flask()
        if app is None:
            return

        # Variables that must survive across phases
        timed_narrator_turn_id = None
        calendar_event_notifications: List[Dict[str, object]] = []
        campaign_name_for_notifications = f"campaign-{campaign_id}"
        log_scope_token: Optional[str] = None
        target_user_id: Optional[int] = None

        # Phase 1 outputs needed by Phase 2
        system_prompt: Optional[str] = None
        user_prompt: Optional[str] = None
        gpt = None
        action: Optional[str] = None
        active_player_user_id: Optional[int] = None
        timer_is_private_context: bool = False
        state_version: int = 0

        # Phase 2 outputs needed by Phase 3
        response: Optional[str] = None
        narration: str = ""
        reasoning = None
        state_update: Dict[str, object] = {}
        summary_update = None
        xp_awarded: int = 0
        player_state_update: Dict[str, object] = {}
        turn_visibility = None
        scene_output_raw = None
        character_updates: Dict[str, object] = {}
        calendar_update = None

        conc = cls._get_concurrency(campaign_id)

        try:
            # === Phase 1: PREFLIGHT (holds commit_lock — <100ms) ===
            async with conc.commit_lock:
                with app.app_context():
                    campaign = ZorkCampaign.query.get(campaign_id)
                    if campaign is None:
                        return
                    campaign_name_for_notifications = str(campaign.name or campaign_name_for_notifications)
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
                                cls._now() - latest_turn.created
                            ).total_seconds()
                            if age < 5:
                                return

                    # Prefer the originating player context for local/story-specific timers.
                    active_player = None
                    if preferred_user_id is not None:
                        active_player = ZorkPlayer.query.filter_by(
                            campaign_id=campaign_id,
                            user_id=preferred_user_id,
                        ).first()
                    if active_player is None:
                        active_player = (
                            ZorkPlayer.query.filter_by(campaign_id=campaign_id)
                            .order_by(ZorkPlayer.last_active.desc())
                            .first()
                        )
                    if active_player is None:
                        return
                    active_player_user_id = active_player.user_id
                    target_user_id = active_player_user_id
                    timer_is_private_context = (
                        ZorkChannel.query.filter_by(channel_id=channel_id).first() is None
                    )
                    log_scope_token = cls._push_contextual_turn_log_scope(
                        campaign,
                        channel_id=None if timer_is_private_context else channel_id,
                        user_id=active_player.user_id,
                        is_dm=timer_is_private_context,
                    )
                    _zork_log(
                        f"TIMER TARGET campaign={campaign_id}",
                        (
                            f"preferred_user_id={preferred_user_id} "
                            f"resolved_user_id={active_player.user_id}"
                        ),
                    )

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
                        turn_visibility_default="public",
                        channel_id=channel_id,
                    )

                    gpt = cls._new_gpt(campaign=campaign, channel_id=channel_id)

                    # Snapshot state version for optimistic concurrency
                    campaign_state = cls.get_campaign_state(campaign)
                    state_version = campaign_state.get("_state_version", 0)

                    conc.inflight_count += 1

            # === Phase 2: LLM EXECUTION (NO lock held — 5-30s) ===
            try:
              with app.app_context():
                response = await gpt.turbo_completion(
                    system_prompt, user_prompt, temperature=0.8, max_tokens=2048
                )
                if not response:
                    return
                response = cls._clean_response(response)

                narration = response.strip()
                reasoning = None
                state_update = {}
                summary_update = None
                xp_awarded = 0
                player_state_update = {}
                turn_visibility = None
                scene_output_raw = None
                character_updates = {}
                calendar_update = None

                json_text = cls._extract_json(response)
                if json_text:
                    try:
                        payload = cls._parse_json_lenient(json_text)
                        scene_output_raw = payload.get("scene_output")
                        narration_candidate = str(payload.get("narration") or "").strip()
                        if not narration_candidate:
                            narration_candidate = cls._scene_output_text_from_raw(
                                scene_output_raw
                            )
                        if not narration_candidate:
                            narration_candidate = cls._fallback_narration_from_payload(
                                payload
                            )
                        if narration_candidate:
                            narration = narration_candidate
                        reasoning = cls._sanitize_reasoning(payload.get("reasoning"))
                        state_update = payload.get("state_update", {}) or {}
                        summary_update = payload.get("summary_update")
                        xp_awarded = payload.get("xp_awarded", 0) or 0
                        player_state_update = (
                            payload.get("player_state_update", {}) or {}
                        )
                        turn_visibility = payload.get("turn_visibility")
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
                                    scene_output_raw = payload.get("scene_output")
                                    narration_candidate = str(
                                        payload.get("narration") or ""
                                    ).strip()
                                    if not narration_candidate:
                                        narration_candidate = cls._scene_output_text_from_raw(
                                            scene_output_raw
                                        )
                                    if not narration_candidate:
                                        narration_candidate = (
                                            cls._fallback_narration_from_payload(
                                                payload
                                            )
                                        )
                                    if narration_candidate:
                                        narration = narration_candidate
                                    reasoning = cls._sanitize_reasoning(
                                        payload.get("reasoning")
                                    )
                                    state_update = payload.get("state_update", {}) or {}
                                    summary_update = payload.get("summary_update")
                                    xp_awarded = payload.get("xp_awarded", 0) or 0
                                    player_state_update = (
                                        payload.get("player_state_update", {}) or {}
                                    )
                                    turn_visibility = payload.get("turn_visibility")
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
                            scene_output_raw = salvage.get("scene_output")
                            narration = str(salvage.get("narration", "")).strip()
                            reasoning = cls._sanitize_reasoning(
                                salvage.get("reasoning")
                            )
                            if not narration:
                                narration = cls._scene_output_text_from_raw(
                                    scene_output_raw
                                )
                            if not narration:
                                narration = cls._fallback_narration_from_payload(
                                    salvage
                                )
                            if not narration:
                                narration = "The world shifts, but nothing clear emerges."
                    except (json.JSONDecodeError, Exception):
                        narration = "The world shifts, but nothing clear emerges."
                if not str(narration or "").strip():
                    narration = cls._fallback_narration_from_payload(
                        {
                            "summary_update": summary_update,
                            "state_update": state_update,
                            "player_state_update": player_state_update,
                            "character_updates": character_updates,
                            "calendar_update": calendar_update,
                        }
                    ) or "The world shifts, but nothing clear emerges."
            finally:
                conc.inflight_count -= 1

            # === Phase 3: COMMIT (holds commit_lock — <100ms) ===
            async with conc.commit_lock:
                with app.app_context():
                    # Re-read authoritative state from DB
                    campaign = ZorkCampaign.query.get(campaign_id)
                    if campaign is None:
                        return
                    active_player = ZorkPlayer.query.filter_by(
                        campaign_id=campaign_id,
                        user_id=active_player_user_id,
                    ).first()
                    if active_player is None:
                        return

                    # Re-read campaign state and check version
                    campaign_state = cls.get_campaign_state(campaign)
                    current_version = campaign_state.get("_state_version", 0)
                    if current_version != state_version:
                        _zork_log(
                            f"STATE VERSION CHANGED (timed event) campaign={campaign_id}",
                            f"preflight_version={state_version} current_version={current_version}",
                        )

                    turn_visibility = cls._normalize_turn_visibility(
                        campaign,
                        active_player,
                        turn_visibility,
                        is_private_context=timer_is_private_context,
                    )
                    turn_visibility = cls._promote_player_npc_slugs(
                        turn_visibility, campaign.id,
                    )

                    # Get player state early for fallback location key
                    player_state = cls.get_player_state(active_player)

                    _resolved_turn_keys = cls._resolved_turn_visibility_keys(
                        turn_visibility,
                        scene_output_raw=scene_output_raw,
                        player_state_update=player_state_update,
                        fallback_location_key=cls._room_key_from_player_state(
                            player_state
                        ),
                    )
                    turn_visibility["location_key"] = _resolved_turn_keys.get("location_key")
                    turn_visibility["context_key"] = _resolved_turn_keys.get("context_key")
                    if (
                        " ".join(str(narration or "").lower().split())
                        == "the world shifts, but nothing clear emerges."
                    ):
                        _zork_log(
                            f"TIMED EVENT GENERIC FALLBACK campaign={campaign_id}",
                            f"event={event_description!r} active_user_id={active_player.user_id}",
                        )
                        narration = str(event_description or "Something happens.").strip()

                    narration = cls._trim_text(narration, cls.MAX_NARRATION_CHARS)
                    narration = cls._strip_inventory_from_narration(narration)

                    state_update, player_state_update = cls._rescue_misplaced_room_state(
                        state_update, player_state_update
                    )
                    state_update, player_state_update = cls._split_room_state(
                        state_update, player_state_update
                    )
                    player_state_update = cls._sync_game_time_to_player_state(
                        state_update, player_state_update
                    )
                    state_update = cls._scrub_inventory_from_state(state_update)
                    existing_chars_for_state_nulls = cls.get_campaign_characters(campaign)
                    resolution_context = (
                        f"{action}\n{narration}\n{summary_update or ''}"
                    )
                    state_update = cls._guard_state_null_character_prunes(
                        state_update,
                        existing_chars_for_state_nulls,
                        resolution_context=resolution_context,
                        campaign_state=campaign_state,
                    )
                    state_null_character_updates = cls._character_updates_from_state_nulls(
                        state_update,
                        existing_chars_for_state_nulls,
                    )
                    if state_null_character_updates:
                        merged_character_updates = dict(state_null_character_updates)
                        if isinstance(character_updates, dict):
                            merged_character_updates.update(character_updates)
                        character_updates = merged_character_updates

                    if isinstance(character_updates, dict) and character_updates:
                        character_updates = cls._sanitize_character_removals(
                            existing_chars_for_state_nulls,
                            character_updates,
                            resolution_context=resolution_context,
                            campaign_state=campaign_state,
                        )
                    state_update, calendar_update = cls._extract_calendar_update_from_state_update(
                        state_update,
                        calendar_update,
                    )
                    pre_turn_game_time = cls._extract_game_time_snapshot(campaign_state)

                    # Merge state update with conflict resolution (instead of _apply_state_update)
                    campaign_state = cls._merge_state_update_with_conflict_resolution(
                        campaign_state, state_update
                    )
                    campaign_state = cls._ensure_game_time_progress(
                        campaign_state,
                        pre_turn_game_time,
                        action_text=action,
                        narration_text=narration,
                    )
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
                            campaign_id=campaign.id,
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
                            campaign_state,
                            calendar_update,
                            resolution_context=resolution_context,
                            actor_user_id=active_player.user_id,
                        )
                        campaign.state_json = cls._dump_json(campaign_state)

                    _turn_is_public = (
                        str((turn_visibility or {}).get("scope") or "").strip().lower()
                        in {"public", "local"}
                    )

                    if summary_update:
                        summary_update = summary_update.strip()
                        summary_update = cls._strip_inventory_mentions(summary_update)
                        if not _turn_is_public:
                            _zork_log(
                                f"SUMMARY FILTERED (non-public timed event) campaign={campaign.id}",
                                summary_update,
                            )
                        elif not cls._scene_output_is_summary_public_safe(scene_output_raw):
                            _zork_log(
                                f"SUMMARY FILTERED (non-public beat timed event) campaign={campaign.id}",
                                summary_update,
                            )
                        elif cls._should_keep_summary_update(
                            summary_update,
                            state_update=state_update,
                            player_state_update=player_state_update,
                            character_updates=character_updates,
                            calendar_update=calendar_update,
                        ):
                            campaign.summary = cls._append_summary(
                                campaign.summary, summary_update
                            )
                        else:
                            _zork_log(
                                f"SUMMARY FILTERED (timed event) campaign={campaign.id}",
                                summary_update,
                            )

                    player_state_update = cls._sanitize_player_state_update(
                        player_state,
                        player_state_update,
                        action_text=action,
                        narration_text=narration,
                    )
                    player_state = cls._apply_state_update(
                        player_state, player_state_update
                    )
                    turn_visibility = cls._sanitize_turn_awareness_for_scene(
                        campaign,
                        player_state,
                        turn_visibility,
                    )
                    turn_visibility["location_key"] = (
                        cls._room_key_from_player_state(player_state).strip() or None
                    )
                    active_player.state_json = cls._dump_json(player_state)
                    cls._sync_main_party_room_state(
                        campaign.id,
                        active_player.user_id,
                        player_state,
                    )
                    _active_char_sync_count = cls._sync_active_player_character_location(
                        campaign,
                        player_state=player_state,
                    )
                    _state_loc_sync_count = cls._auto_sync_companion_locations(
                        campaign_state,
                        player_state=player_state,
                        narration_text=narration,
                    )
                    _char_loc_sync_count = cls._auto_sync_character_locations(
                        campaign,
                        player_state=player_state,
                        narration_text=narration,
                    )
                    _roster_overlay_sync_count = cls._sync_npc_locations_from_state_to_roster(
                        campaign, state_update
                    )
                    if _active_char_sync_count or _state_loc_sync_count or _char_loc_sync_count or _roster_overlay_sync_count:
                        cls._increment_auto_fix_counter(
                            campaign_state,
                            "location_auto_sync_active_character",
                            amount=_active_char_sync_count,
                        )
                        cls._increment_auto_fix_counter(
                            campaign_state,
                            "location_auto_sync_state_entities",
                            amount=_state_loc_sync_count,
                        )
                        cls._increment_auto_fix_counter(
                            campaign_state,
                            "location_auto_sync_world_characters",
                            amount=_char_loc_sync_count,
                        )
                        cls._increment_auto_fix_counter(
                            campaign_state,
                            "location_auto_sync_roster_overlay",
                            amount=_roster_overlay_sync_count,
                        )
                        _zork_log(
                            f"LOCATION AUTO-SYNC (timed event) campaign={campaign.id}",
                            (
                                f"active_player_character={_active_char_sync_count} "
                                f"state_entities={_state_loc_sync_count} "
                                f"world_characters={_char_loc_sync_count} "
                                f"roster_overlay={_roster_overlay_sync_count}"
                            ),
                        )
                    _player_entity_sync_count = cls._sync_player_states_from_campaign_entities(
                        campaign,
                        campaign_state,
                        skip_user_id=active_player.user_id,
                    )
                    if _player_entity_sync_count:
                        cls._increment_auto_fix_counter(
                            campaign_state,
                            "location_auto_sync_other_players",
                            amount=_player_entity_sync_count,
                        )
                        _zork_log(
                            f"PLAYER ENTITY AUTO-SYNC (timed event) campaign={campaign.id}",
                            f"other_players={_player_entity_sync_count}",
                        )

                    # Increment state version before commit
                    campaign_state["_state_version"] = campaign_state.get("_state_version", 0) + 1
                    campaign.state_json = cls._dump_json(campaign_state)

                    if isinstance(xp_awarded, int) and xp_awarded > 0:
                        active_player.xp += xp_awarded

                    scene_output = cls._normalize_scene_output(
                        campaign,
                        scene_output_raw,
                        fallback_narration=narration,
                        turn_visibility=turn_visibility,
                        fallback_location_key=cls._room_key_from_player_state(player_state),
                        actor_user_id=active_player.user_id,
                        actor_player_slug=str(
                            turn_visibility.get("actor_player_slug") or ""
                        ).strip(),
                    )
                    rendered_scene_output = cls._scene_output_rendered_text(scene_output)
                    if rendered_scene_output:
                        narration = rendered_scene_output
                    if scene_output:
                        _zork_log(
                            f"TIMED EVENT SCENE OUTPUT campaign={campaign.id}",
                            json.dumps(scene_output, indent=2, ensure_ascii=False),
                        )

                    narration = cls._strip_narration_footer(narration)
                    post_turn_game_time = cls._extract_game_time_snapshot(campaign_state)
                    calendar_event_notifications = cls._calendar_collect_fired_events(
                        campaign.id,
                        campaign_state,
                        from_time=pre_turn_game_time,
                        to_time=post_turn_game_time,
                    )
                    campaign.last_narration = narration
                    campaign.updated = db.func.now()
                    active_player.updated = db.func.now()
                    timed_turn_meta_payload = {
                        "game_time": post_turn_game_time,
                        "visibility": turn_visibility,
                        "actor_player_slug": cls._player_slug_key(
                            player_state.get("character_name")
                        ),
                        "location_key": cls._room_key_from_player_state(player_state),
                    }
                    if reasoning:
                        timed_turn_meta_payload["reasoning"] = reasoning
                    if summary_update:
                        timed_turn_meta_payload["summary_update"] = summary_update
                    if scene_output:
                        timed_turn_meta_payload["scene_output"] = scene_output
                        rendered_scene_text = cls._scene_output_rendered_text(scene_output)
                        if rendered_scene_text:
                            timed_turn_meta_payload["scene_output_rendered"] = (
                                rendered_scene_text
                            )
                        timed_turn_meta_payload["scene_output_jsonl"] = cls._scene_output_jsonl(
                            turn_id=None,
                            game_time=post_turn_game_time,
                            scene_output=scene_output,
                            turn_visibility=turn_visibility,
                        )

                    narrator_turn = ZorkTurn(
                        campaign_id=campaign.id,
                        user_id=None,
                        kind="narrator",
                        content=f"[TIMED EVENT] {narration}",
                        channel_id=channel_id,
                        meta_json=cls._dump_json(timed_turn_meta_payload),
                    )
                    db.session.add(narrator_turn)
                    db.session.flush()
                    if scene_output:
                        timed_turn_meta_payload["scene_output_jsonl"] = cls._scene_output_jsonl(
                            turn_id=narrator_turn.id,
                            game_time=post_turn_game_time,
                            scene_output=scene_output,
                            turn_visibility=turn_visibility,
                        )
                        narrator_turn.meta_json = cls._dump_json(timed_turn_meta_payload)
                    cls._record_turn_game_time(
                        campaign_state,
                        narrator_turn.id,
                        cls._extract_game_time_snapshot(campaign_state),
                    )
                    campaign.state_json = cls._dump_json(campaign_state)
                    db.session.commit()

                    cls._create_snapshot(narrator_turn, campaign)
                    try:
                        ZorkMemory.store_turn_embedding(
                            narrator_turn.id,
                            campaign.id,
                            active_player.user_id,
                            "narrator",
                            narration,
                            metadata=cls._turn_embedding_metadata(
                                visibility=turn_visibility,
                                actor_player_slug=player_state.get("character_name"),
                                location_key=cls._room_key_from_player_state(player_state),
                                channel_id=channel_id,
                            ),
                        )
                    except Exception:
                        logger.debug(
                            "Zork memory embedding skipped for timed turn %s",
                            narrator_turn.id,
                            exc_info=True,
                        )

                    timed_narrator_turn_id = narrator_turn.id
        finally:
            cls._pop_contextual_turn_log_scope(log_scope_token)

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
        if calendar_event_notifications:
            asyncio.ensure_future(
                cls._send_calendar_event_notifications(
                    campaign_id=campaign_id,
                    campaign_name=campaign_name_for_notifications,
                    notifications=calendar_event_notifications,
                    preferred_channel_id=channel_id,
                )
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
            location = player_state.get("location")
            exits = player_state.get("exits")
            player_loc = cls._map_location_components(location, room_title, room_summary)

            if not player_loc["display"]:
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
                other_loc = cls._map_location_components(
                    other_state.get("location"),
                    other_state.get("room_title"),
                    other_state.get("room_summary"),
                )
                if not other_loc["display"]:
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
                        "room": other_loc["display"],
                        "location_key": other_loc["key"],
                        "location_display": other_loc["display"],
                        "location_hint": other_loc["hint"],
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
                    char_loc = cls._map_location_components(
                        info.get("location"),
                        "",
                        "",
                    )
                    char_entries.append(
                        {
                            "name": str(char_name),
                            "location_key": char_loc["key"] or "unknown-location",
                            "location_display": char_loc["display"] or "Unknown",
                        }
                    )
            chars_text = cls._dump_json(char_entries) if char_entries else "[]"

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
                f"PLAYER_LOCATION_KEY: {player_loc['key']}\n"
                f"PLAYER_LOCATION_DISPLAY: {player_loc['display']}\n"
                f"PLAYER_ROOM_TITLE: {room_title or 'Unknown'}\n"
                f"PLAYER_ROOM_SUMMARY: {room_summary or ''}\n"
                f"PLAYER_EXITS: {exits or []}\n"
                f"WORLD_SUMMARY: {cls._compose_world_summary(campaign, campaign_state, max_chars=1200)}\n"
                f"WORLD_STATE: {cls._dump_json(model_state)}\n"
                f"LANDMARKS: {landmarks_text}\n"
                f"WORLD_CHARACTER_LOCATIONS: {chars_text}\n"
            )
            if story_progress:
                map_prompt += f"STORY_PROGRESS: {story_progress}\n"
            map_prompt += (
                f"OTHER_PLAYERS: {cls._dump_json(other_entries)}\n"
                "MAP_SPATIAL_RULES:\n"
                "- location_key is authoritative for grouping entities.\n"
                "- Same location_key means same room/area.\n"
                "- Different location_key means separate rooms/areas; never nest them.\n"
                "Draw a compact map with @ marking the player's location.\n"
            )
            gpt = cls._new_gpt(
                campaign=campaign,
                channel_id=getattr(ctx.channel, "id", None),
            )
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
