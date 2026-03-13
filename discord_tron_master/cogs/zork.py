from discord.ext import commands
import datetime
import io
import logging
import shlex
import discord

from discord_tron_master.bot import DiscordBot
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.classes.zork_emulator import ZorkEmulator
from discord_tron_master.classes.zork_memory import ZorkMemory
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


class Zork(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = AppConfig()

    def _prefix(self) -> str:
        return self.config.get_command_prefix()

    def _ensure_guild(self, ctx) -> bool:
        if ctx.guild is None:
            return False
        return True

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
        try:
            campaign_id = int(binding.get("campaign_id") or 0)
        except (TypeError, ValueError):
            campaign_id = 0
        if campaign_id <= 0:
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

    @staticmethod
    def _filter_narration(text: str) -> str:
        lines = text.split("\n")
        filtered = [
            line for line in lines
            if not any(f in line.lower() for f in Zork._NARRATION_LINE_FILTERS)
        ]
        return "\n".join(filtered)

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
            await DiscordBot.send_large_message(ctx_like, f"[Notice] {notice}")
        narration = self._filter_narration(narration)
        mention = getattr(getattr(ctx_like, "author", None), "mention", None)
        if mention:
            msg = await DiscordBot.send_large_message(
                ctx_like, f"{mention}\n{narration}"
            )
        else:
            msg = await DiscordBot.send_large_message(ctx_like, narration)
        # If a timer was just scheduled, register the message for later editing.
        if campaign_id is not None and msg is not None:
            ZorkEmulator.register_timer_message(campaign_id, msg.id)
        if msg is not None:
            try:
                await msg.add_reaction("ℹ️")
            except Exception:
                logger.debug("Failed adding Zork info reaction", exc_info=True)
        return msg

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
                await channel.send(attachment_text.replace("ERROR:", "", 1))
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
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return

        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
        operation, parsed_value = self._parse_source_material_options(label)

        if operation == "clear":
            with app.app_context():
                docs = ZorkMemory.list_source_material_documents(campaign.id, limit=200)
                removed_rows = ZorkMemory.clear_source_material_documents(campaign.id)
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
                docs = ZorkMemory.list_source_material_documents(campaign.id, limit=200)
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
                removed_rows = ZorkMemory.delete_source_material_document(
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
                    campaign = ZorkCampaign.query.get(campaign.id)
                    state = ZorkEmulator.get_campaign_state(campaign)
                    styles = state.get(ZorkEmulator.LITERARY_STYLES_STATE_KEY)
                    if not isinstance(styles, dict):
                        styles = {}
                    styles.update(literary_profiles)
                    state[ZorkEmulator.LITERARY_STYLES_STATE_KEY] = styles
                    campaign.state_json = ZorkEmulator._dump_json(state)
                    campaign.updated = db.func.now()
                    db.session.commit()
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
        await DiscordBot.send_large_message(
            ctx,
            message_text or "No source-material changes were made.",
            max_chars=3900,
        )

    async def _handle_literary_reference_command(self, ctx, *, label: str = None):
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return

        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return

        operation, parsed_value = self._parse_literary_reference_options(label)

        if operation == "list":
            with app.app_context():
                campaign = ZorkCampaign.query.get(campaign.id)
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
            await DiscordBot.send_large_message(
                ctx,
                f"Literary style profiles ({len(lines)}):\n" + "\n".join(lines),
                max_chars=3900,
            )
            return

        if operation == "clear":
            with app.app_context():
                campaign = ZorkCampaign.query.get(campaign.id)
                state = ZorkEmulator.get_campaign_state(campaign)
                styles = state.get(ZorkEmulator.LITERARY_STYLES_STATE_KEY)
                if not isinstance(styles, dict) or not styles:
                    await ctx.send("No literary style profiles to clear.")
                    return
                count = len(styles)
                state.pop(ZorkEmulator.LITERARY_STYLES_STATE_KEY, None)
                campaign.state_json = ZorkEmulator._dump_json(state)
                campaign.updated = db.func.now()
                db.session.commit()
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
                campaign = ZorkCampaign.query.get(campaign.id)
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
                campaign.updated = db.func.now()
                db.session.commit()
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
            campaign = ZorkCampaign.query.get(campaign.id)
            state = ZorkEmulator.get_campaign_state(campaign)
            styles = state.get(ZorkEmulator.LITERARY_STYLES_STATE_KEY)
            if not isinstance(styles, dict):
                styles = {}
            styles.update(profiles)
            state[ZorkEmulator.LITERARY_STYLES_STATE_KEY] = styles
            campaign.state_json = ZorkEmulator._dump_json(state)
            campaign.updated = db.func.now()
            db.session.commit()

        keys = sorted(profiles.keys())
        keys_text = ", ".join(f"`{k}`" for k in keys)
        await ctx.send(
            f"Stored {len(profiles)} literary style profile(s): {keys_text}\n"
            f"Characters can reference these via `literary_style` in character_updates."
        )

    async def _handle_campaign_rules_command(self, ctx, *, raw: str = None):
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return

        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
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
            await DiscordBot.send_large_message(
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
            await DiscordBot.send_large_message(
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
                await DiscordBot.send_large_message(
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
            await DiscordBot.send_large_message(
                ctx,
                f"Updated campaign rule `{key_text}`.\n"
                f"Old: {old_value}\n"
                f"New: {new_value}",
                max_chars=3900,
            )
            return

        await DiscordBot.send_large_message(
            ctx,
            f"Added campaign rule `{key_text}`.\n"
            f"New: {new_value}",
            max_chars=3900,
        )

    async def _handle_rewind(self, message, app):
        """Process a 'rewind' reply: restore state and purge messages."""
        target_msg_id = message.reference.message_id

        with app.app_context():
            channel_rec = ZorkEmulator.get_or_create_channel(
                message.guild.id, message.channel.id
            )
            if not channel_rec.enabled or channel_rec.active_campaign_id is None:
                await message.channel.send("No active campaign in this channel.")
                return
            campaign_id = channel_rec.active_campaign_id
            campaign = ZorkCampaign.query.get(campaign_id)
            if campaign is None:
                await message.channel.send("Campaign not found.")
                return
            if ZorkEmulator.is_in_setup_mode(campaign):
                await message.channel.send("Cannot rewind during campaign setup.")
                return

        lock = ZorkEmulator._get_lock(campaign_id)
        async with lock:
            with app.app_context():
                result = ZorkEmulator.execute_rewind(
                    campaign_id, target_msg_id, channel_id=message.channel.id
                )

            if result is None:
                await message.channel.send(
                    "Could not find a snapshot for that message. "
                    "Only messages created after the rewind feature was added can be rewound to."
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

            # Cancel any pending timed events.
            ZorkEmulator.cancel_pending_timer(campaign_id)
            ZorkEmulator.cancel_pending_sms_deliveries(campaign_id)

            await message.channel.send(
                f"Rewound to turn {turn_id}. Removed {deleted_count} subsequent turn(s)."
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
        if not content:
            return

        campaign_id = None
        if message.guild is None:
            binding = self._get_private_dm_binding(message.author.id)
            if binding is None:
                return
            with app.app_context():
                campaign = ZorkCampaign.query.get(binding["campaign_id"])
                if campaign is None:
                    self.config.clear_zork_private_dm(message.author.id)
                    await message.channel.send(
                        "Your linked private Zork campaign no longer exists. "
                        f"Re-enable it from the campaign channel with `{self._prefix()}zork private enable`."
                    )
                    return
                if ZorkEmulator.is_in_setup_mode(campaign):
                    await message.channel.send(
                        f"Campaign `{campaign.name}` is still in setup. "
                        "Finish setup in the server channel or thread before using private DMs."
                    )
                    return
            campaign_id, error_text = await ZorkEmulator.begin_turn_for_campaign(
                message,
                binding["campaign_id"],
            )
            if error_text is not None:
                await message.channel.send(error_text)
                return
            if campaign_id is None:
                return
        else:
            with app.app_context():
                if not ZorkEmulator.is_channel_enabled(
                    message.guild.id, message.channel.id
                ):
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

        if campaign_id is None:
            campaign_id, error_text = await ZorkEmulator.begin_turn(
                message, command_prefix=self._prefix()
            )
            if error_text is not None:
                return
            if campaign_id is None:
                return

        # Setup mode intercept — route to setup handler instead of play_action.
        with app.app_context():
            _setup_campaign = ZorkCampaign.query.get(campaign_id)
            _in_setup = _setup_campaign and ZorkEmulator.is_in_setup_mode(
                _setup_campaign
            )
        if _in_setup:
            reaction_added = await ZorkEmulator._add_processing_reaction(message)
            try:
                with app.app_context():
                    _setup_campaign = ZorkCampaign.query.get(campaign_id)
                    response = await ZorkEmulator.handle_setup_message(
                        message, content, _setup_campaign, command_prefix=self._prefix()
                    )
                    if response:
                        await DiscordBot.send_large_message(message, response)
            finally:
                if reaction_added:
                    await ZorkEmulator._remove_processing_reaction(message)
                ZorkEmulator.end_turn(campaign_id, message.author.id)
            return

        reaction_added = await ZorkEmulator._add_processing_reaction(message)
        try:
            narration = await ZorkEmulator.play_action(
                message,
                content,
                command_prefix=self._prefix(),
                campaign_id=campaign_id,
                manage_claim=False,
            )
            if narration is None:
                return
            notices = ZorkEmulator.pop_turn_ephemeral_notices(
                campaign_id, message.author.id
            )
            msg = await self._send_action_reply(
                message, narration, campaign_id=campaign_id, notices=notices
            )
            if msg is not None:
                with app.app_context():
                    ZorkEmulator.record_turn_message_ids(
                        campaign_id, message.id, msg.id
                    )
        finally:
            if reaction_added:
                await ZorkEmulator._remove_processing_reaction(message)
            ZorkEmulator.end_turn(campaign_id, message.author.id)

    @commands.group(name="zork", invoke_without_command=True)
    async def zork(self, ctx, *, action: str = None):
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
                        await DiscordBot.send_large_message(
                            ctx, campaign.last_narration
                        )
                    return

                campaign = (
                    ZorkCampaign.query.get(channel.active_campaign_id)
                    if channel.active_campaign_id
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
            _setup_campaign = ZorkCampaign.query.get(campaign_id)
            _in_setup = _setup_campaign and ZorkEmulator.is_in_setup_mode(
                _setup_campaign
            )
        if _in_setup:
            reaction_added = await ZorkEmulator._add_processing_reaction(ctx)
            try:
                with app.app_context():
                    _setup_campaign = ZorkCampaign.query.get(campaign_id)
                    response = await ZorkEmulator.handle_setup_message(
                        ctx, action, _setup_campaign, command_prefix=self._prefix()
                    )
                    if response:
                        await DiscordBot.send_large_message(ctx, response)
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
            f"- `{prefix}zork style [prompt|default]` view or set the style direction for this channel/thread (max 120 chars; creator/admin only to change)\n"
            f"- `{prefix}zork private [enable|disable]` bind your DMs to the current campaign so your turns stay private but shared history stays in-world\n"
            f"- `{prefix}zork campaigns` list campaigns\n"
            f"- `{prefix}zork campaign <name>` switch or create campaign\n"
            f"- `{prefix}zork identity <name>` set your character name\n"
            f"- `{prefix}zork persona <text>` set your character persona\n"
            f"- `{prefix}zork rails` show strict guardrails mode status for active campaign\n"
            f"- `{prefix}zork rails enable|disable` toggle strict on-rails action validation for active campaign\n"
            f"- `{prefix}zork on-rails` show on-rails narrative mode status\n"
            f"- `{prefix}zork on-rails enable|disable` lock/unlock story to the chapter outline\n"
            f"- `{prefix}zork timed-events` show timed events status; enable/disable toggles\n"
            f"- `{prefix}zork speed [value]` view or set game speed multiplier (0.1–10.0, creator/admin only)\n"
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
            f"- `{prefix}zork map` draw an ASCII map for your location\n"
            f"- `{prefix}zork reset` reset this channel's Zork state (Image Admin only)\n"
            f"- `{prefix}zork disable` disable adventure mode in this channel\n"
            f"\n**In-game shortcuts** (type directly, no prefix):\n"
            f"- `calendar` / `cal` / `events` — view game time & upcoming events\n"
            f"- `roster` / `characters` / `npcs` — view the NPC roster\n"
        )
        await DiscordBot.send_large_message(ctx, message)

    @zork.command(name="enable")
    async def zork_enable(self, ctx):
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
            channel.updated = db.func.now()
            db.session.commit()
            await ctx.send("Adventure mode disabled for this channel.")

    @zork.command(name="campaigns")
    async def zork_campaigns(self, ctx):
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
            active_id = channel.active_campaign_id
            lines = self._format_preset_campaigns(active_id, campaigns)
            if not lines:
                await ctx.send(
                    f"No campaigns yet. Use `{self._prefix()}zork campaign <name>` to create one."
                )
                return
            await DiscordBot.send_large_message(ctx, "Campaigns:\n" + "\n".join(lines))

    @zork.command(name="campaign")
    async def zork_campaign(self, ctx, *, name: str = None):
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
                if channel.active_campaign_id is None:
                    await ctx.send("No active campaign in this channel.")
                    return
                campaign = ZorkCampaign.query.get(channel.active_campaign_id)
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
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
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
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            ZorkEmulator.set_guardrails_enabled(campaign, True)
            await ctx.send(f"Rails mode enabled for campaign `{campaign.name}`.")

    @zork_rails.command(name="disable")
    async def zork_rails_disable(self, ctx):
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            ZorkEmulator.set_guardrails_enabled(campaign, False)
            await ctx.send(f"Rails mode disabled for campaign `{campaign.name}`.")

    @zork.group(name="on-rails", invoke_without_command=True)
    async def zork_on_rails(self, ctx):
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
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
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            ZorkEmulator.set_on_rails(campaign, True)
            await ctx.send(f"On-rails mode enabled for campaign `{campaign.name}`.")

    @zork_on_rails.command(name="disable")
    async def zork_on_rails_disable(self, ctx):
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            ZorkEmulator.set_on_rails(campaign, False)
            await ctx.send(f"On-rails mode disabled for campaign `{campaign.name}`.")

    @zork.command(name="puzzles")
    async def zork_puzzles(self, ctx, *, mode: str = None):
        """View or set puzzle mode for active campaign."""
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
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
            db.session.commit()
            await ctx.send(f"Puzzle mode set to `{normalized}` for campaign `{campaign.name}`.")

    @zork.group(name="timed-events", invoke_without_command=True)
    async def zork_timed_events(self, ctx):
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
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
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            if campaign.created_by != ctx.author.id and not await self._is_image_admin(
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
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            if campaign.created_by != ctx.author.id and not await self._is_image_admin(
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
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
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
            player.state_json = ZorkEmulator._dump_json(player_state)
            if old_name and isinstance(old_name, str) and old_name != character_name:
                campaign.summary = (campaign.summary or "").replace(
                    old_name, character_name
                )
                campaign.updated = db.func.now()
            player.updated = db.func.now()
            db.session.commit()
            await ctx.send(f"Identity set to `{character_name}`.")

    @zork.command(name="persona")
    async def zork_persona(self, ctx, *, persona: str = None):
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
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
                await DiscordBot.send_large_message(ctx, message)
                return

            persona = persona.strip()
            if not persona:
                await ctx.send("Persona cannot be empty.")
                return
            persona = persona[:400]
            player_state["persona"] = persona
            player.state_json = ZorkEmulator._dump_json(player_state)
            player.updated = db.func.now()
            db.session.commit()
            await ctx.send("Persona updated for your character.")

    @zork.command(name="private")
    async def zork_private(self, ctx, *, mode: str = None):
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
                ZorkCampaign.query.get(channel.active_campaign_id)
                if channel.active_campaign_id
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

    @zork.command(name="backend")
    async def zork_backend(self, ctx, *, option: str = None):
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return

        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return

        current = self.config.get_zork_backend_config(
            ctx.channel.id,
            default_backend="zai",
        )
        current_backend = str(current.get("backend") or "zai").strip() or "zai"
        current_model = str(current.get("model") or "").strip() or None
        allowed = ", ".join(f"`{item}`" for item in AppConfig.ZORK_BACKEND_OPTIONS)
        if option is None:
            model_text = f"`{current_model}`" if current_model else "`default`"
            await ctx.send(
                f"Current Zork backend for this channel/thread: `{current_backend}`.\n"
                f"Current model override: {model_text}\n"
                f"Available backends: {allowed}"
            )
            return

        try:
            tokens = shlex.split(option)
        except ValueError:
            tokens = str(option or "").split()
        if not tokens:
            await ctx.send(f"Available backends: {allowed}")
            return
        normalized = self.config.normalize_zork_backend(tokens[0], default="")
        model = " ".join(str(token or "").strip() for token in tokens[1:]).strip() or None
        if normalized not in AppConfig.ZORK_BACKEND_OPTIONS:
            await ctx.send(
                f"Unknown backend `{option}`. Available backends: {allowed}"
            )
            return

        if campaign.created_by != ctx.author.id and not await self._is_image_admin(ctx):
            await ctx.send(
                "Only the campaign creator or an Image Admin can change this setting."
            )
            return

        self.config.set_zork_backend(ctx.channel.id, normalized, model=model)
        model_text = f" with model `{model}`" if model else " with the backend default model"
        await ctx.send(
            f"Zork backend for `{campaign.name}` in this channel/thread set to `{normalized}`{model_text}."
        )

    @zork.command(name="style")
    async def zork_style(self, ctx, *, option: str = None):
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
                campaign = ZorkCampaign.query.get(binding.get("campaign_id"))
                if campaign is None:
                    self.config.clear_zork_private_dm(ctx.author.id)
                    await ctx.send(
                        "Your private DM binding is stale. Re-enable it from the campaign channel."
                    )
                    return
            else:
                channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
                if channel.active_campaign_id is None:
                    await ctx.send("No active campaign in this channel.")
                    return
                campaign = ZorkCampaign.query.get(channel.active_campaign_id)
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
            and campaign.created_by != ctx.author.id
            and not await self._is_image_admin(ctx)
        ):
            await ctx.send(
                "Only the campaign creator or an Image Admin can change this setting."
            )
            return

        if style_text is None:
            self.config.clear_zork_style(ctx.channel.id)
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
                    channel.active_campaign_id = campaign.id
                    channel.enabled = True
                    channel.updated = db.func.now()
                    db.session.commit()
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
                        campaign.updated = db.func.now()
                        db.session.commit()
                    if not requested_name and (
                        campaign_state.get("setup_phase")
                        or campaign_state.get("default_persona")
                    ):
                        campaign.state_json = "{}"
                        campaign.summary = ""
                        campaign.last_narration = None
                        campaign.updated = db.func.now()
                        db.session.commit()
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
            channel.active_campaign_id = campaign.id
            channel.enabled = True
            channel.updated = db.func.now()
            db.session.commit()
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
                    campaign.updated = db.func.now()
                    db.session.commit()
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

        await ctx.send(f"Created Zork thread: {thread.mention}")
        if create_empty:
            await thread.send(
                f"{ctx.author.mention} Campaign: `{resolved_campaign_name}`.\n"
                f"Empty thread created. Run `{self._prefix()}zork thread` here when you want to start setup."
            )
        else:
            await thread.send(
                f"{ctx.author.mention} Campaign: `{resolved_campaign_name}`.\n\n{setup_message}"
            )

    @zork.command(name="share")
    async def zork_share(self, ctx, thread_id: str = None):
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
                    ZorkCampaign.query.get(channel.active_campaign_id)
                    if channel.active_campaign_id
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
            source_channel = ZorkChannel.query.filter_by(channel_id=source_thread_id).first()
            if source_channel is None or source_channel.active_campaign_id is None:
                await ctx.send("That thread/channel is not linked to an active Zork campaign.")
                return
            source_campaign = ZorkCampaign.query.get(source_channel.active_campaign_id)
            if source_campaign is None:
                await ctx.send("That thread/channel points to a missing Zork campaign.")
                return
            if (
                target_channel.active_campaign_id == source_campaign.id
                and bool(target_channel.enabled)
            ):
                await ctx.send(
                    f"This thread/channel is already linked to `{source_campaign.name}`."
                )
                return
            target_channel.active_campaign_id = source_campaign.id
            target_channel.enabled = True
            target_channel.updated = db.func.now()
            db.session.commit()
            source_guild_text = (
                f" from guild `{source_campaign.guild_id}`"
                if source_campaign.guild_id != ctx.guild.id
                else ""
            )
            await ctx.send(
                f"Linked this thread/channel to shared campaign `{source_campaign.name}`"
                f"{source_guild_text} via source id `{source_thread_id}`."
            )

    @zork.command(name="source-material")
    async def zork_source_material(self, ctx, *, label: str = None):
        await self._handle_source_material_command(ctx, label=label)

    @zork.command(name="campaign-rules")
    async def zork_campaign_rules(self, ctx, *, raw: str = None):
        await self._handle_campaign_rules_command(ctx, raw=raw)

    @zork.command(name="literary-reference")
    async def zork_literary_reference(self, ctx, *, label: str = None):
        await self._handle_literary_reference_command(ctx, label=label)

    @zork.command(name="source-material-export")
    async def zork_source_material_export(self, ctx):
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return

        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            docs = ZorkMemory.list_source_material_documents(campaign.id, limit=200)
            export_rows: list[tuple[str, str, str]] = []
            used_names: set[str] = set()
            for row in docs:
                document_key = str(row.get("document_key") or "").strip()
                document_label = str(row.get("document_label") or "").strip()
                if not document_key:
                    continue
                units = ZorkMemory.get_source_material_document_units(
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
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
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
                campaign = ZorkCampaign.query.get(campaign_id)
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
                docs = ZorkMemory.list_source_material_documents(campaign.id, limit=200)
                source_export_files: dict[str, str] = {}
                used_names = set(export_files.keys())
                for row in docs:
                    document_key = str(row.get("document_key") or "").strip()
                    document_label = str(row.get("document_label") or "").strip()
                    if not document_key:
                        continue
                    units = ZorkMemory.get_source_material_document_units(
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
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
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
                await DiscordBot.send_large_message(ctx, "\n".join(lines))
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
                player.updated = db.func.now()
                db.session.commit()
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
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
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
                await DiscordBot.send_large_message(
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
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
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
            await DiscordBot.send_large_message(ctx, message)

    @zork.command(name="level")
    async def zork_level(self, ctx):
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
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
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            players = ZorkPlayer.query.filter_by(campaign_id=campaign.id).all()
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
                    if player.last_active and player.last_active >= cutoff
                    else "inactive"
                )
                extra = f" | party: {party_status}" if party_status else ""
                lines.append(f"- <@{player.user_id}>: {room} ({status}{extra})")
            await DiscordBot.send_large_message(ctx, "Locations:\n" + "\n".join(lines))

    @zork.command(name="hint")
    async def zork_hint(self, ctx):
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
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
            await DiscordBot.send_large_message(ctx, "\n".join(lines))

    @zork.command(name="speed")
    async def zork_speed(self, ctx, *, value: str = None):
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
            if campaign is None:
                await ctx.send("No active campaign in this channel.")
                return
            if value is None:
                current = ZorkEmulator.get_speed_multiplier(campaign)
                await ctx.send(
                    f"Current speed multiplier: `{current}x` for campaign `{campaign.name}`.\n"
                    f"Use `{self._prefix()}zork speed <value>` to change (0.1–10.0)."
                )
                return
            if campaign.created_by != ctx.author.id and not await self._is_image_admin(ctx):
                await ctx.send(
                    "Only the campaign creator or an Image Admin can change the speed multiplier."
                )
                return
            try:
                multiplier = float(value.strip())
            except ValueError:
                await ctx.send("Speed multiplier must be a number (0.1–10.0).")
                return
            if multiplier < 0.1 or multiplier > 10.0:
                await ctx.send("Speed multiplier must be between 0.1 and 10.0.")
                return
            ZorkEmulator.set_speed_multiplier(campaign, multiplier)
            await ctx.send(f"Speed multiplier set to `{multiplier}x` for campaign `{campaign.name}`.")

    @zork.command(name="difficulty")
    async def zork_difficulty(self, ctx, *, value: str = None):
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
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

            if campaign.created_by != ctx.author.id and not await self._is_image_admin(ctx):
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
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        with app.app_context():
            channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
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
            await DiscordBot.send_large_message(
                ctx, ZorkEmulator.format_roster(characters)
            )

    @zork.command(name="map")
    async def zork_map(self, ctx):
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return
        ascii_map = await ZorkEmulator.generate_map(ctx, command_prefix=self._prefix())
        if ascii_map.startswith("```") and ascii_map.endswith("```"):
            await DiscordBot.send_large_message(ctx, ascii_map)
            return
        await DiscordBot.send_large_message(ctx, f"```\n{ascii_map}\n```")

    @zork.command(name="reset")
    async def zork_reset(self, ctx):
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
            if channel.active_campaign_id is None:
                await ctx.send("No active campaign in this channel.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
            if campaign is None:
                channel.active_campaign_id = None
                channel.updated = db.func.now()
                db.session.commit()
                await ctx.send("Channel state cleared.")
                return

            shared_refs = ZorkChannel.query.filter(
                ZorkChannel.guild_id == ctx.guild.id,
                ZorkChannel.active_campaign_id == campaign.id,
                ZorkChannel.channel_id != ctx.channel.id,
            ).count()

            if shared_refs > 0:
                # Avoid wiping state for other channels still bound to this campaign.
                reset_name = f"{campaign.name}-reset-{ctx.channel.id}-{int(datetime.datetime.now(datetime.timezone.utc).timestamp())}"
                new_campaign = ZorkEmulator.get_or_create_campaign(
                    ctx.guild.id, reset_name, ctx.author.id
                )
                ZorkSnapshot.query.filter_by(campaign_id=new_campaign.id).delete(
                    synchronize_session=False
                )
                ZorkTurn.query.filter_by(campaign_id=new_campaign.id).delete(
                    synchronize_session=False
                )
                ZorkPlayer.query.filter_by(campaign_id=new_campaign.id).delete(
                    synchronize_session=False
                )
                new_campaign.summary = ""
                new_campaign.state_json = "{}"
                new_campaign.last_narration = None
                new_campaign.updated = db.func.now()
                channel.active_campaign_id = new_campaign.id
                channel.enabled = True
                channel.updated = db.func.now()
                db.session.commit()
                ZorkMemory.delete_campaign_embeddings(new_campaign.id)
                await ctx.send(
                    f"Channel reset to fresh campaign `{new_campaign.name}` (shared campaign left untouched)."
                )
                return

            ZorkSnapshot.query.filter_by(campaign_id=campaign.id).delete(
                synchronize_session=False
            )
            ZorkTurn.query.filter_by(campaign_id=campaign.id).delete(
                synchronize_session=False
            )
            ZorkPlayer.query.filter_by(campaign_id=campaign.id).delete(
                synchronize_session=False
            )
            campaign.summary = ""
            campaign.state_json = "{}"
            campaign.last_narration = None
            campaign.updated = db.func.now()
            channel.enabled = True
            channel.updated = db.func.now()
            db.session.commit()
            ZorkMemory.delete_campaign_embeddings(campaign.id)
            ZorkEmulator.cancel_pending_timer(campaign.id)
            ZorkEmulator.cancel_pending_sms_deliveries(campaign.id)
            await ctx.send(f"Reset campaign `{campaign.name}` for this channel.")

    @zork.command(name="restart")
    async def zork_restart(self, ctx):
        if not await self.bot.is_owner(ctx.author):
            await ctx.send("This command is restricted to the bot owner.")
            return
        await ctx.send(
            "Restart initiated. Rejecting new requests and draining in-flight turns..."
        )
        ZorkEmulator.request_shutdown()
        drained = await ZorkEmulator.wait_for_drain(timeout=120)
        if drained:
            await ctx.send("All turns drained. Shutting down now.")
        else:
            remaining = len(ZorkEmulator._inflight_turns)
            await ctx.send(
                f"Drain timeout. {remaining} turn(s) still in-flight. Forcing shutdown."
            )
        await self.bot.close()
        import sys

        sys.exit(0)
