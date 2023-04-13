from discord.ext import commands
from asyncio import Lock
from typing import List
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.models.transformers import Transformers
import logging

class Model(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = AppConfig()

    @commands.command(name="model-list", help="List all models currently approved for use.")
    async def model_list(self, ctx):
        user_id = ctx.author.id
        user_config = self.config.get_user_config(user_id=user_id)
        app = AppConfig.flask
        with app.app_context():
            all_transformers = Transformers.get_all_approved()
        wrapper = "```"
        def build_transformer_output(transformer):
            return f"{transformer.model_owner}/{transformer.model_id}: {transformer.description}\n" \
                f"!model {transformer.model_owner}/{transformer.model_id}\n"

        def split_into_chunks(text_lines: List[str], max_length: int = 2000) -> List[str]:
            chunks = []
            current_chunk = ""
            for line in text_lines:
                if len(current_chunk) + len(line) + len(wrapper) + len(wrapper) > max_length:
                    chunks.append(current_chunk)
                    current_chunk = ""
                current_chunk += wrapper + line + wrapper
            if current_chunk:
                chunks.append(current_chunk)
            return chunks

        transformer_outputs = [build_transformer_output(t) for t in all_transformers]
        message_chunks = split_into_chunks(transformer_outputs)

        if message_chunks:
            for chunk in message_chunks:
                await ctx.send(chunk)
        else:
            message = "Evidently, there are zero registered models available."
            await ctx.send(message)

    @commands.command(name="model-description", help="Are you an image admin? Set a description for your model.")
    async def model_description(self, ctx, model_id, *, description):
        user_id = ctx.author.id
        user_config = self.config.get_user_config(user_id=user_id)

        user_roles = ctx.author.roles
        message = "Sorry, you can not do that."
        for role in user_roles:
            if role.name == "Image Admin":
                message = "You do not have permission to do that."
                app = AppConfig.flask
                with app.app_context():
                    transformer = Transformers.set_description(model_id, description)
                    if not transformer:
                        message = "That model does not exist."
                    else:
                        message = "Successfully set the new description for " + str(model_id)
        await ctx.send(message)

    @commands.command(name="model", help="Set your currently-used model.")
    async def model(self, ctx, full_model_name: str = None):
        app = AppConfig.flask

        if not full_model_name:
            current_model_id = self.config.get_user_setting(ctx.author.id, "model")
            if current_model_id:
                await ctx.send(f"Your current model is: {current_model_id}")
            else:
                await ctx.send("You do not have a model set.")
            return

        if "/" not in full_model_name:
            await ctx.send("Model name must be in the format `owner/model`.")
            return
        with app.app_context():
            new_model_id, new_model_owner = full_model_name.split('/')[1], full_model_name.split('/')[0]
            existing = Transformers.query.filter_by(model_id=new_model_id, model_owner=new_model_owner).first()

        if not existing:
            await ctx.send("That model is not registered for use. To make your horse cock porn, or whatever, use `!model-add <model> <image|text> <description>` where `image|text` determines whether it's a diffuser or language model.")
            return

        await ctx.send("Your model is now set to: " + str(full_model_name))
        self.config.set_user_setting(ctx.author.id, "model", full_model_name)

    @commands.command(name="model-delete", help="Delete a model. Not available to non-admins.")
    async def model_delete(self, ctx, full_model_name: str):
        # Is the user in the Image Admin role?
        app = AppConfig.flask
        is_admin = await self.is_admin(ctx)
        if not is_admin:
            await ctx.send("sory bae, u must be admuin 😭😭😭 u rek me inside in the worst waysz")
            return
        logging.info("Deleting model!")
        try:
            with app.app_context():
                transformers = Transformers()
                transformers.delete_by_full_model_id(full_model_name)
                await ctx.send(f"Sigh. Well, it is done. That model is now obliviated from existence.")
        except Exception as e:
            logging.error(f"Could not delete model: {e}")
            await ctx.send(f"Sorry bae, could not delete that model for you. Have you tried using more lube? {e}")

    @commands.command(name="model-add", help="Add a model to the list for approval.")
    async def model_add(self, ctx, full_model_name: str, model_type: str, *, description):
        app = AppConfig.flask
        with app.app_context():
            existing = Transformers.query.filter_by(model_id=full_model_name).first()
            if existing:
                await ctx.send("That model is already registered. Go use it to make your horse cock porn, or whatever!")
                return
        # Was the model name in the owner/model format?
        if "/" not in full_model_name:
            await ctx.send("Model name must be in the format `owner/model`.")
            return
        # Repo id must use alphanumeric chars or '-', '', '.', '--' and '..' are forbidden, '-' and '.' cannot start or end the name, max length is 96
        # import re
        # if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9.-_]{0,94}[a-zA-Z0-9]$', full_model_name):
        #     await ctx.send("Model name must be alphanumeric.")
        #     return
        # Is the model type valid?
        if model_type not in ["image", "text"]:
            await ctx.send("Model type must be either `image` or `text`.")
            return
        # Attempt to list model details via huggingface API
        try:
            import logging, traceback
            from huggingface_hub.hf_api import model_info
            # Use huggingface to grab the model list:
            model_details = model_info(full_model_name)
            # Model Name: andite/anything-v4.0, Tags: ['diffusers', 'en', 'stable-diffusion', 'stable-diffusion-diffusers', 'text-to-image', 'license:creativeml-openrail-m', 'has_space'], Task: text-to-image
            logging.info(f"Found model_details? {model_details}")
        except Exception as e:
            logging.error(f"Error while attempting to list model details: {e}, traceback: {traceback.format_exc()}")
            await ctx.send(f"I am very sorry. That model was not found. I searched for it at, https://huggingface.co/api/models/{full_model_name}.")
            return
        user_id = ctx.author.id
        addition_status = False

        user_config = self.config.get_user_config(user_id=user_id)
        try:
            import json
            with app.app_context():
                new_record = Transformers()
                new_record.create(
                    model_id=full_model_name,
                    model_type=model_type,
                    preferred_ar="none",
                    recommended_negative="set a recommended negative string for this model with `!model-negative <model_id> <negative_string>`",
                    recommended_positive="set a recommended positive string for this model with `!model-positive <model_id> <positive_string>`",
                    approved=addition_status,
                    description=description,
                    tags=json.dumps(model_details.tags),
                    added_by=user_id,
                )
        except Exception as e:
            logging.error(f"Error while attempting to create new record: {e}, traceback: {traceback.format_exc()}")
            await ctx.send(f"I am very sorry. I was unable to create a new record for that model. You might be touching yourself at night too frequently. Have you tried touching yourself less?")
            return
        await ctx.send("That model exists? Cool. I'll add it to the list for approval. Please be patient. I'm a bot, and I'm slow. I'll let you know when it's approved.")

    async def is_admin(self, ctx):
        # Was the user in the "Image Admin" group?
        user_roles = ctx.author.roles
        for role in user_roles:
            if role.name == "Image Admin":
                return True
        return False