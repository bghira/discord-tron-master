# A class for managing ChatML histories via Flask DB.
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.models.conversation import Conversations
from discord_tron_master.classes.openai.tokens import TokenTester

import json, logging

config = AppConfig()
app = AppConfig.flask

if app is None:
    raise Exception("Flask app is not initialized.")

class ChatML:
    def __init__(self, conversation: Conversations, token_limit: int = 120000, config_user_id: int = None):
        self.conversations = conversation
        self.user_id = conversation.owner
        self.history = conversation.get_history(self.user_id) or Conversations.get_new_history()
        self.config_user_id = self.user_id if config_user_id is None else config_user_id
        self.user_config = config.get_user_config(self.config_user_id)
        # Pick up their current role from their profile.
        self.role = self.user_config["gpt_role"]
        self.reply = {}
        self.tokenizer = TokenTester()
        self.token_limit = token_limit

    # Pick up a DB connector and store it. Create the conversation, if needed.
    async def initialize_conversation(self):
        with app.app_context():
            self.conversations = Conversations()
            self.conversation = await self.get_conversation_or_create()

    async def get_conversation_or_create(self):
        with app.app_context():
            conversation = self.conversations.get_by_owner(self.user_id)
            logging.debug(f"Picked up conversation from db: {conversation}")
            if conversation is None:
                conversation = self.conversations.create(self.user_id, self.role, self.history)
            return conversation

    async def validate_reply(self):
        # If we are too long, maybe we can clean it up.
        logging.debug(f"Validating reply")
        if await self.is_reply_too_long():
            # let's clean up until it does fit.
            logging.debug(f"Eureka! We can enter Alzheimers mode.")
            await self.remove_history_until_reply_fits()
        return True

    # See if we can fit everything without emptying the history too far.
    async def can_new_reply_fit_without_emptying_everything_from_history(self):
        if await self.is_history_empty() and not await self.is_reply_too_long():
            logging.debug(f"History is empty and reply is short enough to fit.")
            return True
        # If we are not empty, we can fit the reply if the history is short enough.
        if await self.get_history_token_count() < self.token_limit:
            logging.debug(f"History is short enough to fit.")
            return True
        logging.debug(f"Returning true by default. Maybe this should be a false..")
        return True

    # Loop over the history and remove items until the new reply will fit with the current text.
    async def remove_history_until_reply_fits(self):
        logging.debug(f"Stripping conversation back until the reply fits.")
        while await self.is_reply_too_long() and len(await self.get_history()) > 0:
            logging.debug(f"Reply is too long. Removing oldest history item.")
            await self.remove_oldest_history_item()
        logging.debug(f"Cleanup is complete. Returning newly pruned history.")
        return await self.get_history()

    # Remove the oldest history item and return the new history.
    async def remove_oldest_history_item(self):
        conversation = await self.get_conversation_or_create()
        item = conversation.history.pop(0)
        logging.debug(f"Removing oldest history item: {item}")
        with app.app_context():
            Conversations.set_history(self.user_id, conversation.history)
            return Conversations.get_history(owner=self.user_id)

    # Look at the actual token counts of each item and compare against our limit.
    async def is_reply_too_long(self):
        reply_token_count = await self.get_reply_token_count()
        history_token_count = await self.get_history_token_count()
        logging.debug(f"Reply token count: {reply_token_count}")
        logging.debug(f"History token count: {history_token_count}")
        if reply_token_count + history_token_count > self.token_limit:
            return True
        return False
    async def get_reply_token_count(self):
        return self.tokenizer.get_token_count(json.dumps(self.reply))
    async def get_history_token_count(self):
        # Pad the value by 64 to accommodate for the metadata in the JSON we can't really count right here.
        return self.tokenizer.get_token_count(json.dumps(await self.get_history())) + 512

    # Format the history as a string for OpenAI.
    async def get_prompt(self):
        return json.dumps(await self.get_history())
        
    async def get_history(self):
        conversation = await self.get_conversation_or_create()
        logging.debug(f"Conversation: {conversation}")
        return conversation.history

    async def is_history_empty(self):
        history = await self.get_history()
        if len(history) == 0:
            return True
        if history == Conversations.get_new_history():
            return True
        return False

    async def add_user_reply(self, content: str):
        return await self.add_to_history("user", content)

    async def add_system_reply(self, content: str):
        return await self.add_to_history("system", content)

    async def add_assistant_reply(self, content: str):
        return await self.add_to_history("assistant", content)

    async def add_to_history(self, role: str, content: str):
        # Store the reply for processing
        self.reply = {"role": role, "content": ChatML.clean(content)}
        if not await self.validate_reply():
            raise ValueError(f"I am sorry. It seems your reply would overrun the limits of reality and time. We are currently stuck at {self.token_limit} tokens, and your message used {await self.get_reply_token_count()} tokens. Please try again.")
        with app.app_context():
            conversation = await self.get_conversation_or_create()
            conversation.history.append(self.reply)
            self.conversations.set_history(self.user_id, conversation.history)
            return conversation.history

    # Clean the txt in a manner it can be inserted into the DB.
    @staticmethod
    def clean(text):
        # Clean the newlines.
        return text.replace('\\n', '\n')

    def truncate_conversation_history(self, conversation_history, new_prompt, max_tokens=2048):
        # Calculate tokens for new_prompt
        new_prompt_token_count = self.tokenizer.get_token_count(new_prompt)
        if new_prompt_token_count >= max_tokens:
            raise ValueError("The new prompt alone exceeds the maximum token limit.")
    
        # Calculate tokens for conversation_history
        conversation_history_token_counts = [len(self.tokenizer.tokenize(entry)) for entry in conversation_history]
        total_tokens = sum(conversation_history_token_counts) + new_prompt_token_count
    
        # Truncate conversation history if total tokens exceed max_tokens
        while total_tokens > max_tokens:
            conversation_history.pop(0)  # Remove the oldest entry
            conversation_history_token_counts.pop(0)  # Remove the oldest entry's token count
            total_tokens = sum(conversation_history_token_counts) + new_prompt_token_count
    
        return conversation_history  
