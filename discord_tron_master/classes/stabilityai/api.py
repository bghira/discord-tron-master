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
    def generate_image(self, prompt: str, user_config: dict, output_format: str = "png", model: str ="sd3"):
        res = user_config.get("resolution", {})
        width, height = res.get("width", 1024), res.get("height", 1024)
        aspect_ratio = round(width / height, 2)
        seed = user_config.get("seed", 0)
        response = requests.post(
            self.base_url,
            headers=self.headers,
            files={"none": ''},
            data={
                "prompt": f"{prompt} {user_config.get('positive_prompt', '')}",
                "negative_prompt": user_config.get("negative_prompt", ""),
                "output_format": output_format,
                "seed": seed,
                "aspect_ratio": aspect_ratio,
                "model": model
            },
        )
        if response.status_code == 200:
            return response.content
        else:
            raise Exception(str(response.json()))
