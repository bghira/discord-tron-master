from discord.ext import commands
from asyncio import Lock
from discord_tron_master.classes.openai.text import GPT
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.classes.guilds import Guilds
import logging, traceback
import discord as discord_library
from PIL import Image
from discord_tron_master.bot import DiscordBot
from discord_tron_master.classes.jobs.image_generation_job import ImageGenerationJob
from discord_tron_master.bot import clean_traceback
from discord_tron_master.adapters.emulator_bridge import EmulatorBridge as ZorkEmulator

# For queue manager, etc.
discord = DiscordBot.get_instance()
guild_config = Guilds()


class Reactions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = AppConfig()

    async def _is_image_admin_member(self, user) -> bool:
        for role in getattr(user, "roles", []) or []:
            if getattr(role, "name", "") == "Image Admin":
                return True
        return False

    async def _purge_messages_after(self, channel, target_message):
        def should_delete(m):
            return m.id != target_message.id

        try:
            await channel.purge(after=target_message, check=should_delete, limit=200)
        except Exception:
            logging.exception("Zork rewind via reaction: purge failed")

    async def _delete_turn_messages(self, channel, narrator_message, user_message_id: int | None = None):
        if user_message_id:
            try:
                user_message = await channel.fetch_message(int(user_message_id))
            except Exception:
                user_message = None
            if user_message is not None:
                try:
                    await user_message.delete()
                except Exception:
                    logging.debug("Zork delete-turn: failed deleting paired user message", exc_info=True)
        try:
            await narrator_message.delete()
        except Exception:
            logging.debug("Zork delete-turn: failed deleting narrator message", exc_info=True)

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        if user.bot:  # Ignore bot reactions
            return
        # Code to execute when a reaction is added
        # await reaction.message.channel.send(f'{user.name} has reacted with {reaction.emoji}!')
        logging.debug(f"{user.name} has reacted with {reaction.emoji}!")
        no_op = ["👎", "👍"]  # WE do nothing with these right now.
        if reaction.emoji in no_op:
            logging.debug(f"Ignoring no-op reaction: {reaction.emoji}")
            return
        # Now, we need to check if this is a reaction to a message we sent.
        logging.debug(
            f"Reaction: {reaction} on message content: {reaction.message.content}"
        )
        if reaction.message.author != self.bot.user:
            logging.debug(f"Ignoring reaction on message not from me.")
            return
        if str(reaction.emoji) in {"ℹ️", "ℹ", "⏪", "❌"}:
            app = AppConfig.get_flask()
            if app is None:
                return
            with app.app_context():
                turn = ZorkEmulator.get_turn_for_message(reaction.message.id)
                if str(reaction.emoji) in {"ℹ️", "ℹ"}:
                    info_text = ZorkEmulator.get_turn_info_text_for_message(
                        reaction.message.id
                    )
                else:
                    info_text = None
            if turn is not None:
                if str(reaction.emoji) in {"ℹ️", "ℹ"}:
                    if not info_text:
                        return
                    if reaction.message.guild is None:
                        await DiscordBot.send_large_message(reaction.message, info_text)
                    else:
                        await DiscordBot.send_large_message(
                            reaction.message,
                            f"{user.mention}\n{info_text}",
                        )
                    return

                is_admin = await self._is_image_admin_member(user)
                owner_ok = str(getattr(turn, "actor_id", "") or "") == str(user.id)
                if not owner_ok and not is_admin:
                    await reaction.message.channel.send(
                        f"{user.mention} only the turn owner or an Image Admin can do that."
                    )
                    return

                dm_scope = reaction.message.guild is None
                with app.app_context():
                    if str(reaction.emoji) == "⏪":
                        result = ZorkEmulator.execute_rewind(
                            turn.campaign_id,
                            reaction.message.id,
                            channel_id=reaction.message.channel.id,
                            rewind_user_id=user.id if dm_scope else None,
                            player_only=dm_scope,
                        )
                    else:
                        result = ZorkEmulator.execute_delete_turn(
                            turn.campaign_id,
                            reaction.message.id,
                            channel_id=reaction.message.channel.id,
                            delete_user_id=user.id if dm_scope else None,
                            player_only=dm_scope,
                        )

                if str(reaction.emoji) == "⏪":
                    if result is None:
                        await reaction.message.channel.send(
                            "Could not find a snapshot for that message. Only newer rewind-capable turns can be rewound to."
                        )
                        return
                    turn_id, deleted_count = result
                    await self._purge_messages_after(reaction.message.channel, reaction.message)
                    if not dm_scope:
                        ZorkEmulator.cancel_pending_timer(turn.campaign_id)
                        ZorkEmulator.cancel_pending_sms_deliveries(turn.campaign_id)
                        await reaction.message.channel.send(
                            f"Rewound to turn {turn_id}. Removed {deleted_count} subsequent turn(s)."
                        )
                    else:
                        await reaction.message.channel.send(
                            f"Rewound your DM thread to turn {turn_id}. Removed {deleted_count} of your subsequent turn(s)."
                        )
                    return

                status = str((result or {}).get("status") or "")
                if status == "not-found":
                    await reaction.message.channel.send("Turn not found.")
                    return
                if status == "forbidden":
                    await reaction.message.channel.send(
                        f"{user.mention} you can only remove your own DM turns."
                    )
                    return
                if status == "not-latest":
                    await reaction.message.channel.send(
                        "Only the latest turn in this scope can be removed safely."
                    )
                    return
                if status == "no-prior-snapshot":
                    await reaction.message.channel.send(
                        "That turn cannot be removed because there is no prior snapshot to restore from."
                    )
                    return
                if status != "ok":
                    await reaction.message.channel.send("Could not remove that turn.")
                    return

                if not dm_scope:
                    ZorkEmulator.cancel_pending_timer(turn.campaign_id)
                    ZorkEmulator.cancel_pending_sms_deliveries(turn.campaign_id)
                await self._delete_turn_messages(
                    reaction.message.channel,
                    reaction.message,
                    int((result or {}).get("user_message_id") or 0),
                )
                if dm_scope:
                    await reaction.message.channel.send(
                        f"Removed your latest DM turn {result.get('turn_id')}."
                    )
                else:
                    await reaction.message.channel.send(
                        f"Removed latest turn {result.get('turn_id')}."
                    )
                return
        image_urls = []
        img = None
        for embed in reaction.message.embeds:
            logging.debug(f"Embed: {embed}, url: {embed.image.url}")
            image_urls.append(embed.image.url)
            import os

            filename = os.path.basename(embed.image.url)
            img = Image.open(os.path.join(self.config.get_web_root(), filename))
            logging.debug(f"Image info: {img.info}")
            if img.info == {}:
                logging.debug(f"No info found, continuing")
                continue
        # We have our info.
        logging.debug(f"User id: {user.id}")
        # Set the config:
        new_config = {}
        if img is not None:
            import json

            new_config = json.loads(img.info["user_config"])
        current_config = self.config.get_user_config(user.id)

        user_id = 69
        if "user_id" in new_config:
            user_id = new_config["user_id"]
            del new_config["user_id"]
        # Did load correctly?
        if new_config == {} and reaction.emoji != "❌":
            logging.debug(f"Error loading config from image info.")
            return
        if reaction.emoji == "📋":
            # We want to clone/copy the settings of this post.
            logging.debug(
                f'Would clone settings: user_config {img.info["user_config"]}, prompt {img.info["prompt"]}.'
            )
            # Keep the user's current seed instead of setting a static one.
            new_config["seed"] = current_config["seed"]
            self.config.set_user_config(user.id, new_config)
            # Send a message back to the reaction thread/channel:
            await reaction.message.channel.send(
                f"Cloned settings from <@{user_id}>'s post for {user.mention}."
            )
        if reaction.emoji == "♻️":
            # We are going to resubmit this task for the new user that requested it.
            logging.debug(
                f'Would resubmit settings: user_config {img.info["user_config"]}, prompt {img.info["prompt"]}'
            )
            generator = self.bot.get_cog("Generate")
            prompt = json.loads(img.info["prompt"])
            # Now the whitespace:
            prompt = prompt.strip()
            if "style" in new_config:
                new_config["style"] = "base"
            new_config["seed"] = -1
            await generator.generate_from_user_config(
                reaction.message, user_config=new_config, prompt=prompt, user_id=user.id
            )
            return
        # reactions = [ '♻️', '©️', '🌱', '📜', '1️⃣', '2️⃣', '3️⃣', '4️⃣', '❌', '💾' ]  # Maybe: '👍', '👎'
        if reaction.emoji == "🌱":
            # We want to copy the 'seed' from the image user_config into the requesting user's config:
            logging.debug(
                f'Would copy seed: user_config {img.info["user_config"]}, prompt {img.info["prompt"]}'
            )
            # Get the seed from the image:
            seed = int(json.loads(img.info["seed"]))
            # Set the seed in the requesting user's config:
            self.config.set_user_setting(user.id, "seed", seed)
            # Send a message back to the reaction thread/channel:
            await reaction.message.channel.send(
                f"Copied seed {seed} from <@{user_id}>'s post for {user.mention}."
            )
            return
        if reaction.emoji == "📜":
            # We want to generate a new image using just the prompt from the post, with the user's config.
            if "style" in new_config and (
                new_config["style"] != "base" and new_config["style"] is not None
            ):
                # Override style with base, since the prompt already had one.
                current_config["style"] = "base"
            logging.debug(
                f'Would resubmit settings: user_config {current_config}, prompt {img.info["prompt"]}'
            )
            generator = self.bot.get_cog("Generate")
            prompt = json.loads(img.info["prompt"])
            # Now the whitespace:
            prompt = prompt.strip()
            await generator.generate_from_user_config(
                reaction.message,
                user_config=current_config,
                prompt=prompt,
                user_id=user.id,
            )
            return
        if reaction.emoji == "❌":
            # We want to delete the post, if the user_id is the same as the user reacting.
            if user_id == user.id or str(user.id) in reaction.message.content:
                await reaction.message.delete()
                return
        their_config = self.config.get_user_config(user_id)
        their_aspect_ratio = (
            their_config["resolution"]["width"] / their_config["resolution"]["height"]
        )
        old_aspect_ratio = (
            new_config["resolution"]["width"] / new_config["resolution"]["height"]
        )
        if their_aspect_ratio != old_aspect_ratio:
            their_config["resolution"] = new_config[
                "resolution"
            ]  # img2img should have resolution overridden if the aspect isn't the same.
        extra_params = {"user_config": their_config, "user_id": user_id}
        if reaction.emoji == "1️⃣":
            # We want to do an image variation, with the first image in the embeds.
            current_config = self.config.get_user_config(user.id)
            logging.debug(
                f'Would perform img2img variation, prompt {img.info["prompt"]}'
            )
            generator = self.bot.get_cog("Img2img")
            # _handle_image_attachment(self, message, attachment, prompt_override: str = None)
            prompt = json.loads(img.info["prompt"])
            await generator._handle_image_attachment(
                reaction.message,
                image_urls[0],
                prompt_override=prompt,
                user_config_override=extra_params,
            )
            return
        if reaction.emoji == "2️⃣":
            # We want to do an image variation, with the first image in the embeds.
            current_config = self.config.get_user_config(user.id)
            logging.debug(
                f'Would perform img2img variation, prompt {img.info["prompt"]}'
            )
            generator = self.bot.get_cog("Img2img")
            # _handle_image_attachment(self, message, attachment, prompt_override: str = None)
            prompt = json.loads(img.info["prompt"])
            await generator._handle_image_attachment(
                reaction.message,
                image_urls[1],
                prompt_override=prompt,
                user_config_override=extra_params,
            )
            return
        if reaction.emoji == "3️⃣":
            # We want to do an image variation, with the first image in the embeds.
            current_config = self.config.get_user_config(user.id)
            logging.debug(
                f'Would perform img2img variation, prompt {img.info["prompt"]}'
            )
            generator = self.bot.get_cog("Img2img")
            # _handle_image_attachment(self, message, attachment, prompt_override: str = None)
            prompt = json.loads(img.info["prompt"])
            await generator._handle_image_attachment(
                reaction.message,
                image_urls[2],
                prompt_override=prompt,
                user_config_override=extra_params,
            )
            return
        if reaction.emoji == "4️⃣":
            # We want to do an image variation, with the first image in the embeds.
            current_config = self.config.get_user_config(user.id)
            logging.debug(
                f'Would perform img2img variation, prompt {img.info["prompt"]}'
            )
            generator = self.bot.get_cog("Img2img")
            # _handle_image_attachment(self, message, attachment, prompt_override: str = None)
            prompt = json.loads(img.info["prompt"])
            await generator._handle_image_attachment(
                reaction.message,
                image_urls[3],
                prompt_override=prompt,
                user_config_override=extra_params,
            )
            return
        # Floppy disk should send the image as an embed to the main channel the thread is in.
        if reaction.emoji == "💾":
            # Find the parent channel for the thread:
            logging.debug(f"Reaction message: {reaction.message}")
            logging.debug(f"Reaction channel: {reaction.message.channel}")
            logging.debug(f"Reaction parent: {reaction.message.channel.parent}")

            parent_channel = reaction.message.channel.parent

            # Strip the original mention from the prompt
            original_content = reaction.message.content
            if "<@" in original_content:
                original_content = original_content.split(">", 1)[1]
                original_content = original_content.strip()

            preservation_message = f"User {user.mention} has preserved the following image:\n{original_content}"

            for image_url in image_urls:
                # Grab the image data from the URL:
                import requests
                from io import BytesIO

                response = requests.get(image_url)
                image_data = BytesIO(response.content)
                file = discord_library.File(
                    image_data, filename=image_url.split("/")[-1]
                )
                new_msg = await parent_channel.send(
                    content=preservation_message, file=file
                )
                # Add 'x' emote to the message:
                await new_msg.add_reaction("❌")

            return

        # if reaction.emoji = "👍":
        #     best_of_channel_id = guild_config.get_guild_setting(reaction.message.guild.id, "best_of_channel_id")
        #     if best_of_channel_id is None:
        #         logging.debug(f'No best of channel set for guild {reaction.message.guild.id}.')
        #         return
        #     best_of_channel = self.bot.get_channel(best_of_channel_id)
        #     if best_of_channel is None:
        #         logging.debug(f'Could not find best of channel {best_of_channel_id}.')
        #         return
        #     # Let's send the entire reaction.message to the best_of_channel.
        #     await best_of_channel.send(reaction.message)
        #     await reaction.message.delete()

    @commands.Cog.listener()
    async def on_reaction_remove(self, reaction, user):
        if user.bot:  # Ignore bot reactions
            return
        # Code to execute when a reaction is removed
        # await reaction.message.channel.send(f'{user.name} has removed their reaction of {reaction.emoji}!')
        logging.debug(f"{user.name} has removed their reaction of {reaction.emoji}!")
