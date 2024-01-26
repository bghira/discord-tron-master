from discord_tron_master.classes.job import Job
from discord_tron_master.models.schedulers import Schedulers
from discord_tron_master.classes.app_config import AppConfig
import logging, base64, time

class PromptlessVariationJob(Job):
    def __init__(self, author_id: str, payload):
        super().__init__("variation", "image_variation", "promptless_variation", author_id, payload)
        self.date_created = time.time()

    async def format_payload(self):
        # Format payload into a message format for WebSocket handling.
        bot, config, ctx, prompt, discord_first_message, image = self.payload
        logging.info(f"Formatting message for img2img payload")
        logging.debug(f"{self.payload}")
        user_config = config.get_user_config(user_id=ctx.author.id)
        flask = AppConfig.get_flask()
        with flask.app_context():
            message = {
                "job_type": self.job_type,
                "job_id": self.id,
                "module_name": self.module_name,
                "module_command": self.module_command,
                "discord_context": self.context_to_dict(ctx),
                "image_prompt": "__No prompt - promptless variation__",
                "prompt": "__No prompt - promptless variation__",
                "image_data": image,
                "scheduler_config": Schedulers.get_user_scheduler(user_config).to_dict(),
                "discord_first_message": self.discordmsg_to_dict(discord_first_message),
                "config": config.get_user_config(user_id=ctx.author.id)
            }
        return message