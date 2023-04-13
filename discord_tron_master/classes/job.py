import uuid, logging, json
from typing import Dict, Any
class Job:
    def __init__(self, job_type: str, module_name: str, command_name: str, payload: Dict[str, Any]):
        self.id = str(uuid.uuid4())
        self.job_id = self.id
        self.job_type = job_type
        self.payload = payload
        self.module_name = module_name
        self.module_command = command_name
        self.discord_first_message = payload[4]  # Store the discord_first_message object
        self.worker = None

    def set_worker(self, worker):
        self.worker = worker

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
            "config": user_config
        }
        return message      

    def context_to_dict(self, ctx):
        # Format context into a dict for WebSocket handling.
        try:
            logging.debug(f"Trying to format context to dict: {ctx}")
            message_id = None
            if hasattr(ctx, "message"):
                message_id = ctx.message.id
            return {
                "author": {
                    "id": ctx.author.id,
                    "name": ctx.author.name,
                    "discriminator": ctx.author.discriminator
                },
                "channel": {
                    "id": ctx.channel.id,
                    "name": ctx.channel.name
                },
                "guild": {
                    "id": ctx.guild.id,
                    "name": ctx.guild.name
                },
                "message_id": message_id,
            }
        except Exception as e:
            logging.error("Error formatting context to dict: " + str(e))
            raise e
    def discordmsg_to_dict(self, discordmsg):
        # Format discord message into a dict for WebSocket handling.
        try:
            return {
                "author": {
                    "id": discordmsg.author.id,
                    "name": discordmsg.author.name,
                    "discriminator": discordmsg.author.discriminator
                },
                "channel": {
                    "id": discordmsg.channel.id,
                    "name": discordmsg.channel.name
                },
                "guild": {
                    "id": discordmsg.guild.id,
                    "name": discordmsg.guild.name
                },
                "message_id": discordmsg.id
            }
        except Exception as e:
            logging.error("Error formatting discord message to dict: " + str(e))
            raise e

    async def execute(self):
        websocket = self.worker.websocket
        message = await self.format_payload()
        try:
            await self.worker.send_websocket_message(json.dumps(message))
        except Exception as e:
            await self.discord_first_message.edit(content="Sorry, hossicle. We had an error sending your " + self.module_command + " job to worker: " + str(e))
            logging.error("Error sending websocket message: " + str(e) + " traceback: " + str(e.__traceback__))
            return False

    async def job_lost(self):
        try:
            await self.discord_first_message.edit(content="Sorry, hossicle. We had an error reassigning your " + self.module_command + f" job to another worker. Press F in chat for {self.worker.worker_id}. 😢😞😔😟😩😫😭😓😥😰❤️❤️")
            await self.discord_first_message.delete(delay=15)
            return True
        except Exception as e:
            logging.error("Error updating the discord message on job lost: " + str(e))
            raise e