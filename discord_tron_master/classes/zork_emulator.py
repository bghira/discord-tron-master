import asyncio
import datetime
import json
import logging
import re
import threading
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


class ZorkEmulator:
    BASE_POINTS = 10
    POINTS_PER_LEVEL = 5
    MAX_ATTRIBUTE_VALUE = 20
    MAX_SUMMARY_CHARS = 4000
    MAX_STATE_CHARS = 8000
    MAX_RECENT_TURNS = 12
    MAX_TURN_CHARS = 1200
    MAX_NARRATION_CHARS = 3500
    MAX_PARTY_CONTEXT_PLAYERS = 6
    MAX_SCENE_PROMPT_CHARS = 900
    MAX_PERSONA_PROMPT_CHARS = 140
    MAX_SCENE_REFERENCE_IMAGES = 10
    XP_BASE = 100
    XP_PER_LEVEL = 50
    MAX_INVENTORY_CHANGES_PER_TURN = 2
    ROOM_IMAGE_STATE_KEY = "room_scene_images"
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
    PLAYER_STATE_EXCLUDE_KEYS = {"inventory", "room_description"}

    _locks: Dict[int, asyncio.Lock] = {}
    _inflight_turns = set()
    _inflight_turns_lock = threading.Lock()
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
        "- state_update: object (world state patches)\n"
        "- summary_update: string (one or two sentences of lasting changes)\n"
        "- xp_awarded: integer (0-10)\n"
        "- player_state_update: object (optional, player state patches)\n\n"
        "- scene_image_prompt: string (optional; include only when scene/location changes and a fresh image should be rendered)\n\n"
        "Rules:\n"
        "- No markdown or code fences.\n"
        "- Keep narration under 1800 characters.\n"
        "- If WORLD_SUMMARY is empty, invent a strong starting room and seed the world.\n"
        "- Use player_state_update for player-specific location and status.\n"
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
        "\nYou have a memory_search tool. If the current context is insufficient to "
        "resolve the player's action or reference, return ONLY:\n"
        '{"tool_call": "memory_search", "query": "..."}\n'
        "No other keys alongside tool_call. Only use when genuinely needed.\n"
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
    def _build_model_state(cls, campaign_state: Dict[str, object]) -> Dict[str, object]:
        if not isinstance(campaign_state, dict):
            return {}
        model_state = {}
        for key, value in campaign_state.items():
            if key in cls.MODEL_STATE_EXCLUDE_KEYS:
                continue
            model_state[key] = value
        return model_state

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
            cleaned.pop("inventory", None)
            logger.warning(
                "Ignored full inventory list in player_state_update; only delta fields are accepted."
            )

        # Only accept inventory deltas when item names are referenced in action/narration.
        filtered_add = []
        for item in inventory_add:
            item_l = item.lower()
            if item_l in action_l or item_l in narration_l:
                filtered_add.append(item)
            else:
                logger.warning(
                    "Inventory add rejected: '%s' not found in action or narration.",
                    item,
                )
        inventory_add = filtered_add

        filtered_remove = []
        for item in inventory_remove:
            item_l = item.lower()
            if item_l in action_l or item_l in narration_l:
                filtered_remove.append(item)
            else:
                logger.warning(
                    "Inventory remove rejected: '%s' not found in action or narration.",
                    item,
                )
        inventory_remove = filtered_remove

        if (
            len(inventory_add) > cls.MAX_INVENTORY_CHANGES_PER_TURN
            or len(inventory_remove) > cls.MAX_INVENTORY_CHANGES_PER_TURN
        ):
            logger.warning(
                "Rejected suspicious inventory delta for user update: adds=%s removes=%s",
                inventory_add,
                inventory_remove,
            )
            inventory_add = []
            inventory_remove = []

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
        state_text = cls._dump_json(model_state)
        state_text = cls._trim_text(state_text, cls.MAX_STATE_CHARS)
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
        for turn in turns:
            if turn.kind != "player":
                continue
            clipped = cls._trim_text(turn.content, cls.MAX_TURN_CHARS)
            clipped = cls._strip_inventory_mentions(clipped)
            recent_lines.append(f"PLAYER: {clipped}")
        recent_text = "\n".join(recent_lines) if recent_lines else "None"
        rails_context = cls._build_rails_context(player_state, party_snapshot)

        user_prompt = (
            f"CAMPAIGN: {campaign.name}\n"
            f"PLAYER_ID: {player.user_id}\n"
            f"IS_NEW_PLAYER: {str(is_new_player).lower()}\n"
            f"GUARDRAILS_ENABLED: {str(guardrails_enabled).lower()}\n"
            f"RAILS_CONTEXT: {cls._dump_json(rails_context)}\n"
            f"WORLD_SUMMARY: {summary}\n"
            f"WORLD_STATE: {state_text}\n"
            f"PLAYER_CARD: {cls._dump_json(player_card)}\n"
            f"PARTY_SNAPSHOT: {cls._dump_json(party_snapshot)}\n"
            f"RECENT_TURNS:\n{recent_text}\n"
            f"PLAYER_ACTION: {action}\n"
        )
        system_prompt = cls.SYSTEM_PROMPT
        if guardrails_enabled:
            system_prompt = f"{system_prompt}{cls.GUARDRAILS_SYSTEM_PROMPT}"
        system_prompt = f"{system_prompt}{cls.MEMORY_TOOL_PROMPT}"
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
            cleaned = []
            for line in text.splitlines():
                if "```" in line:
                    continue
                cleaned.append(line)
            text = "\n".join(cleaned).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        return text[start : end + 1]

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
                    player.last_active = db.func.now()
                    player.updated = db.func.now()
                    db.session.commit()

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

                    if action_clean in ("look", "l") and player_state.get(
                        "room_description"
                    ):
                        title = player_state.get("room_title") or "Unknown"
                        desc = player_state.get("room_description") or ""
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

                    turns = cls.get_recent_turns(campaign.id, user_id=ctx.author.id)
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
                    gpt = GPT()
                    response = await gpt.turbo_completion(
                        system_prompt, user_prompt, temperature=0.8, max_tokens=900
                    )
                    if not response:
                        response = "A hollow silence answers. Try again."

                    # --- Tool-call detection (memory_search) ---
                    json_text_tc = cls._extract_json(response)
                    if json_text_tc:
                        try:
                            first_payload = json.loads(json_text_tc)
                        except Exception:
                            first_payload = None
                        if first_payload and cls._is_tool_call(first_payload):
                            query = str(first_payload.get("query") or "").strip()
                            if query:
                                logger.info(
                                    "Zork memory search requested: campaign=%s query=%r",
                                    campaign.id,
                                    query,
                                )
                                results = ZorkMemory.search(query, campaign.id, top_k=5)
                                if results:
                                    recall_lines = []
                                    for turn_id, kind, content, score in results:
                                        recall_lines.append(
                                            f"- [{kind} turn {turn_id}, relevance {score:.2f}]: {content[:300]}"
                                        )
                                    recall_block = (
                                        "MEMORY_RECALL (results from memory_search):\n"
                                        + "\n".join(recall_lines)
                                    )
                                else:
                                    recall_block = "MEMORY_RECALL: No relevant memories found."
                                augmented_prompt = f"{user_prompt}\n{recall_block}\n"
                                response = await gpt.turbo_completion(
                                    system_prompt,
                                    augmented_prompt,
                                    temperature=0.8,
                                    max_tokens=900,
                                )
                                if not response:
                                    response = "A hollow silence answers. Try again."

                    narration = response.strip()
                    state_update = {}
                    summary_update = None
                    xp_awarded = 0
                    player_state_update = {}
                    scene_image_prompt = None

                    json_text = cls._extract_json(response)
                    if json_text:
                        try:
                            payload = json.loads(json_text)
                            narration = payload.get("narration", narration).strip()
                            state_update = payload.get("state_update", {}) or {}
                            summary_update = payload.get("summary_update")
                            xp_awarded = payload.get("xp_awarded", 0) or 0
                            player_state_update = (
                                payload.get("player_state_update", {}) or {}
                            )
                            scene_image_prompt = payload.get("scene_image_prompt")
                        except Exception as e:
                            logger.warning(f"Failed to parse Zork JSON response: {e}")

                    raw_narration = narration
                    narration = cls._trim_text(narration, cls.MAX_NARRATION_CHARS)
                    narration = cls._strip_inventory_from_narration(narration)

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

                    if summary_update:
                        summary_update = summary_update.strip()
                        summary_update = cls._strip_inventory_mentions(summary_update)
                        if campaign.summary:
                            campaign.summary = f"{campaign.summary}\n{summary_update}"
                        else:
                            campaign.summary = summary_update
                        campaign.summary = cls._trim_text(
                            campaign.summary, cls.MAX_SUMMARY_CHARS
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

                    campaign.last_narration = narration
                    campaign.updated = db.func.now()
                    player.updated = db.func.now()

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
