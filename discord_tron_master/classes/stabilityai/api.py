import requests
from discord_tron_master.classes.app_config import AppConfig
config = AppConfig()
class StabilityAI:
    def __init__(self):
        self.api_key = config.get_stabilityai_api_key()
        self.base_url = "https://api.stability.ai/v2beta/stable-image/generate/sd3"
        # allowed resolutions: '21:9' | '16:9' | '3:2' | '5:4' | '1:1' | '4:5' | '2:3' | '9:16' | '9:21'
        self.allowed_resolutions = [
            '21:9', '16:9', '3:2', '5:4', '1:1', '4:5', '2:3', '9:16', '9:21'
        ]
        self.headers = {
            "authorization": f"Bearer {self.api_key}",
            "accept": "image/*"
        }
    
    def decimal_to_ratio(self, decimal: float):
        """Convert the decimal aspect_ratio representation to the nearest supported aspect."""
        # First, convert the allowed list from x:y to decimal:
        allowed_ratios = []
        ratio_dict = {}
        for ratio in self.allowed_resolutions:
            x, y = map(int, ratio.split(":"))
            decimal_ratio = x / y
            allowed_ratios.append(decimal_ratio)
            ratio_dict[decimal_ratio] = ratio  # Map decimal ratio back to string ratio
        import logging
        logging.info(f"Allowed aspect ratios: {allowed_ratios}")
        
        # Next, find the closest ratio to the decimal:
        closest_ratio = min(allowed_ratios, key=lambda x: abs(x - decimal))
        
        # Get the string representation from the dictionary:
        return ratio_dict[closest_ratio]

    def generate_image(self, prompt: str, user_config: dict, output_format: str = "png", model: str = "sd3"):
        res = user_config.get("resolution", {})
        width, height = res.get("width", 1024), res.get("height", 1024)
        aspect_ratio = self.decimal_to_ratio(round(width / height, 2))
        seed = user_config.get("seed", 0)
        arguments = {
            "prompt": f"{prompt} {user_config.get('positive_prompt', '')}",
            "output_format": output_format,
            "aspect_ratio": aspect_ratio,
            "model": model
        }
        if model != "sd3-turbo":
            arguments["negative_prompt"] = user_config.get("negative_prompt", "")
        if seed >= 0:
            arguments["seed"] = seed
        response = requests.post(
            self.base_url,
            headers=self.headers,
            files={"none": ''},
            data=arguments
        )
        if response.status_code == 200:
            return response.content
        else:
            raise Exception(str(response.json()))
