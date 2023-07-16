from discord_tron_master.classes.job import Job
from discord_tron_master.models.schedulers import Schedulers
from discord_tron_master.classes.app_config import AppConfig
import logging, base64

class PromptVariationJob(Job):
    def __init__(self, payload, extra_payload:dict = None):
        super().__init__("gpu", "image_variation", "prompt_variation", payload)
        self.extra_payload = extra_payload

    async def format_payload(self):
        # Format payload into a message format for WebSocket handling.
        bot, config, ctx, prompt, discord_first_message, image = self.payload
        logging.info(f"Formatting message for img2img payload")
        logging.debug(f"{self.payload}")
        user_config = config.get_user_config(user_id=ctx.author.id)
        overridden_user_id = ctx.author.id
        if self.extra_payload is not None and "user_config" in self.extra_payload:
            user_config = self.extra_payload["user_config"]
            overridden_user_id = self.extra_payload["user_id"]
        flask = AppConfig.get_flask()
        with flask.app_context():
            message = {
                "job_type": self.job_type,
                "job_id": self.id,
                "module_name": self.module_name,
                "module_command": self.module_command,
                "discord_context": self.context_to_dict(ctx),
                "overridden_user_id": overridden_user_id,
                "image_prompt": prompt,
                "prompt": prompt,
                "image_data": image,
                "scheduler_config": Schedulers.get_user_scheduler(user_config).to_dict(),
                "discord_first_message": self.discordmsg_to_dict(discord_first_message),
                "config": user_config,
            }
        return message