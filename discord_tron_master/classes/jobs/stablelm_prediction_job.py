import logging, json, time
from discord_tron_master.classes.job import Job
from discord_tron_master.classes.app_config import AppConfig

class StableLMPredictionJob(Job):
    def __init__(self, author_id: str, payload):
        super().__init__("stablelm", "stablelm", "predict", author_id, payload)
        self.date_created = time.time()

    async def format_payload(self):
        # Format payload into a message format for WebSocket handling.
        num_artefacts = len(self.payload)
        if num_artefacts == 5:
            bot, config, ctx, prompt, discord_first_message = self.payload
        elif num_artefacts == 6:
            bot, config, ctx, prompt, discord_first_message, image = self.payload
        logging.info(f"Formatting message for payload: {self.payload}")
        user_config = config.get_user_config(user_id=ctx.author.id)
        message = {
            "job_type": self.job_type,
            "job_id": self.id,
            "module_name": self.module_name,
            "module_command": self.module_command,
            "discord_context": self.context_to_dict(ctx),
            "prompt": prompt,
            "discord_first_message": self.discordmsg_to_dict(discord_first_message),
            "config": user_config
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