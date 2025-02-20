import json, logging, os
from pathlib import Path

DEFAULT_CONFIG = {
    "concurrent_slots": 1,
    "cmd_prefix": "+",
    "websocket_hub": {
        "host": "localhost",
        "port": 6789,
        "tls": False,
    },
    "openai_api": {
        "api_key": None
    },
    "huggingface_api": {
        "api_key": None,
    },
    "discord": {
        "api_key": None,
    },
    "huggingface": {
        "local_model_path": None,
    },
    "users": {},
    "mysql": {
        "user": "diffusion",
        "password": "diffusion_pwd",
        "hostname": "localhost",
        "dbname": "diffusion_master",
    },
}

DEFAULT_USER_CONFIG = {
    "seed": -1,
    "scheduler": "fast",
    "enable_deepcache": True,
    "deepcache_branch_id": 0,
    "deepcache_interval": 3,
    "enable_teacache": True,
    "teacache_distance": 0.6,
    "enable_sageattn": True,
    "steps": 25,
    "gpt_role": "You are a Discord bot.",
    "tts_voice": "en_fiery",
    "df_guidance_scale_1": 9.2,
    "df_guidance_scale_2": 5.7,
    "df_guidance_scale_3": 5.6,
    "df_x4_upscaler": False,
    "df_latent_refiner": False,
    "df_controlnet_upscaler": True,
    "df_controlnet_strength": 1.0,
    "df_esrgan_upscaler": False,
    "df_stage3_strength": 1.0,
    "refiner_guidance": 7.5,
    "refiner_guidance_rescale": 0.7,
    "refiner_strength": 0.3,
    "aesthetic_score": 5.0,
    "negative_aesthetic_score": 1.0,
    "refiner_steps": 10,
    "refiner_prompt_weighting": False,
    "first_inference_step": None,
    "max_inference_steps": None,
    "temperature": 0.9,
    "repeat_penalty": 1.1,
    "top_p": 0.95,
    "top_k": 40,
    "max_tokens": 1024,
    "strength": 0.3,
    "resize": 1,
    "guidance_scaling": 6,
    "guidance_rescale": 0.7,
    "negative_prompt": "washed-out low-contrast (deep fried) watermark, cropped, out-of-frame, low quality, low res, poorly drawn, bad anatomy, wrong anatomy, extra limb, missing limb, floating limbs, (mutated hands and fingers:1.4), disconnected limbs, mutation, mutated, ugly, disgusting, blurry, amputation",
    "positive_prompt": "",
    "tile_negative": "blur, lowres, bad anatomy, bad hands, cropped, worst quality",
    "tile_positive": "best quality",
    "tile_strength": 0.0,
    "hires_fix": False,
    "latent_refiner": False,
    "auto_model": True,
    "resolution": {
        "width": 1024,
        "height": 1024
    },
    "model_adapter_1": None,
    "flux_adapter_1": "",
    "flux_guidance_scale": 3.0,
    "skip_guidance_layers": -1,
}

class AppConfig:
    flask = None
    def __init__(self):
        parent = os.path.dirname(Path(__file__).resolve().parent)
        self.project_root = parent
        config_path = os.path.join(parent, "config")
        self.config_path = os.path.join(config_path, "config.json")
        self.example_config_path = os.path.join(config_path, "example.json")
        self.reload_config()

    @classmethod
    def set_flask(cls, flask):
        cls.flask = flask

    @classmethod
    def get_flask(cls):
        return cls.flask

    def reload_config(self):
        if not os.path.exists(self.config_path):
            with open(self.example_config_path, "r") as example_file:
                example_config = json.load(example_file)
            with open(self.config_path, "w") as config_file:
                json.dump(example_config, config_file, indent=4)
        with open(self.config_path, "r") as config_file:
            self.config = json.load(config_file)
        self.config = self.merge_dicts(DEFAULT_CONFIG, self.config)

    def get_log_level(self):
        self.reload_config()
        level = self.config.get("log_level", "INFO")
        result = getattr(logging, level.upper(), "ERROR")
        return result

    def get_user_config(self, user_id):
        self.reload_config()
        user_config = self.config.get("users", {}).get(str(user_id), {})
        merged_settings = self.merge_dicts(DEFAULT_USER_CONFIG, user_config)
        if "model" not in merged_settings:
            merged_settings["model"] = self.config.get("default_diffusion_model", "ptx0/terminus-xl-gamma-v2")
        return merged_settings

    def should_compare(self):
        self.reload_config()
        return self.config.get("compare_images", False)

    @staticmethod
    def merge_dicts(dict1, dict2):
        result = dict1.copy()
        for key, value in dict2.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = AppConfig.merge_dicts(result[key], value)
            else:
                result[key] = value
        return result

    def get_concurrent_slots(self):
        self.reload_config()
        return self.config.get("concurrent_slots", 1)

    def get_command_prefix(self):
        self.reload_config()
        return self.config.get("cmd_prefix")

    def get_concurrent_openai_requests(self):
        self.reload_config()
        return self.config.get("concurrent_openai_requests", 15)

    def get_openai_api_key(self):
        self.reload_config()
        return self.config["openai_api"].get("api_key", None)

    def get_websocket_hub_host(self):
        self.reload_config()
        return self.config.get("websocket_hub", {}).get("host", "localhost")

    def get_websocket_hub_port(self):
        self.reload_config()
        return self.config.get("websocket_hub", {}).get("port", 6789)

    def get_websocket_hub_tls(self):
        self.reload_config()
        return self.config.get("websocket_hub", {}).get("tls", False)

    def get_huggingface_api_key(self):
        self.reload_config()
        return self.config["huggingface_api"].get("api_key", None)

    def get_discord_api_key(self):
        self.reload_config()
        return self.config.get("discord", {}).get("api_key", None)

    def get_stabilityai_api_key(self):
        self.reload_config()
        return self.config.get("stabilityai", {}).get("api_key", None)

    def get_local_model_path(self):
        self.reload_config()
        return self.config.get("huggingface", {}).get("local_model_path", "/root/.cache/huggingface/hub")

    def set_user_config(self, user_id, user_config):
        self.config.get("users", {})[str(user_id)] = user_config
        with open(self.config_path, "w") as config_file:
            logging.info(f"Saving config: {self.config}")
            json.dump(self.config, config_file, indent=4)

    def set_user_setting(self, user_id, setting_key, value):
        user_id = str(user_id)
        user_config = self.get_user_config(user_id)
        user_config[setting_key] = value
        self.set_user_config(user_id, user_config)

    def get_user_setting(self, user_id, setting_key, default_value=None):
        self.reload_config()
        user_id = str(user_id)
        user_config = self.get_user_config(user_id)
        return user_config.get(setting_key, default_value)

    def get_web_root(self):
        self.reload_config()
        return self.config.get("web_root", "/")
    def get_url_base(self):
        self.reload_config()
        return self.config.get("url_base", "http://localhost")
    def get_mysql_user(self):
        self.reload_config()
        return self.config.get("mysql", {}).get("user", "diffusion")
    def get_mysql_password(self):
        self.reload_config()
        return self.config.get("mysql", {}).get("password", "diffusion_pwd")
    def get_mysql_hostname(self):
        self.reload_config()
        return self.config.get("mysql", {}).get("hostname", "localhost")
    def get_mysql_dbname(self):
        self.reload_config()
        return self.config.get("mysql", {}).get("dbname", "diffusion_master")