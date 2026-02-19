from discord.ext import commands
import datetime
import logging
import discord

from discord_tron_master.bot import DiscordBot
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.classes.zork_emulator import ZorkEmulator, _zork_log
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

    async def _send_action_reply(
        self, ctx_like, narration: str, campaign_id: int = None
    ):
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
        return msg

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
        if message.guild is None:
            return
        if self._should_ignore_message(message):
            return
        app = AppConfig.get_flask()
        if app is None:
            return
        with app.app_context():
            if not ZorkEmulator.is_channel_enabled(
                message.guild.id, message.channel.id
            ):
                return
        content = self._strip_bot_mention(message.content)
        if not content:
            return

        # Rewind detection — must happen before begin_turn.
        content_stripped = message.content.strip().lower()
        if (
            content_stripped == "rewind"
            and message.reference is not None
            and message.reference.message_id is not None
        ):
            await self._handle_rewind(message, app)
            return

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
            msg = await self._send_action_reply(
                message, narration, campaign_id=campaign_id
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
            msg = await self._send_action_reply(ctx, narration, campaign_id=campaign_id)
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
            f"- `{prefix}zork thread [name]` create a dedicated Zork thread/campaign for yourself\n"
            f"- `{prefix}zork campaigns` list campaigns\n"
            f"- `{prefix}zork campaign <name>` switch or create campaign\n"
            f"- `{prefix}zork identity <name>` set your character name\n"
            f"- `{prefix}zork persona <text>` set your character persona\n"
            f"- `{prefix}zork rails` show strict guardrails mode status for active campaign\n"
            f"- `{prefix}zork rails enable|disable` toggle strict on-rails action validation for active campaign\n"
            f"- `{prefix}zork on-rails` show on-rails narrative mode status\n"
            f"- `{prefix}zork on-rails enable|disable` lock/unlock story to the chapter outline\n"
            f"- `{prefix}zork timed-events` show timed events status; enable/disable toggles\n"
            f"- `{prefix}zork avatar <prompt|accept|decline>` generate/accept/decline your character avatar\n"
            f"- `{prefix}zork attributes` view attributes and points\n"
            f"- `{prefix}zork attributes <name> <value>` set or create attribute\n"
            f"- `{prefix}zork stats` view player stats\n"
            f"- `{prefix}zork level` level up if you have enough XP\n"
            f"- `{prefix}zork map` draw an ASCII map for your location\n"
            f"- `{prefix}zork reset` reset this channel's Zork state (Image Admin only)\n"
            f"- `{prefix}zork disable` disable adventure mode in this channel\n"
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
            if not campaigns:
                await ctx.send(
                    f"No campaigns yet. Use `{self._prefix()}zork campaign <name>` to create one."
                )
                return
            active_id = channel.active_campaign_id
            lines = []
            for campaign in campaigns:
                marker = "*" if campaign.id == active_id else "-"
                lines.append(f"{marker} {campaign.name}")
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

    @zork.command(name="thread")
    async def zork_thread(self, ctx, *, name: str = None):
        if not self._ensure_guild(ctx):
            await ctx.send("Zork is only available in servers.")
            return
        app = AppConfig.get_flask()
        if app is None:
            await ctx.send("Zork is not ready yet (no Flask app).")
            return

        if isinstance(ctx.channel, discord.Thread):
            setup_message = None
            with app.app_context():
                channel, _ = ZorkEmulator.enable_channel(
                    ctx.guild.id, ctx.channel.id, ctx.author.id
                )
                campaign_name = name or f"thread-{ctx.channel.id}"
                campaign, _, _ = ZorkEmulator.set_active_campaign(
                    channel,
                    ctx.guild.id,
                    campaign_name,
                    ctx.author.id,
                    enforce_activity_window=False,
                )
                campaign_state = ZorkEmulator.get_campaign_state(campaign)
                if not campaign_state.get("setup_phase") and not campaign_state.get(
                    "default_persona"
                ):
                    setup_message = await ZorkEmulator.start_campaign_setup(
                        campaign, campaign_name
                    )
                    # Handle .txt attachment
                    att_text = await ZorkEmulator._extract_attachment_text(ctx.message)
                    if isinstance(att_text, str) and att_text.startswith("ERROR:"):
                        await ctx.send(att_text.replace("ERROR:", "", 1))
                    elif att_text:
                        summary = await ZorkEmulator._summarise_long_text(att_text, ctx.message)
                        if summary:
                            campaign = ZorkCampaign.query.get(campaign.id)
                            state = ZorkEmulator.get_campaign_state(campaign)
                            sd = state.get("setup_data") or {}
                            sd["attachment_summary"] = summary
                            state["setup_data"] = sd
                            campaign.state_json = ZorkEmulator._dump_json(state)
                            campaign.updated = db.func.now()
                            db.session.commit()
                            _zork_log(
                                f"ATTACHMENT STORED (in-thread) campaign={campaign.id}",
                                f"summary_len={len(summary)}",
                            )
                resolved_campaign_name = campaign.name
            if setup_message:
                await ctx.send(
                    f"Thread mode enabled. Campaign: `{resolved_campaign_name}`.\n\n{setup_message}"
                )
            else:
                await ctx.send(
                    f"Thread mode enabled here. Active campaign: `{resolved_campaign_name}`. "
                    f"This thread is tracked independently."
                )
            return

        thread_name = (name or f"zork-{ctx.author.display_name}").strip()
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
            channel, _ = ZorkEmulator.enable_channel(
                ctx.guild.id, thread.id, ctx.author.id
            )
            campaign_name = (name or thread.name or f"thread-{thread.id}").strip()
            if not campaign_name:
                campaign_name = f"thread-{thread.id}"
            campaign, _, _ = ZorkEmulator.set_active_campaign(
                channel,
                ctx.guild.id,
                campaign_name,
                ctx.author.id,
                enforce_activity_window=False,
            )
            setup_message = await ZorkEmulator.start_campaign_setup(
                campaign, name or thread_name
            )
            # Handle .txt attachment — progress messages go to the thread
            att_text = await ZorkEmulator._extract_attachment_text(ctx.message)
            if isinstance(att_text, str) and att_text.startswith("ERROR:"):
                await thread.send(att_text.replace("ERROR:", "", 1))
            elif att_text:
                summary = await ZorkEmulator._summarise_long_text(
                    att_text, ctx.message, channel=thread
                )
                if summary:
                    campaign = ZorkCampaign.query.get(campaign.id)
                    state = ZorkEmulator.get_campaign_state(campaign)
                    sd = state.get("setup_data") or {}
                    sd["attachment_summary"] = summary
                    state["setup_data"] = sd
                    campaign.state_json = ZorkEmulator._dump_json(state)
                    campaign.updated = db.func.now()
                    db.session.commit()
                    _zork_log(
                        f"ATTACHMENT STORED (new-thread) campaign={campaign.id}",
                        f"summary_len={len(summary)}",
                    )
            resolved_campaign_name = campaign.name

        await ctx.send(f"Created Zork thread: {thread.mention}")
        await thread.send(
            f"{ctx.author.mention} Campaign: `{resolved_campaign_name}`.\n\n{setup_message}"
        )

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
            now = datetime.datetime.utcnow()
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
                reset_name = f"{campaign.name}-reset-{ctx.channel.id}-{int(datetime.datetime.utcnow().timestamp())}"
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
            await ctx.send(f"Reset campaign `{campaign.name}` for this channel.")
