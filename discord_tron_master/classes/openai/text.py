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

    async def sentiment_analysis(self, prompts):
        prompt = f"As a playful exercise, analyse the user who provided the following text: {prompts}"
        system_role = "You are a sentiment analysis bot. Provide ONLY up to two paragraphs explaining the averages. Do not use run-on sentences or make a wall of text. Do not explain what a sentiment analysis is. Just provide the paragraph. You can use Discord formatting or average percent values to describe trends, but keep it succinct."
        return await self.turbo_completion(system_role, prompt, temperature=1.18)

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
                "\nModels:"
                "\n -> ptx0/terminus-xl-otaku-v1"
                "\n    -> Anime, cartoons, comics, manga, ghibli, watercolour."
                "\n -> ptx0/terminus-xl-gamma-v2"
                "\n    -> Requests for 'high quality' images go here, but it has some high frequency noise issues."
                "\n -> ptx0/terminus-xl-gamma-training"
                "\n    -> This was an attempt to resolve some issues in the v2 model, but the issues persist. It noticeably improves on some concepts, and the high freq noise issue appears less often than v2. This model might have the strongest ability to produce readable text."
                "\n -> ptx0/terminus-xl-gamma-v2-1"
                "\n    -> Cinema, photographs, most images with text in them, adult content, etc. This is the default model, but if the request contains 'high quality', it should use gamma-v2 or training instead."
                "\n\n-----------\n\n"
                "Resolutions: "
                "\n| Square        | Landscape    | Portrait     |"
                "\n+---------------+--------------+--------------+"
                "\n|               | 1024x960     | 960x1088     | "
                "\n| 1024x1024     | 1088x896     | 960x1024     | "
                "\n|               | 1088x960     | 896x1152     | "
                "\n|               | 1152x832     | 704x1472     | "
                "\n|               | 1152x896     | 768x1280     | "
                "\n|               | 1216x832     | 768x1344     | "
                "\n|               | 1280x768     | 832x1152     | "
                "\n|               | 1344x704     | 832x1216     | "
                "\n|               | 1344x768     | 896x1088     | "
                "\n\n-----------\n\n"
                "Output format:\n"
                '{"model": <selected model>, "resolution": <selected resolution>}'
                "\n\n-----------\n\n"
                "Objective: Determine from the user prompt which model to use. The content can be better if an appropriate resolution/aspect are chosen - eg portraits are taller, pictures of book covers may be too, but try not to use extreme aspects unless the prompt demands it.."
                "\n\n-----------\n\n"
                "Analyze Prompt: " + prompt
            )

        system_role = "Print ONLY the specified JSON document. Determine which model and resolution would work best for the user's prompt, ignoring any other issues. If anything but the JSON object and the defined keys are returned, THE APPLICATION WILL ERROR OUT."
        prediction = await self.turbo_completion(system_role, query_str, temperature=1.18)
        import json
        try:
            result = json.loads(prediction)
            model_name = result["model"]
            raw_resolution = result["resolution"]
            width, heidht = raw_resolution.split("x")
            resolution = {
                "width": int(width),
                "height": int(heidht)
            }
        except Exception as e:
            logging.error(f"Error parsing JSON from prediction: {prediction}")
            return ("1280x768", "ptx0/terminus-xl-gamma-training")
        logging.debug(f'OpenAI returned the following response to the prompt: {model_name}')
        # Did it refuse?
        if "ptx0" not in model_name:
            logging.warning(f"OpenAI refused to label our spicy model name. Lets default to ptx0/terminus-xl-gamma-training.")
            return ("1280x768", "ptx0/terminus-xl-gamma-training")

        return (resolution, model_name)


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
