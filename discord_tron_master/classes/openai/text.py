from discord_tron_master.classes.app_config import AppConfig
config = AppConfig()
import openai
openai.api_key = config.get_openai_api_key()

class GPT:
    def __init__(self):
        self.engine = "chatgpt-3.5-turbo"
        self.temperature = 0.9
        self.max_tokens = 100
        self.discord_bot_role = "You are a Discord bot."
    
    def set_values(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    def updated_setting_response(self, name, value):
        prompt = f"Please provide a message to the user. They have updated setting '{name}' to be set to '{value}'"
        return self.turbo_completion(self.discord_bot_role, prompt)

    def compliment_user_selection(self, author):
        prompt = f"Please compliment the user '{author}', in a random style, on their image generation selection."
        return self.turbo_completion(self.discord_bot_role, prompt)

    def insult_user_selection(self, author):
        prompt = f"Please insult the user '{author}', in a random style, on their image generation selection."
        return self.turbo_completion(self.discord_bot_role, prompt)

    def random_image_prompt(self):
        prompt = f"Print ONLY a random image generation prompt for Stable Diffusion."
        return self.turbo_completion(self.discord_bot_role, prompt)

    def turbo_completion(self, role, prompt, **kwargs):
        if kwargs:
            self.set_values(**kwargs)
        message_log = [
            { "role": "system", "content": role },
            { "role": "user", "content": prompt }
        ]
        response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",  # The name of the OpenAI chatbot model to use
                messages=message_log,   # The conversation history up to this point, as a list of dictionaries
                max_tokens=self.max_tokens,        # The maximum number of tokens (words or subwords) in the generated response
                stop=None,              # The stopping sequence for the generated response, if any (not used here)
                temperature=self.temperature,        # The "creativity" of the generated response (higher temperature = more creative)
            )
        # Find the first response from the chatbot that has text in it (some responses may not have text)
        for choice in response.choices:
            if "text" in choice:
                return choice.text

        # If no response with text is found, return the first response's content (which may be empty)
        return response.choices[0].message.content
