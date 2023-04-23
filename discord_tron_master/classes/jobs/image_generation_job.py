from discord_tron_master.classes.job import Job
import logging, json
from discord_tron_master.models.transformers import Transformers
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.models.schedulers import Schedulers
flask = AppConfig.get_flask()
class ImageGenerationJob(Job):
    def __init__(self, payload):
        super().__init__("gpu", "image_generation", "generate_image", payload)

    async def format_payload(self):
        # Format payload into a message format for WebSocket handling.
        bot, config, ctx, prompt, discord_first_message = self.payload
        logging.info(f"Formatting message for payload: {self.payload}")
        user_config = config.get_user_config(user_id=ctx.author.id)
        with flask.app_context():
            message = {
                "job_type": self.job_type,
                "job_id": self.id,
                "module_name": self.module_name,
                "module_command": self.module_command,
                "discord_context": self.context_to_dict(ctx),
                "image_prompt": prompt,
                "discord_first_message": self.discordmsg_to_dict(discord_first_message),
                "config": user_config,
                "scheduler_config": Schedulers.get_user_scheduler(user_config).to_dict(),
                "model_config": self.get_transformer_details(user_config)
            }
        return message
    async def execute(self):
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