import json
import os
from pathlib import Path

DEFAULT_CONFIG = {
    "concurrent_slots": 1,
    "cmd_prefix": "+",
    "websocket_hub": {
        "host": "localhost",
        "port": 6789,
        "tls": False,
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
    "model": "theintuitiveye/HARDblend",
    "negative_prompt": "(child, teen) (malformed, malignant)",
    "steps": 100,
    "positive_prompt": "(beautiful, unreal engine 5, highly detailed, hyperrealistic)",
    "resolution": {
        "width": 512,
        "height": 768
    },
}

class AppConfig:
    def __init__(self):
        parent = os.path.dirname(Path(__file__).resolve().parent)
        config_path = os.path.join(parent, "config")
        self.config_path = os.path.join(config_path, "config.json")
        self.example_config_path = os.path.join(config_path, "example.json")

        if not os.path.exists(self.config_path):
            with open(self.example_config_path, "r") as example_file:
                example_config = json.load(example_file)

            with open(self.config_path, "w") as config_file:
                json.dump(example_config, config_file)

        with open(self.config_path, "r") as config_file:
            self.config = json.load(config_file)

        self.config = self.merge_dicts(DEFAULT_CONFIG, self.config)

    @staticmethod
    def merge_dicts(dict1, dict2):
        result = dict1.copy()
        for key, value in dict2.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = AppConfig.merge_dicts(result[key], value)
            else:
                result[key] = value
        return result

    def get_user_config(self, user_id):
        user_config = self.config.get("users", {}).get(str(user_id), {})
        return self.merge_dicts(DEFAULT_USER_CONFIG, user_config)

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
        return self.config.get("concurrent_slots", 1)

    def get_command_prefix(self):
        return self.config.get("cmd_prefix", "+")

    def get_websocket_hub_host(self):
        return self.config.get("websocket_hub", {}).get("host", "localhost")

    def get_websocket_hub_port(self):
        return self.config.get("websocket_hub", {}).get("port", 6789)

    def get_websocket_hub_tls(self):
        return self.config.get("websocket_hub", {}).get("tls", False)

    def get_huggingface_api_key(self):
        return self.config["huggingface_api"].get("api_key", None)
    def get_discord_api_key(self):
        return self.config.get("discord", {}).get("api_key", None)
    def get_local_model_path(self):
        return self.config["huggingface"].get("local_model_path", None)

    def set_user_config(self, user_id, user_config):
        self.config.get("users", {})[str(user_id)] = user_config
        with open(self.config_path, "w") as config_file:
            json.dump(self.config, config_file)

    def set_user_setting(self, user_id, setting_key, value):
        user_id = str(user_id)
        self.config.get("users", {}).get(user_id, {})[setting_key] = value
        with open(self.config_path, "w") as config_file:
            json.dump(self.config, config_file)

    def get_user_setting(self, user_id, setting_key, default_value=None):
        user_id = str(user_id)
        return self.config.get("users", {}).get(user_id, {}).get(setting_key, default_value)

    def get_mysql_user(self):
        return self.config.get("mysql", {}).get("user", "diffusion")
    def get_mysql_password(self):
        return self.config.get("mysql", {}).get("password", "diffusion_pwd")
    def get_mysql_hostname(self):
        return self.config.get("mysql", {}).get("hostname", "localhost")
    def get_mysql_dbname(self):
        return self.config.get("mysql", {}).get("dbname", "diffusion_master")