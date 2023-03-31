from discord_tron_master.auth import Auth
from discord_tron_master.api import API
from discord_tron_master.websocket_hub import WebSocketHub
from discord_tron_master.bot import DiscordBot
from discord_tron_master.classes.app_config import AppConfig
def main():
    auth = Auth()
    config = AppConfig()
    api = API(auth)
    websocket_hub = WebSocketHub(auth)
    discord_bot = DiscordBot(config.get_discord_api_key())

    api.run()
    websocket_hub.run()
    discord_bot.run()

if __name__ == "__main__":
    main()
