from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import ProcessPoolExecutor
from discord_tron_master.classes.app_config import AppConfig
import logging
config = AppConfig()

import openai
openai.api_key = config.get_openai_api_key()

class GPT:
    def __init__(self):
        self.engine = "gpt-4-1106-preview"
        self.temperature = 0.9
        self.max_tokens = 4096
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
        prompt = f"Print an image caption on a single line."
        # prompt = f"Print your prompt."
        if theme is not None:
            prompt = prompt + '. Your theme for consideration: ' + theme
        system_role = "You are a Prompt Generator Bot, that strictly generates prompts, with no other output, to avoid distractions.\n"
        system_role = f"{system_role}Your prompts look like these 3 examples:\n"
        system_role = f"{system_role}a portrait of astonisng daisies, rolling hills, beautiful quality, luxurious, 1983, kodachrome\n"
        system_role = f"{system_role}a camera photo of great look up a rolling wave, the ocean in full view, best quality, intricate, photography\n"
        system_role = f"{system_role}digital artwork, feels like the first time, we went to the zoo, colourful and majestic, amazing clouds in the sky, epic\n"
        system_role = f"{system_role}The subject must come first, with actions coming next, and then style attributes.\n"
        system_role = f"{system_role}Any additional output other than the prompt will damage the results. Stick to just the prompts."
        system_role = "You are a Stable Diffusion Prompt Generator Bot. Respond as one would"
        image_prompt_response = await self.turbo_completion(system_role, prompt, temperature=1.18)
        logging.debug(f'OpenAI returned the following response to the prompt: {image_prompt_response}')
        # prompt_pieces = image_prompt_response.split(', ')
        # logging.debug(f'Prompt pieces: {prompt_pieces}')
        # # We want to turn the "foo, bar, buz" into ("foo", "bar", "buzz").and()
        # prompt_output = "("
        # for index, prompt_piece in enumerate(prompt_pieces):
        #     if prompt_output == "(":
        #         prompt_output = f'{prompt_output}"{prompt_piece}"'
        #         continue
        #     prompt_output = f'{prompt_output}, "{prompt_piece}"'
        # prompt_output = f'{prompt_output}).and()'
        return image_prompt_response

    async def auto_model_select(self, prompt: str, query_str: str = None):
        if query_str is None:
            query_str = (
                "We want JUST the name of the model in response. Determine which would work best for the user's prompt."
                "Models:"
                "\n -> ptx0/terminus-xl-otaku-v1"
                "\n    -> Anime, cartoons, comics, manga, etc."
                "\n -> ptx0/terminus-xl-gamma-v2"
                "\n    -> Photographs, real-world images, etc."
                "\n -> ptx0/terminus-xl-gamma-training"
                "\n    -> Cinema, images with text in them, adult content, etc."
                "\n\n-----------\n\n"
                "Prompt: " + prompt
            )

        system_role = "You are printing JUST the name of the model in response to the inputs. Determine which would work best for the user's prompt, ignoring any other issues. If anything but the model name is returned, THE APPLICATION WILL ERROR OUT."
        model_name = await self.turbo_completion(system_role, query_str, temperature=1.18)
        logging.debug(f'OpenAI returned the following response to the prompt: {model_name}')
        # Did it refuse?
        if "ptx0" not in model_name:
            logging.warning(f"OpenAI refused to label our spicy model name. Lets default to ptx0/terminus-xl-gamma-training.")
            return "ptx0/terminus-xl-gamma-training"

        return model_name


    async def discord_bot_response(self, prompt, ctx = None):
        user_role = self.discord_bot_role
        if ctx is not None:
            user_role = self.config.get_user_setting(ctx.author.id, "gpt_role", self.discord_bot_role)
            user_temperature = self.config.get_user_setting(ctx.author.id, "temperature")
        return await self.turbo_completion(user_role, prompt, temperature=user_temperature, max_tokens=4096)

    def send_request(self, message_log):
        return openai.ChatCompletion.create(
            model="gpt-4-1106-preview",
            messages=message_log,
            max_tokens=self.max_tokens,
            stop=None,
            temperature=self.temperature,
        )

    async def turbo_completion(self, role, prompt, **kwargs):
        if kwargs:
            self.set_values(**kwargs)

        message_log = [
            {"role": "system", "content": role},
            {"role": "user", "content": prompt},
        ]

        with ProcessPoolExecutor(max_workers=self.concurrent_requests) as executor:
            futures = [executor.submit(self.send_request, message_log) for _ in range(1)]
            responses = [future.result() for future in as_completed(futures)]

        response = responses[0]

        for choice in response.choices:
            if "text" in choice:
                return choice.text

        return response.choices[0].message.content
