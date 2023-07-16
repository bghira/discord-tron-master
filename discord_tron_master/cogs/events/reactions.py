from discord.ext import commands
from asyncio import Lock
from discord_tron_master.classes.openai.text import GPT
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.classes.guilds import Guilds
import logging, traceback
from PIL import Image
from discord_tron_master.bot import DiscordBot
from discord_tron_master.classes.jobs.image_generation_job import ImageGenerationJob
from discord_tron_master.bot import clean_traceback

# For queue manager, etc.
discord = DiscordBot.get_instance()
guild_config = Guilds()
class Reactions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = AppConfig()

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        if user.bot:  # Ignore bot reactions
            return
        # Code to execute when a reaction is added
        # await reaction.message.channel.send(f'{user.name} has reacted with {reaction.emoji}!')
        logging.debug(f'{user.name} has reacted with {reaction.emoji}!')
        no_op = [ '👎', '👍' ] # WE do nothing with these right now.
        if reaction.emoji in no_op:
            logging.debug(f'Ignoring no-op reaction: {reaction.emoji}')
            return
        # Now, we need to check if this is a reaction to a message we sent.
        logging.debug(f'Reaction: {reaction} on message content: {reaction.message.content}')
        if reaction.message.author != self.bot.user:
            logging.debug(f'Ignoring reaction on message not from me.')
            return
        image_urls = []
        for embed in reaction.message.embeds:
            logging.debug(f'Embed: {embed}, url: {embed.image.url}')
            image_urls.append(embed.image.url)
            import os
            filename = os.path.basename(embed.image.url)
            img = Image.open(os.path.join(self.config.get_web_root(), filename))
            logging.debug(f'Image info: {img.info}')
            if img.info == {}:
                logging.debug(f'No info found, continuing')
                continue
        # We have our info.
        logging.debug(f'User id: {user.id}')
        # Set the config:
        import json
        new_config = json.loads(img.info["user_config"])
        user_id = 69
        if "user_id" in new_config:
            user_id = new_config["user_id"]
            del new_config["user_id"]
        # Did load correctly?
        if new_config == {}:
            logging.debug(f'Error loading config from image info.')
            return
        if reaction.emoji == "©️":
            # We want to clone the settings of this post.
            logging.debug(f'Would clone settings: user_config {img.info["user_config"]}, prompt {img.info["prompt"]}.')
            self.config.set_user_config(user.id, new_config)
            # Send a message back to the reaction thread/channel:
            await reaction.message.channel.send(f'Cloned settings from <@{user_id}>\'s post for {user.mention}.')
        if reaction.emoji == "♻️":
            # We are going to resubmit this task for the new user that requested it.
            logging.debug(f'Would resubmit settings: user_config {img.info["user_config"]}, prompt {img.info["prompt"]}')
            generator = self.bot.get_cog('Generate')
            prompt = json.loads(img.info["prompt"])
            # Now the whitespace:
            prompt = prompt.strip()
            await generator.generate_from_user_config(reaction.message, user_config=new_config, prompt=prompt, user_id=user.id)
            return
        # reactions = [ '♻️', '©️', '🌱', '📜', '1️⃣', '2️⃣', '3️⃣', '4️⃣', '❌' ]  # Maybe: '👍', '👎'
        if reaction.emoji == '🌱':
            # We want to copy the 'seed' from the image user_config into the requesting user's config:
            logging.debug(f'Would copy seed: user_config {img.info["user_config"]}, prompt {img.info["prompt"]}')
            # Get the seed from the image:
            seed = int(json.loads(img.info["seed"]))
            # Set the seed in the requesting user's config:
            self.config.set_user_setting(user.id, "seed", seed)
            # Send a message back to the reaction thread/channel:
            await reaction.message.channel.send(f'Copied seed {seed} from <@{user_id}>\'s post for {user.mention}.')
            return
        if reaction.emoji == '📜':
            # We want to generate a new image using just the prompt from the post, with the user's config.
            current_config = self.config.get_user_config(user.id)
            logging.debug(f'Would resubmit settings: user_config {current_config}, prompt {img.info["prompt"]}')
            generator = self.bot.get_cog('Generate')
            prompt = json.loads(img.info["prompt"])
            # Now the whitespace:
            prompt = prompt.strip()
            await generator.generate_from_user_config(reaction.message, user_config=current_config, prompt=prompt, user_id=user.id)
            return
        if reaction.emoji == '❌':
            # We want to delete the post, if the user_id is the same as the user reacting.
            if user_id == user.id:
                logging.debug(f'Would delete post: user_config {img.info["user_config"]}, prompt {img.info["prompt"]}')
                await reaction.message.delete()
                return
        extra_params = { "user_config": new_config['user_config'], "user_id": user_id }
        if reaction.emoji == '1️⃣':
            # We want to do an image variation, with the first image in the embeds.
            current_config = self.config.get_user_config(user.id)
            logging.debug(f'Would perform img2img variation, prompt {img.info["prompt"]}')
            generator = self.bot.get_cog('Img2img')
            # _handle_image_attachment(self, message, attachment, prompt_override: str = None)
            prompt = json.loads(img.info["prompt"])
            await generator._handle_image_attachment(reaction.message, image_urls[0], prompt_override=prompt, user_config_override=extra_params)
            return
        if reaction.emoji == '2️⃣':
            # We want to do an image variation, with the first image in the embeds.
            current_config = self.config.get_user_config(user.id)
            logging.debug(f'Would perform img2img variation, prompt {img.info["prompt"]}')
            generator = self.bot.get_cog('Img2img')
            # _handle_image_attachment(self, message, attachment, prompt_override: str = None)
            prompt = json.loads(img.info["prompt"])
            await generator._handle_image_attachment(reaction.message, image_urls[1], prompt_override=prompt, user_config_override=extra_params)
            return
        if reaction.emoji == '3️⃣':
            # We want to do an image variation, with the first image in the embeds.
            current_config = self.config.get_user_config(user.id)
            logging.debug(f'Would perform img2img variation, prompt {img.info["prompt"]}')
            generator = self.bot.get_cog('Img2img')
            # _handle_image_attachment(self, message, attachment, prompt_override: str = None)
            prompt = json.loads(img.info["prompt"])
            await generator._handle_image_attachment(reaction.message, image_urls[2], prompt_override=prompt, user_config_override=extra_params)
            return
        if reaction.emoji == '4️⃣':
            # We want to do an image variation, with the first image in the embeds.
            current_config = self.config.get_user_config(user.id)
            logging.debug(f'Would perform img2img variation, prompt {img.info["prompt"]}')
            generator = self.bot.get_cog('Img2img')
            # _handle_image_attachment(self, message, attachment, prompt_override: str = None)
            prompt = json.loads(img.info["prompt"])
            await generator._handle_image_attachment(reaction.message, image_urls[3], prompt_override=prompt, user_config_override=extra_params)
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
        logging.debug(f'{user.name} has removed their reaction of {reaction.emoji}!')

