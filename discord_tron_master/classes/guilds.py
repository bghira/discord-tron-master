import json, logging, os
from pathlib import Path

DEFAULT_CONFIG = {
    "home_guild": None
}
DEFAULT_GUILD_CONFIG = {
}
class Guilds:
    def __init__(self):
        parent = os.path.dirname(Path(__file__).resolve().parent)
        self.project_root = parent
        config_path = os.path.join(parent, "config")
        self.config_path = os.path.join(config_path, "guilds.json")
        self.example_config_path = os.path.join(config_path, "example.json")
        self.reload_config()

    @staticmethod
    def merge_dicts(dict1, dict2):
        result = dict1.copy()
        for key, value in dict2.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = Guilds.merge_dicts(result[key], value)
            else:
                result[key] = value
        return result

    def reload_config(self):
        if not os.path.exists(self.config_path):
            with open(self.config_path, "w") as config_file:
                json.dump({}, config_file, indent=4)
        with open(self.config_path, "r") as config_file:
            self.config = json.load(config_file)
        self.config = self.merge_dicts(DEFAULT_CONFIG, self.config)

    def get_config_value(self, value):
        self.reload_config()
        return self.config.get(value, None)
    def set_config_value(self, key, value):
        self.reload_config()
        self.config[key] = value
        with open(self.config_path, "w") as config_file:
            logging.info(f"Saving config: {self.config}")
            json.dump(self.config, config_file, indent=4)
 
    def get_guild_config(self, guild_id = None):
        self.reload_config()
        guild_config = self.config.get("guilds", {})
        if guild_id is not None:
            guild_config = guild_config.get(str(guild_id), {})
            return self.merge_dicts(DEFAULT_GUILD_CONFIG, guild_config)
        return self.merge_dicts(DEFAULT_CONFIG, guild_config)

    def set_guild_config(self, guild_id, guild_config):
        current_config = self.get_guild_config()
        current_config[guild_id] = guild_config
        with open(self.config_path, "w") as config_file:
            logging.info(f"Saving config: {self.config}")
            json.dump(self.config, config_file, indent=4)

    def set_guild_setting(self, guild_id, setting_key, value):
        guild_id = str(guild_id)
        guild_config = self.get_guild_config(guild_id)
        guild_config[setting_key] = value
        self.set_guild_config(guild_id, guild_config)

    def get_guild_setting(self, guild_id, setting_key, default_value=None):
        self.reload_config()
        guild_id = str(guild_id)
        guild_config = self.get_guild_config(guild_id)
        return guild_config.get(setting_key, default_value)
    
    def get_guild_allowed_models(self, guild_id):
        return self.get_guild_setting(guild_id, "allowed_models", [])
    
    def set_guild_allowed_models(self, guild_id, allowed_models):
        return self.set_guild_setting(guild_id, "allowed_models", allowed_models)
    
    def set_guild_allowed_model(self, guild_id, model):
        allowed_models = self.get_guild_allowed_models(guild_id)
        allowed_models.append(model)
        return self.set_guild_allowed_models(guild_id, allowed_models)

    def is_guild_home_defined(self):
        if self.get_config_value('home_guild') is None:
            return False
        else:
            return True
    def is_guild_home(self, guild_id):
        if self.get_config_value('home_guild') is None or self.get_config_value('home_guild') != guild_id:
            return False
        if self.get_config_value('home_guild') == guild_id:
            return True
    def set_guild_home(self, guild_id):
        if self.is_guild_home_defined():
            raise Exception('Please do not try and mess with me, bb.')
        else:
            self.set_config_value('home_guild', guild_id)