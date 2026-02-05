import asyncio
import datetime
import json
import logging
import re
from typing import Dict, List, Optional, Tuple

from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.classes.openai.text import GPT
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
    MODEL_STATE_EXCLUDE_KEYS = {"room_description", "last_narration"}
    PLAYER_STATE_EXCLUDE_KEYS = {"inventory"}

    _locks: Dict[int, asyncio.Lock] = {}

    SYSTEM_PROMPT = (
        "You are the ZorkEmulator, a classic text-adventure GM with light RPG rules. "
        "You describe outcomes in second person, terse but vivid. You track rooms, "
        "objects, exits, and consequences. You never break character.\n\n"
        "Return ONLY valid JSON with these keys:\n"
        "- narration: string (what the player sees)\n"
        "- state_update: object (world state patches, include room_title and room_description when location changes)\n"
        "- summary_update: string (one or two sentences of lasting changes)\n"
        "- xp_awarded: integer (0-10)\n"
        "- player_state_update: object (optional, player state patches)\n\n"
        "Rules:\n"
        "- No markdown or code fences.\n"
        "- Keep narration under 1800 characters.\n"
        "- If WORLD_SUMMARY is empty, invent a strong starting room and seed the world.\n"
        "- Use state_update.room_description for a full room description only when location changes.\n"
        "- Use state_update.room_summary for a short one-line room summary for future context.\n"
        "- Use state_update.exits as a short list of exits if applicable.\n"
        "- Use player_state_update for inventory, hp, or conditions.\n"
        "- Do not repeat full room descriptions or inventory unless asked or the room changes.\n"
    )

    @classmethod
    def _get_lock(cls, campaign_id: int) -> asyncio.Lock:
        lock = cls._locks.get(campaign_id)
        if lock is None:
            lock = asyncio.Lock()
            cls._locks[campaign_id] = lock
        return lock

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
    ) -> Tuple[ZorkCampaign, bool, Optional[str]]:
        normalized = cls._normalize_campaign_name(name)
        if channel.active_campaign_id is not None:
            can_switch, active_count = cls.can_switch_campaign(channel.active_campaign_id, user_id)
            if not can_switch:
                return None, False, f"{active_count} other player(s) active in last hour"
        campaign = cls.get_or_create_campaign(guild_id, normalized, user_id)
        channel.active_campaign_id = campaign.id
        channel.updated = db.func.now()
        db.session.commit()
        return campaign, True, None

    @classmethod
    def get_or_create_player(cls, campaign_id: int, user_id: int) -> ZorkPlayer:
        player = ZorkPlayer.query.filter_by(campaign_id=campaign_id, user_id=user_id).first()
        if player is None:
            player = ZorkPlayer(
                campaign_id=campaign_id,
                user_id=user_id,
                level=1,
                xp=0,
                attributes_json="{}",
                state_json="{}",
            )
            db.session.add(player)
            db.session.commit()
        return player

    @classmethod
    def get_recent_turns(cls, campaign_id: int, limit: int = None) -> List[ZorkTurn]:
        if limit is None:
            limit = cls.MAX_RECENT_TURNS
        turns = (
            ZorkTurn.query.filter_by(campaign_id=campaign_id)
            .order_by(ZorkTurn.id.desc())
            .limit(limit)
            .all()
        )
        return list(reversed(turns))

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
        summary = cls._trim_text(campaign.summary or "", cls.MAX_SUMMARY_CHARS)
        state = cls.get_campaign_state(campaign)
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
            recent_lines.append(f"PLAYER: {clipped}")
        recent_text = "\n".join(recent_lines) if recent_lines else "None"

        user_prompt = (
            f"CAMPAIGN: {campaign.name}\n"
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
    async def play_action(cls, ctx, action: str, command_prefix: str = "!") -> str:
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
            lock = cls._get_lock(campaign_id)

        async with lock:
            with app.app_context():
                campaign = ZorkCampaign.query.get(campaign_id)
                player = cls.get_or_create_player(campaign_id, ctx.author.id)
                player.last_active = db.func.now()
                player.updated = db.func.now()
                db.session.commit()

                state = cls.get_campaign_state(campaign)
                player_state = cls.get_player_state(player)
                action_clean = action.strip().lower()
                if action_clean in ("look", "l") and state.get("room_description"):
                    title = state.get("room_title", "Unknown")
                    desc = state.get("room_description", "")
                    exits = state.get("exits")
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

                turns = cls.get_recent_turns(campaign.id)
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

                json_text = cls._extract_json(response)
                if json_text:
                    try:
                        payload = json.loads(json_text)
                        narration = payload.get("narration", narration).strip()
                        state_update = payload.get("state_update", {}) or {}
                        summary_update = payload.get("summary_update")
                        xp_awarded = payload.get("xp_awarded", 0) or 0
                        player_state_update = payload.get("player_state_update", {}) or {}
                    except Exception as e:
                        logger.warning(f"Failed to parse Zork JSON response: {e}")

                narration = cls._trim_text(narration, cls.MAX_NARRATION_CHARS)

                campaign_state = cls.get_campaign_state(campaign)
                campaign_state = cls._apply_state_update(campaign_state, state_update)
                campaign.state_json = cls._dump_json(campaign_state)

                if summary_update:
                    summary_update = summary_update.strip()
                    if campaign.summary:
                        campaign.summary = f"{campaign.summary}\n{summary_update}"
                    else:
                        campaign.summary = summary_update
                    campaign.summary = cls._trim_text(campaign.summary, cls.MAX_SUMMARY_CHARS)

                player_state = cls.get_player_state(player)
                player_state = cls._apply_state_update(player_state, player_state_update)
                player.state_json = cls._dump_json(player_state)

                if isinstance(xp_awarded, int) and xp_awarded > 0:
                    player.xp += xp_awarded

                inventory_line = cls._format_inventory(player_state)
                if inventory_line and "Inventory:" not in narration:
                    narration = f"{narration}\n\n{inventory_line}"

                campaign.last_narration = narration
                campaign.updated = db.func.now()
                player.updated = db.func.now()

                db.session.add(ZorkTurn(campaign_id=campaign.id, user_id=ctx.author.id, kind="player", content=action))
                db.session.add(ZorkTurn(campaign_id=campaign.id, user_id=ctx.author.id, kind="narrator", content=narration))
                db.session.commit()

                return narration
