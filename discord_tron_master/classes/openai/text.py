from concurrent.futures import ThreadPoolExecutor, as_completed
from discord_tron_master.classes.app_config import AppConfig
import logging
config = AppConfig()

import openai
openai.api_key = config.get_openai_api_key()

class GPT:
    def __init__(self):
        self.engine = "gpt-3.5-turbo-0613"
        self.temperature = 0.9
        self.max_tokens = 100
        self.discord_bot_role = "You are a Discord bot."
        self.concurrent_requests = config.get_concurrent_openai_requests()
        self.config = AppConfig()
    
    def set_values(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    async def updated_setting_response(self, name, value):
        prompt = f"Please provide a message to the user. They have updated setting '{name}' to be set to '{value}'"
        return await self.turbo_completion(self.discord_bot_role, prompt)

    async def compliment_user_selection(self):
        role = f"You are Joe Rogan! Respond as he would."
        prompt = f"Return just a compliment on a decision I've made. Maybe you can ask Jamie to pull a clip up about the image that's about to be generated. Short and sweet output only."
        return await self.turbo_completion(role, prompt, max_tokens=50, temperature=1.05, engine="text-davinci-003")

    async def insult_user_selection(self):
        role = "You are Joe Rogan! We tease each other in non-offensive ways. We are friends. Keep it short and sweet."
        prompt = f"Return just a playful, short and sweet tease me about a decision I've made, in the style of Joe Rogan."
        return await self.turbo_completion(role, prompt, temperature=1.05, max_tokens=50, engine="text-davinci-003")

    async def insult_or_compliment_random(self):
        # Roll a dice and select whether we insult or compliment, and then, return that:
        import random
        random_number = random.randint(1, 2)
        if random_number == 1:
            return await self.insult_user_selection()
        else:
            return await self.compliment_user_selection()

    async def random_image_prompt(self, theme: str = None):
        prompt = f"Print ONLY comma-separated descriptive keywords for an imagined image, without any other text."
        if theme is not None:
            prompt = prompt + '. Your theme: ' + theme
        image_prompt_response = await self.turbo_completion("You are a Prompt Generator Bot. Respond as one would.", prompt, temperature=1.18)
        logging.debug(f'OpenAI returned the following response to the prompt: {image_prompt_response}')
        prompt_pieces = image_prompt_response.split(', ')
        logging.debug(f'Prompt pieces: {prompt_pieces}')
        # We want to turn the "foo, bar, buz" into ("foo", "bar", "buzz").and()
        prompt_output = "("
        for index, prompt_piece in enumerate(prompt_pieces):
            if prompt_output == "(":
                prompt_output = f'"{prompt_piece}"'
                continue
            prompt_output = f'{prompt_output}, "{prompt_piece}"'
        prompt_output = f'{prompt_output}).and()'
        return prompt_output

    async def discord_bot_response(self, prompt, ctx = None):
        user_role = self.discord_bot_role
        if ctx is not None:
            user_role = self.config.get_user_setting(ctx.author.id, "gpt_role", self.discord_bot_role)
            user_temperature = self.config.get_user_setting(ctx.author.id, "temperature")
        return await self.turbo_completion(user_role, prompt, temperature=user_temperature, max_tokens=2048)

    async def turbo_completion(self, role, prompt, **kwargs):
        if kwargs:
            self.set_values(**kwargs)
            
        message_log = [
            {"role": "system", "content": role},
            {"role": "user", "content": prompt},
        ]

        def send_request():
            return openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=message_log,
                max_tokens=self.max_tokens,
                stop=None,
                temperature=self.temperature,
            )

        with ThreadPoolExecutor(max_workers=self.concurrent_requests) as executor:
            futures = [executor.submit(send_request) for _ in range(1)]
            responses = [future.result() for future in as_completed(futures)]

        response = responses[0]

        for choice in response.choices:
            if "text" in choice:
                return choice.text

        return response.choices[0].message.content
