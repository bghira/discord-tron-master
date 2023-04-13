from discord_tron_master.classes.job import Job
import logging
from discord_tron_master.models.transformers import Transformers

class ImageGenerationJob(Job):
    def __init__(self, payload):
        super().__init__("gpu", "image_generation", "generate_image", payload)

    async def format_payload(self):
        # Format payload into a message format for WebSocket handling.
        bot, config, ctx, prompt, discord_first_message = self.payload
        logging.info(f"Formatting message for payload: {self.payload}")
        user_config = config.get_user_config(user_id=ctx.author.id)
        message = {
            "job_type": self.job_type,
            "job_id": self.id,
            "module_name": self.module_name,
            "module_command": self.module_command,
            "discord_context": self.context_to_dict(ctx),
            "image_prompt": prompt,
            "discord_first_message": self.discordmsg_to_dict(discord_first_message),
            "config": user_config,
            "model_config": self.get_transformer_details(user_config)
        }
        return message

    def get_transformer_details(self, user_config):
        model_id = user_config['model']
        transformer = Transformers.get_by_full_model_id(model_id)
        return transformer.to_dict()