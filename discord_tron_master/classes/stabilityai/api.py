import requests
from discord_tron_master.classes.app_config import AppConfig
config = AppConfig()
class StabilityAI:
    def __init__(self):
        self.api_key = config.get_stabilityai_api_key()
        self.base_url = "https://api.stability.ai/v2beta/stable-image/generate/sd3"
        self.headers = {
            "authorization": f"Bearer {self.api_key}",
            "accept": "image/*"
        }
    def generate_image(self, prompt: str, output_format: str = "png"):
        response = requests.post(
            self.base_url,
            headers=self.headers,
            files={"none": ''},
            data={
                "prompt": prompt,
                "output_format": output_format,
            },
        )
        if response.status_code == 200:
            return response.content
        else:
            raise Exception(str(response.json()))
