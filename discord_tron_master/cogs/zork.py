from discord.ext import commands
import datetime
import logging

from discord_tron_master.bot import DiscordBot
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.classes.zork_emulator import ZorkEmulator
from discord_tron_master.models.base import db
from discord_tron_master.models.zork import ZorkCampaign, ZorkPlayer

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
            if not ZorkEmulator.is_channel_enabled(message.guild.id, message.channel.id):
                return
        content = self._strip_bot_mention(message.content)
        if not content:
            return
        narration = await ZorkEmulator.play_action(message, content, command_prefix=self._prefix())
        await DiscordBot.send_large_message(message, narration)

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
                channel = ZorkEmulator.get_or_create_channel(ctx.guild.id, ctx.channel.id)
                if not channel.enabled:
                    _, campaign = ZorkEmulator.enable_channel(ctx.guild.id, ctx.channel.id, ctx.author.id)
                    message = (
                        f"Adventure mode enabled for this channel. Active campaign: `{campaign.name}`.\n"
                        f"Use `{self._prefix()}zork help` to see commands."
                    )
                    await ctx.send(message)
                    if campaign.last_narration:
                        await DiscordBot.send_large_message(ctx, campaign.last_narration)
                    return

                campaign = ZorkCampaign.query.get(channel.active_campaign_id) if channel.active_campaign_id else None
                if campaign is None:
                    _, campaign = ZorkEmulator.enable_channel(ctx.guild.id, ctx.channel.id, ctx.author.id)
                campaign_name = campaign.name
                await ctx.send(
                    f"Adventure mode is already enabled. Active campaign: `{campaign_name}`.\n"
                    f"Use `{self._prefix()}zork help` to see commands."
                )
                return

        narration = await ZorkEmulator.play_action(ctx, action, command_prefix=self._prefix())
        await DiscordBot.send_large_message(ctx, narration)

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
            f"- `{prefix}zork campaigns` list campaigns\n"
            f"- `{prefix}zork campaign <name>` switch or create campaign\n"
            f"- `{prefix}zork attributes` view attributes and points\n"
            f"- `{prefix}zork attributes <name> <value>` set or create attribute\n"
            f"- `{prefix}zork stats` view player stats\n"
            f"- `{prefix}zork level` level up if you have enough XP\n"
            f"- `{prefix}zork map` draw an ASCII map for your location\n"
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
            _, campaign = ZorkEmulator.enable_channel(ctx.guild.id, ctx.channel.id, ctx.author.id)
            await ctx.send(f"Adventure mode enabled. Active campaign: `{campaign.name}`.")

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
                await ctx.send(f"No campaigns yet. Use `{self._prefix()}zork campaign <name>` to create one.")
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
                channel, ctx.guild.id, name, ctx.author.id
            )
            if not allowed:
                await ctx.send(f"Cannot switch campaigns: {reason}.")
                return
            await ctx.send(f"Active campaign set to `{campaign.name}`.")

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
                await ctx.send(f"Adventure mode is disabled. Run `{self._prefix()}zork` first.")
                return
            campaign = ZorkCampaign.query.get(channel.active_campaign_id)
            if campaign is None:
                campaign = ZorkEmulator.get_or_create_campaign(ctx.guild.id, "main", ctx.author.id)
            player = ZorkEmulator.get_or_create_player(campaign.id, ctx.author.id, campaign=campaign)

            if not args:
                attrs = ZorkEmulator.get_player_attributes(player)
                total_points = ZorkEmulator.total_points_for_level(player.level)
                spent = ZorkEmulator.points_spent(attrs)
                remaining = total_points - spent
                if not attrs:
                    await ctx.send(f"No attributes set. Points available: {remaining}/{total_points}.")
                    return
                lines = [f"{k}: {v}" for k, v in sorted(attrs.items())]
                await DiscordBot.send_large_message(
                    ctx,
                    "Attributes:\n" + "\n".join(lines) + f"\nPoints available: {remaining}/{total_points}",
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
                await ctx.send(f"Usage: `{self._prefix()}zork attributes <name> <value>`")
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
                campaign = ZorkEmulator.get_or_create_campaign(ctx.guild.id, "main", ctx.author.id)
            player = ZorkEmulator.get_or_create_player(campaign.id, ctx.author.id, campaign=campaign)
            attrs = ZorkEmulator.get_player_attributes(player)
            total_points = ZorkEmulator.total_points_for_level(player.level)
            spent = ZorkEmulator.points_spent(attrs)
            remaining = total_points - spent
            xp_needed = ZorkEmulator.xp_needed_for_level(player.level)
            attrs_text = ", ".join([f"{k}={v}" for k, v in sorted(attrs.items())]) if attrs else "none"
            message = (
                f"Campaign: `{campaign.name}`\n"
                f"Level: {player.level} | XP: {player.xp}/{xp_needed}\n"
                f"Attributes: {attrs_text}\n"
                f"Points available: {remaining}/{total_points}"
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
                campaign = ZorkEmulator.get_or_create_campaign(ctx.guild.id, "main", ctx.author.id)
            player = ZorkEmulator.get_or_create_player(campaign.id, ctx.author.id, campaign=campaign)
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
                status = "active" if player.last_active and player.last_active >= cutoff else "inactive"
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
