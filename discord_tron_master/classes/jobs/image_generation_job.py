from discord_tron_master.classes.job import Job
import logging, json
from discord_tron_master.models.transformers import Transformers
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.models.schedulers import Schedulers
import time
flask = AppConfig.get_flask()
class ImageGenerationJob(Job):
    def __init__(self, author_id: str, payload, extra_payload:dict = None):
        super().__init__("gpu", "image_generation", "generate_image", author_id, payload)
        self.extra_payload = extra_payload
        self.date_created = time.time()

    async def format_payload(self):
        # Format payload into a message format for WebSocket handling.
        num_artefacts = len(self.payload)
        if num_artefacts == 5:
            bot, config, ctx, prompt, discord_first_message = self.payload
        elif num_artefacts == 6:
            bot, config, ctx, prompt, discord_first_message, image = self.payload
        logging.info(f"Formatting message for payload: {self.payload}")
        overridden_user_id = None
        if self.extra_payload is not None and "user_config" in self.extra_payload:
            user_config = self.extra_payload["user_config"]
            overridden_user_id = self.extra_payload["user_id"]
            message_flags = self.extra_payload.get("message_flags", {})
        else:
            user_config = config.get_user_config(user_id=ctx.author.id)
            message_flags = {}
        discord_context = self.context_to_dict(ctx)
        if isinstance(message_flags, dict) and message_flags.get("zork_scene"):
            # Keep the marker on discord_context in case worker responses only echo this sub-object.
            discord_context["zork_scene"] = True
        with flask.app_context():
            message = {
                "job_type": self.job_type,
                "job_id": self.id,
                "module_name": self.module_name,
                "module_command": self.module_command,
                "discord_context": discord_context,
                "overridden_user_id": overridden_user_id,
                "image_prompt": prompt,
                "prompt": prompt,
                "discord_first_message": self.discordmsg_to_dict(discord_first_message),
                "config": user_config,
                "model_config": self.get_transformer_details(user_config),
                "message_flags": message_flags,
            }
        return message
    async def execute(self):
        if self.executed:
            logging.warning(f"Job {self.job_id} has already been executed. Ignoring.")
            return
        logging.info(f"Job {self.job_id} is executing. {self}")
        self.executed = True
        self.executed_date = time.time()
        websocket = self.worker.websocket
        message = await self.format_payload()
        try:
            await self.worker.send_websocket_message(json.dumps(message))
        except Exception as e:
            await self.discord_first_message.edit(content="Sorry, hossicle. We had an error sending your " + self.module_command + " job to worker: " + str(e))
            logging.error("Error sending websocket message: " + str(e) + " traceback: " + str(e.__traceback__))
            return False
    def get_transformer_details(self, user_config):
        model_id = user_config['model']
        app = AppConfig.flask
        with app.app_context():
            transformer = Transformers.get_by_full_model_id(model_id)
        return transformer.to_dict()
