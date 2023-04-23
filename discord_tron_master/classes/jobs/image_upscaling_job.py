from discord_tron_master.classes.job import Job
import logging, base64
from discord_tron_master.models.schedulers import Schedulers

class ImageUpscalingJob(Job):
    def __init__(self, payload):
        super().__init__("gpu", "image_upscaling", "upscale", payload)

    async def format_payload(self):
        # Format payload into a message format for WebSocket handling.
        bot, config, ctx, prompt, discord_first_message, image = self.payload
        logging.info(f"Formatting message for img2img payload")
        logging.debug(f"{self.payload}")
        user_config = config.get_user_config(user_id=ctx.author.id)
        user_config["model"] = 'stabilityai/stable-diffusion-x4-upscaler'
        message = {
            "job_type": self.job_type,
            "job_id": self.id,
            "module_name": self.module_name,
            "module_command": self.module_command,
            "discord_context": self.context_to_dict(ctx),
            "image_prompt": prompt,
            "image_data": image,
            "discord_first_message": self.discordmsg_to_dict(discord_first_message),
            "config": user_config,
            "scheduler_config": Schedulers.get_user_scheduler(config),
            "upscaler": True
        }
        return message
