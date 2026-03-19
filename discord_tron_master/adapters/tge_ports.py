"""Adapter implementations that bridge discord-tron-master infrastructure
to text-game-engine's Protocol ports.

Each adapter wraps an existing DTM subsystem (DiscordBot, GPT, ZorkMemory,
QueueManager, IMDB scraper) behind the Protocol interface that TGE expects.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TextCompletionAdapter
# ---------------------------------------------------------------------------
class TextCompletionAdapter:
    """Wraps DTM's ``GPT.turbo_completion`` behind ``TextCompletionPort``."""

    def __init__(self, *, gpt_factory=None):
        """
        Parameters
        ----------
        gpt_factory : callable, optional
            A zero-argument callable that returns a configured ``GPT`` instance.
            If ``None``, a default ``GPT()`` is created on each call.
        """
        self._gpt_factory = gpt_factory

    def _make_gpt(self):
        if self._gpt_factory is not None:
            return self._gpt_factory()
        from discord_tron_master.classes.openai.text import GPT

        return GPT()

    async def complete(
        self,
        system_prompt: str,
        prompt: str,
        *,
        temperature: float = 0.8,
        max_tokens: int = 2048,
    ) -> str | None:
        gpt = self._make_gpt()
        return await gpt.turbo_completion(
            system_prompt,
            prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )


# ---------------------------------------------------------------------------
# TimerEffectsAdapter
# ---------------------------------------------------------------------------
class TimerEffectsAdapter:
    """Wraps DiscordBot for timer-line edits and timed-event emission."""

    async def edit_timer_line(
        self,
        channel_id: str,
        message_id: str,
        replacement: str,
    ) -> None:
        from discord_tron_master.bot import DiscordBot

        bot_instance = DiscordBot.get_instance()
        if bot_instance is None:
            return
        try:
            channel = await bot_instance.find_channel(int(channel_id))
        except (TypeError, ValueError):
            return
        if channel is None:
            return
        try:
            message = await channel.fetch_message(int(message_id))
        except Exception:
            return
        if message is None:
            return
        content = message.content or ""
        lines = content.split("\n")
        new_lines = [
            replacement if line.startswith("\u23f0") else line for line in lines
        ]
        new_content = "\n".join(new_lines)
        if len(new_content) > 2000:
            new_content = new_content[:1997] + "..."
        if new_content != content:
            try:
                await message.edit(content=new_content)
            except Exception as exc:
                logger.debug("Timer line edit failed: %s", exc)

    async def emit_timed_event(
        self,
        campaign_id: str,
        channel_id: str,
        actor_id: str | None,
        narration: str,
    ) -> None:
        from discord_tron_master.bot import DiscordBot

        bot_instance = DiscordBot.get_instance()
        if bot_instance is None:
            return
        try:
            channel = await bot_instance.find_channel(int(channel_id))
        except (TypeError, ValueError):
            return
        if channel is None:
            return
        text = str(narration or "").strip()
        if not text:
            return
        try:
            await DiscordBot.send_large_message(channel, text)
        except Exception as exc:
            logger.debug("Timed event send failed: %s", exc)


# ---------------------------------------------------------------------------
# NotificationAdapter
# ---------------------------------------------------------------------------
class NotificationAdapter:
    """Wraps DiscordBot DM delivery behind ``NotificationPort``."""

    async def send_dm(
        self,
        actor_id: str,
        message: str,
    ) -> None:
        from discord_tron_master.bot import DiscordBot

        bot_instance = DiscordBot.get_instance()
        if bot_instance is None or bot_instance.bot is None:
            return
        text = str(message or "").strip()
        if not text:
            return
        try:
            discord_user_id = int(actor_id)
        except (TypeError, ValueError):
            logger.debug("Cannot send DM: actor_id %r is not a valid Discord ID", actor_id)
            return
        user = bot_instance.bot.get_user(discord_user_id)
        if user is None:
            try:
                user = await bot_instance.bot.fetch_user(discord_user_id)
            except Exception:
                logger.debug("Cannot fetch Discord user %s", actor_id)
                return
        try:
            await DiscordBot.send_large_message(user, text)
        except Exception as exc:
            logger.debug("DM send to %s failed: %s", actor_id, exc)


# ---------------------------------------------------------------------------
# MediaGenerationAdapter
# ---------------------------------------------------------------------------
class MediaGenerationAdapter:
    """Wraps DTM's Generate cog / QueueManager behind ``MediaGenerationPort``."""

    def gpu_worker_available(self) -> bool:
        try:
            from discord_tron_master.classes.worker_manager import WorkerManager

            workers = WorkerManager.get_workers()
            return any(
                getattr(w, "gpu", False) or getattr(w, "has_gpu", False)
                for w in (workers or [])
            )
        except Exception:
            return False

    def _get_generator(self):
        from discord_tron_master.bot import DiscordBot

        bot_instance = DiscordBot.get_instance()
        if bot_instance is None or bot_instance.bot is None:
            return None
        return bot_instance.bot.get_cog("Generate")

    async def enqueue_scene_generation(
        self,
        *,
        actor_id: str,
        prompt: str,
        model: str,
        reference_images: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        channel_id: str | None = None,
    ) -> bool:
        generator = self._get_generator()
        if generator is None:
            return False
        channel = None
        if channel_id is not None:
            try:
                from discord_tron_master.bot import DiscordBot

                bot_instance = DiscordBot.get_instance()
                if bot_instance is not None:
                    channel = await bot_instance.find_channel(int(channel_id))
            except Exception:
                channel = None
        try:
            from discord_tron_master.classes.app_config import AppConfig

            cfg = AppConfig()
            user_config = cfg.get_user_config(user_id=int(actor_id))
        except Exception:
            user_config = {}
        user_config["auto_model"] = False
        user_config["model"] = model or "flux"
        user_config["steps"] = 12
        user_config["guidance_scaling"] = 2.5
        job_metadata = dict(metadata or {})
        job_metadata.setdefault("zork_scene", True)
        job_metadata.setdefault("suppress_image_reactions", True)
        job_metadata.setdefault("suppress_image_details", True)
        try:
            await generator.generate_from_user_config(
                ctx=channel,
                user_config=user_config,
                user_id=int(actor_id),
                prompt=prompt,
                job_metadata=job_metadata,
                image_data=reference_images if reference_images else None,
            )
            return True
        except Exception as exc:
            logger.debug("Scene generation enqueue failed: %s", exc)
            return False

    async def enqueue_avatar_generation(
        self,
        *,
        actor_id: str,
        prompt: str,
        model: str,
        metadata: dict[str, Any] | None = None,
        channel_id: str | None = None,
    ) -> bool:
        generator = self._get_generator()
        if generator is None:
            return False
        try:
            from discord_tron_master.classes.app_config import AppConfig

            cfg = AppConfig()
            user_config = cfg.get_user_config(user_id=int(actor_id))
        except Exception:
            user_config = {}
        user_config["auto_model"] = False
        user_config["model"] = model or "flux"
        user_config["steps"] = 16
        user_config["guidance_scaling"] = 3.0
        user_config["resolution"] = {"width": 768, "height": 768}
        job_metadata = dict(metadata or {})
        job_metadata.setdefault("zork_scene", True)
        job_metadata.setdefault("suppress_image_reactions", True)
        job_metadata.setdefault("zork_store_avatar", True)
        try:
            await generator.generate_from_user_config(
                ctx=None,
                user_config=user_config,
                user_id=int(actor_id),
                prompt=prompt,
                job_metadata=job_metadata,
            )
            return True
        except Exception as exc:
            logger.debug("Avatar generation enqueue failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# ZorkMemoryAdapter
# ---------------------------------------------------------------------------
class ZorkMemoryAdapter:
    """Wraps DTM's ``ZorkMemory`` class behind ``MemorySearchPort``.

    Handles the ``campaign_id: str`` ↔ ``int`` translation since
    ZorkMemory uses integer campaign IDs internally.  TGE campaign IDs
    are UUID strings; the legacy integer ID is stored in the campaign's
    ``state_json`` under ``_legacy_campaign_id`` by the data migration.
    """

    _legacy_id_cache: Dict[str, int] = {}

    @classmethod
    def _int_campaign_id(cls, campaign_id: str) -> int:
        # Fast path: already an integer string (pre-migration callers)
        try:
            return int(campaign_id)
        except (TypeError, ValueError):
            pass
        # UUID path: look up the legacy integer ID from the campaign state.
        cached = cls._legacy_id_cache.get(campaign_id)
        if cached is not None:
            return cached
        try:
            from discord_tron_master.adapters.emulator_bridge import EmulatorBridge
            campaign = EmulatorBridge.query_campaign(campaign_id)
            if campaign is not None:
                import json as _json
                state = _json.loads(getattr(campaign, "state_json", "{}") or "{}")
                legacy = state.get("_legacy_campaign_id")
                if legacy is not None:
                    cls._legacy_id_cache[campaign_id] = int(legacy)
                    return int(legacy)
        except Exception:
            pass
        raise ValueError(f"Cannot resolve integer campaign ID for {campaign_id!r}")

    @classmethod
    def _maybe_int_campaign_id(cls, campaign_id: str) -> int | None:
        try:
            return cls._int_campaign_id(campaign_id)
        except Exception:
            return None

    def search(
        self,
        query: str,
        campaign_id: str,
        top_k: int = 5,
    ) -> list[tuple[int, str, str, float]]:
        from discord_tron_master.classes.zork_memory import ZorkMemory

        cid = self._maybe_int_campaign_id(campaign_id)
        if cid is None:
            return []
        hits = ZorkMemory.search(query, cid, top_k=top_k)
        results: list[tuple[int, str, str, float]] = []
        for hit in hits:
            if isinstance(hit, dict):
                results.append((
                    int(hit.get("turn_id", 0)),
                    str(hit.get("kind", "")),
                    str(hit.get("content", "")),
                    float(hit.get("score", 0.0)),
                ))
            elif isinstance(hit, (list, tuple)) and len(hit) >= 4:
                results.append((int(hit[0]), str(hit[1]), str(hit[2]), float(hit[3])))
        return results

    def delete_turns_after(self, campaign_id: str, turn_id: int) -> int:
        from discord_tron_master.classes.zork_memory import ZorkMemory

        cid = self._maybe_int_campaign_id(campaign_id)
        if cid is None:
            return 0
        return ZorkMemory.delete_turns_after(
            cid, turn_id
        )

    def list_terms(
        self,
        campaign_id: str,
        wildcard: str = "%",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        from discord_tron_master.classes.zork_memory import ZorkMemory

        cid = self._maybe_int_campaign_id(campaign_id)
        if cid is None:
            return []
        if hasattr(ZorkMemory, "list_memory_terms"):
            return ZorkMemory.list_memory_terms(cid, wildcard=wildcard, limit=limit)
        return []

    def store_memory(
        self,
        campaign_id: str,
        *,
        category: str,
        memory: str,
        term: str | None = None,
    ) -> tuple[bool, str]:
        from discord_tron_master.classes.zork_memory import ZorkMemory

        cid = self._maybe_int_campaign_id(campaign_id)
        if cid is None:
            return False, "legacy-memory-unavailable"
        return ZorkMemory.store_manual_memory(
            cid,
            category=category,
            content=memory,
            term=term,
        )

    def search_curated(
        self,
        query: str,
        campaign_id: str,
        *,
        category: str | None = None,
        top_k: int = 5,
    ) -> list[tuple[str, str, float]]:
        from discord_tron_master.classes.zork_memory import ZorkMemory

        cid = self._maybe_int_campaign_id(campaign_id)
        if cid is None:
            return []
        return ZorkMemory.search_manual_memories(
            query,
            cid,
            category=category,
            top_k=top_k,
        )

    def store_turn_embedding(
        self,
        turn_id: int,
        campaign_id: str,
        actor_id: str | None,
        kind: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        from discord_tron_master.classes.zork_memory import ZorkMemory

        cid = self._maybe_int_campaign_id(campaign_id)
        if cid is None:
            return
        user_id: int | None = None
        if actor_id is not None:
            try:
                user_id = int(actor_id)
            except (TypeError, ValueError):
                user_id = None
        ZorkMemory.store_turn_embedding(
            turn_id,
            cid,
            user_id,
            kind,
            content,
            metadata=metadata,
        )


# ---------------------------------------------------------------------------
# IMDBLookupAdapter
# ---------------------------------------------------------------------------
class IMDBLookupAdapter:
    """Standalone IMDB scraper behind ``IMDBLookupPort``.

    This is a direct reimplementation rather than a wrapper, because the
    original code lives inside ZorkEmulator as @classmethods with no
    external dependencies beyond ``requests``.
    """

    SUGGEST_URL = "https://sg.media-imdb.com/suggestion/{first}/{query}.json"
    TIMEOUT = 5

    def _search_single(self, query: str, max_results: int = 3) -> list[dict]:
        import requests as _requests

        clean = re.sub(r"[^\w\s]", "", query.strip().lower())
        if not clean:
            return []
        first = clean[0] if clean[0].isalpha() else "a"
        encoded = clean.replace(" ", "_")
        url = self.SUGGEST_URL.format(first=first, query=encoded)
        try:
            resp = _requests.get(
                url, timeout=self.TIMEOUT, headers={"User-Agent": "Mozilla/5.0"}
            )
            if resp.status_code != 200:
                return []
        except Exception:
            return []
        data = resp.json()
        results: list[dict] = []
        for item in data.get("d", [])[:max_results]:
            title = item.get("l")
            if not title:
                continue
            results.append({
                "imdb_id": item.get("id", ""),
                "title": title,
                "year": item.get("y"),
                "type": item.get("q", ""),
                "stars": item.get("s", ""),
            })
        return results

    def search(self, query: str, max_results: int = 3) -> list[dict]:
        results = self._search_single(query, max_results)
        if results:
            return results
        stripped = re.sub(
            r"\b(s\d+e\d+|season\s*\d+|episode\s*\d+|ep\s*\d+)\b",
            "",
            query,
            flags=re.IGNORECASE,
        ).strip()
        if stripped and stripped != query:
            results = self._search_single(stripped, max_results)
            if results:
                return results
        words = query.strip().split()
        for length in range(len(words) - 1, 1, -1):
            sub = " ".join(words[:length])
            results = self._search_single(sub, max_results)
            if results:
                return results
        return []

    def fetch_details(self, imdb_id: str) -> dict:
        import json as _json

        import requests as _requests

        if not imdb_id or not imdb_id.startswith("tt"):
            return {}
        url = f"https://www.imdb.com/title/{imdb_id}/"
        try:
            resp = _requests.get(
                url,
                timeout=self.TIMEOUT + 3,
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            if resp.status_code != 200:
                return {}
            match = re.search(
                r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
                resp.text,
                re.DOTALL,
            )
            if not match:
                return {}
            ld_data = _json.loads(match.group(1))
            details: dict = {}
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
        except Exception:
            return {}

    def enrich(self, results: list[dict], max_enrich: int = 1) -> list[dict]:
        for r in results[:max_enrich]:
            imdb_id = r.get("imdb_id", "")
            if imdb_id:
                details = self.fetch_details(imdb_id)
                if details.get("description"):
                    r["description"] = details["description"]
                if details.get("genre"):
                    r["genre"] = details["genre"]
                if details.get("actors"):
                    r["stars"] = ", ".join(details["actors"])
        return results
