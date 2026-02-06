import asyncio
import datetime
import json
import logging
import re
import threading
from typing import Dict, List, Optional, Tuple

import discord
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.classes.openai.text import GPT
from discord_tron_master.bot import DiscordBot
from discord_tron_master.models.base import db
from discord_tron_master.models.zork import ZorkCampaign, ZorkChannel, ZorkPlayer, ZorkTurn

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
    XP_BASE = 100
    XP_PER_LEVEL = 50
    MAX_INVENTORY_CHANGES_PER_TURN = 2
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
        "may be in a different location or timeline than other players. You never break character.\n\n"
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
        "- When a player must pick a path, accept only exact responses: 'main party' or 'new path'.\n"
        "- If the player has no room_summary or party_status, ask whether they are joining the main party or starting a new path, and set party_status accordingly.\n"
        "- Do not print an Inventory section; the emulator appends authoritative inventory.\n"
        "- Do not repeat full room descriptions or inventory unless asked or the room changes.\n"
        "- scene_image_prompt should describe the visible scene, not inventory lists.\n"
    )
    MAP_SYSTEM_PROMPT = (
        "You draw compact ASCII maps for text adventures.\n"
        "Return ONLY the ASCII map (no markdown, no code fences).\n"
        "Keep it under 25 lines and 60 columns. Use @ for the player location.\n"
        "Use simple ASCII only: - | + . # / \\ and letters.\n"
        "Include other player markers (A, B, C, ...) and add a Legend at the bottom.\n"
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
                return None, f"Adventure mode is disabled in this channel. Run `{command_prefix}zork` to enable it."
            if channel.active_campaign_id is None:
                _, campaign = cls.enable_channel(ctx.guild.id, ctx.channel.id, ctx.author.id)
            else:
                campaign = ZorkCampaign.query.get(channel.active_campaign_id)
                if campaign is None:
                    _, campaign = cls.enable_channel(ctx.guild.id, ctx.channel.id, ctx.author.id)
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
    def _build_player_state_for_prompt(cls, player_state: Dict[str, object]) -> Dict[str, object]:
        if not isinstance(player_state, dict):
            return {}
        model_state = {}
        for key, value in player_state.items():
            if key in cls.PLAYER_STATE_EXCLUDE_KEYS:
                continue
            model_state[key] = value
        return model_state

    @classmethod
    def _format_inventory(cls, player_state: Dict[str, object]) -> Optional[str]:
        if not isinstance(player_state, dict):
            return None
        inventory = player_state.get("inventory")
        if not inventory:
            return None
        if isinstance(inventory, list):
            inv_text = ", ".join([str(item) for item in inventory])
        else:
            inv_text = str(inventory)
        return f"Inventory: {inv_text}"

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
    def _apply_inventory_delta(
        cls,
        current: List[str],
        adds: List[str],
        removes: List[str],
    ) -> List[str]:
        out = []
        remove_norm = {item.lower() for item in removes}
        for item in current:
            if item.lower() in remove_norm:
                continue
            out.append(item)
        out_norm = {item.lower() for item in out}
        for item in adds:
            if item.lower() in out_norm:
                continue
            out.append(item)
            out_norm.add(item.lower())
        return out

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
        previous_inventory = cls._normalize_inventory_items(previous_state.get("inventory"))
        action_l = (action_text or "").lower()
        narration_l = (narration_text or "").lower()

        inventory_add = cls._normalize_inventory_items(cleaned.pop("inventory_add", []))
        inventory_remove = cls._normalize_inventory_items(cleaned.pop("inventory_remove", []))

        if "inventory" in cleaned:
            cleaned.pop("inventory", None)
            logger.warning("Ignored full inventory list in player_state_update; only delta fields are accepted.")

        # Only accept inventory deltas when item names are referenced in action/narration.
        filtered_add = []
        for item in inventory_add:
            item_l = item.lower()
            if item_l in action_l or item_l in narration_l:
                filtered_add.append(item)
        inventory_add = filtered_add

        filtered_remove = []
        for item in inventory_remove:
            item_l = item.lower()
            if item_l in action_l or item_l in narration_l:
                filtered_remove.append(item)
        inventory_remove = filtered_remove

        if len(inventory_add) > cls.MAX_INVENTORY_CHANGES_PER_TURN or len(inventory_remove) > cls.MAX_INVENTORY_CHANGES_PER_TURN:
            logger.warning(
                "Rejected suspicious inventory delta for user update: adds=%s removes=%s",
                inventory_add,
                inventory_remove,
            )
            inventory_add = []
            inventory_remove = []

        if inventory_add or inventory_remove:
            cleaned["inventory"] = cls._apply_inventory_delta(
                previous_inventory, inventory_add, inventory_remove
            )
        else:
            cleaned["inventory"] = previous_inventory

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
        channel = ZorkChannel.query.filter_by(guild_id=guild_id, channel_id=channel_id).first()
        if channel is None:
            channel = ZorkChannel(guild_id=guild_id, channel_id=channel_id, enabled=False)
            db.session.add(channel)
            db.session.commit()
        return channel

    @classmethod
    def is_channel_enabled(cls, guild_id: int, channel_id: int) -> bool:
        channel = ZorkChannel.query.filter_by(guild_id=guild_id, channel_id=channel_id).first()
        if channel is None:
            return False
        return bool(channel.enabled)

    @classmethod
    def get_or_create_campaign(cls, guild_id: int, name: str, created_by: int) -> ZorkCampaign:
        normalized = cls._normalize_campaign_name(name)
        campaign = ZorkCampaign.query.filter_by(guild_id=guild_id, name=normalized).first()
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
    def enable_channel(cls, guild_id: int, channel_id: int, user_id: int) -> Tuple[ZorkChannel, ZorkCampaign]:
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
        return ZorkCampaign.query.filter_by(guild_id=guild_id).order_by(ZorkCampaign.name.asc()).all()

    @classmethod
    def can_switch_campaign(cls, campaign_id: int, user_id: int, window_seconds: int = 3600) -> Tuple[bool, int]:
        cutoff = cls._now() - datetime.timedelta(seconds=window_seconds)
        active_count = (
            ZorkPlayer.query.filter(
                ZorkPlayer.campaign_id == campaign_id,
                ZorkPlayer.user_id != user_id,
                ZorkPlayer.last_active >= cutoff,
            )
            .count()
        )
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
            can_switch, active_count = cls.can_switch_campaign(channel.active_campaign_id, user_id)
            if not can_switch:
                return None, False, f"{active_count} other player(s) active in last hour"
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
        player = ZorkPlayer.query.filter_by(campaign_id=campaign_id, user_id=user_id).first()
        if player is None:
            player_state = {}
            if campaign is not None:
                campaign_state = cls.get_campaign_state(campaign)
                start_room = campaign_state.get("start_room")
                if isinstance(start_room, dict):
                    player_state.update(start_room)
                default_persona = campaign_state.get("default_persona")
                if default_persona:
                    player_state["persona"] = str(default_persona).strip()
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
    def _copy_identity_fields(cls, source_state: Dict[str, object], target_state: Dict[str, object]) -> Dict[str, object]:
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
    async def _enqueue_scene_image(cls, ctx, scene_image_prompt: str):
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
        cfg = AppConfig()
        user_config = cfg.get_user_config(user_id=ctx.author.id)
        user_config["auto_model"] = False
        user_config["model"] = "Tongyi-MAI/Z-Image-Turbo"
        user_config["steps"] = 8
        user_config["guidance_scaling"] = 1.0
        user_config["guidance_scale"] = 1.0
        try:
            await generator.generate_from_user_config(
                ctx=ctx,
                user_config=user_config,
                user_id=ctx.author.id,
                prompt=scene_image_prompt,
                job_metadata={
                    "zork_scene": True,
                    "suppress_image_reactions": True,
                    "suppress_image_details": True,
                },
            )
        except Exception as e:
            logger.warning(f"Failed to enqueue scene image prompt: {e}")

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
    ) -> Tuple[str, str]:
        summary = cls._strip_inventory_mentions(campaign.summary or "")
        summary = cls._trim_text(summary, cls.MAX_SUMMARY_CHARS)
        state = cls.get_campaign_state(campaign)
        state = cls._scrub_inventory_from_state(state)
        model_state = cls._build_model_state(state)
        state_text = cls._dump_json(model_state)
        state_text = cls._trim_text(state_text, cls.MAX_STATE_CHARS)
        attributes = cls.get_player_attributes(player)
        player_state = cls.get_player_state(player)
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

        user_prompt = (
            f"CAMPAIGN: {campaign.name}\n"
            f"PLAYER_ID: {player.user_id}\n"
            f"WORLD_SUMMARY: {summary}\n"
            f"WORLD_STATE: {state_text}\n"
            f"PLAYER_CARD: {cls._dump_json(player_card)}\n"
            f"RECENT_TURNS:\n{recent_text}\n"
            f"PLAYER_ACTION: {action}\n"
        )
        return cls.SYSTEM_PROMPT, user_prompt

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
        return text[start:end + 1]

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
    def _assign_player_markers(cls, players: List["ZorkPlayer"], exclude_user_id: int) -> List[dict]:
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
    def _apply_state_update(cls, state: Dict[str, object], update: Dict[str, object]) -> Dict[str, object]:
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
            campaign_id, error_text = await cls.begin_turn(ctx, command_prefix=command_prefix)
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
                    player = cls.get_or_create_player(campaign_id, ctx.author.id, campaign=campaign)
                    player.last_active = db.func.now()
                    player.updated = db.func.now()
                    db.session.commit()

                    player_state = cls.get_player_state(player)
                    action_clean = action.strip().lower()
                    is_thread_channel = isinstance(ctx.channel, discord.Thread)

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
                                options = cls._build_campaign_suggestion_text(ctx.guild.id)
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

                            thread_channel, _ = cls.enable_channel(ctx.guild.id, thread.id, ctx.author.id)
                            thread_campaign, _, _ = cls.set_active_campaign(
                                thread_channel,
                                ctx.guild.id,
                                campaign_name,
                                ctx.author.id,
                                enforce_activity_window=False,
                            )
                            thread_player = cls.get_or_create_player(
                                thread_campaign.id, ctx.author.id, campaign=thread_campaign
                            )
                            thread_state = cls.get_player_state(thread_player)
                            thread_state = cls._copy_identity_fields(player_state, thread_state)
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

                    if action_clean in ("look", "l") and player_state.get("room_description"):
                        title = player_state.get("room_title") or "Unknown"
                        desc = player_state.get("room_description") or ""
                        exits = player_state.get("exits")
                        exits_text = f"\nExits: {', '.join(exits)}" if exits else ""
                        narration = f"{title}\n{desc}{exits_text}"
                        inventory_line = cls._format_inventory(player_state)
                        if inventory_line:
                            narration = f"{narration}\n\n{inventory_line}"
                        narration = cls._trim_text(narration, cls.MAX_NARRATION_CHARS)
                        db.session.add(ZorkTurn(campaign_id=campaign.id, user_id=ctx.author.id, kind="player", content=action))
                        db.session.add(ZorkTurn(campaign_id=campaign.id, user_id=ctx.author.id, kind="narrator", content=narration))
                        campaign.last_narration = narration
                        campaign.updated = db.func.now()
                        db.session.commit()
                        return narration
                    if action_clean in ("inventory", "inv", "i"):
                        narration = cls._format_inventory(player_state) or "Inventory: empty"
                        narration = cls._trim_text(narration, cls.MAX_NARRATION_CHARS)
                        db.session.add(ZorkTurn(campaign_id=campaign.id, user_id=ctx.author.id, kind="player", content=action))
                        db.session.add(ZorkTurn(campaign_id=campaign.id, user_id=ctx.author.id, kind="narrator", content=narration))
                        campaign.last_narration = narration
                        campaign.updated = db.func.now()
                        db.session.commit()
                        return narration

                    turns = cls.get_recent_turns(campaign.id, user_id=ctx.author.id)
                    system_prompt, user_prompt = cls.build_prompt(campaign, player, action, turns)
                    gpt = GPT()
                    response = await gpt.turbo_completion(system_prompt, user_prompt, temperature=0.8, max_tokens=900)
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
                            player_state_update = payload.get("player_state_update", {}) or {}
                            scene_image_prompt = payload.get("scene_image_prompt")
                        except Exception as e:
                            logger.warning(f"Failed to parse Zork JSON response: {e}")

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

                    if summary_update:
                        summary_update = summary_update.strip()
                        summary_update = cls._strip_inventory_mentions(summary_update)
                        if campaign.summary:
                            campaign.summary = f"{campaign.summary}\n{summary_update}"
                        else:
                            campaign.summary = summary_update
                        campaign.summary = cls._trim_text(campaign.summary, cls.MAX_SUMMARY_CHARS)

                    player_state = cls.get_player_state(player)
                    player_state_update = cls._sanitize_player_state_update(
                        player_state,
                        player_state_update,
                        action_text=action,
                        narration_text=narration,
                    )
                    player_state = cls._apply_state_update(player_state, player_state_update)
                    player.state_json = cls._dump_json(player_state)

                    if isinstance(xp_awarded, int) and xp_awarded > 0:
                        player.xp += xp_awarded

                    inventory_line = cls._format_inventory(player_state) or "Inventory: empty"
                    if narration:
                        narration = f"{narration}\n\n{inventory_line}"
                    else:
                        narration = inventory_line

                    campaign.last_narration = narration
                    campaign.updated = db.func.now()
                    player.updated = db.func.now()

                    db.session.add(ZorkTurn(campaign_id=campaign.id, user_id=ctx.author.id, kind="player", content=action))
                    db.session.add(ZorkTurn(campaign_id=campaign.id, user_id=ctx.author.id, kind="narrator", content=narration))
                    db.session.commit()

                    if isinstance(scene_image_prompt, str):
                        cleaned_scene_prompt = scene_image_prompt.strip()
                        if cleaned_scene_prompt:
                            await cls._enqueue_scene_image(ctx, cleaned_scene_prompt)

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
                _, campaign = cls.enable_channel(ctx.guild.id, ctx.channel.id, ctx.author.id)
            else:
                campaign = ZorkCampaign.query.get(channel.active_campaign_id)
                if campaign is None:
                    _, campaign = cls.enable_channel(ctx.guild.id, ctx.channel.id, ctx.author.id)
            campaign_id = campaign.id

        with app.app_context():
            campaign = ZorkCampaign.query.get(campaign_id)
            player = cls.get_or_create_player(campaign_id, ctx.author.id, campaign=campaign)
            player_state = cls.get_player_state(player)
            room_summary = player_state.get("room_summary")
            room_title = player_state.get("room_title")
            exits = player_state.get("exits")

            if not room_summary and not room_title:
                return "No map data yet. Try `look` first."

            other_players = ZorkPlayer.query.filter_by(campaign_id=campaign.id).order_by(ZorkPlayer.user_id.asc()).all()
            marker_data = cls._assign_player_markers(other_players, ctx.author.id)
            other_entries = []
            for entry in marker_data:
                other = entry["player"]
                other_state = cls.get_player_state(other)
                other_room = other_state.get("room_summary") or other_state.get("room_title") or other_state.get("location")
                if not other_room:
                    continue
                other_entries.append(
                    {
                        "marker": entry["marker"],
                        "user_id": other.user_id,
                        "room": other_room,
                        "party_status": other_state.get("party_status"),
                    }
                )

            map_prompt = (
                f"CAMPAIGN: {campaign.name}\n"
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
