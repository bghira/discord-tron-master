print(f"Loading main file..")
import logging, threading
from discord_tron_master.classes import log_format

from discord_tron_master.classes.database_handler import DatabaseHandler
from discord_tron_master.api import API
from discord_tron_master.classes.app_config import AppConfig
from flask_migrate import Migrate
from flask import Flask

from discord_tron_master.classes.worker_manager import WorkerManager
from discord_tron_master.classes.queue_manager import QueueManager
from discord_tron_master.classes.command_processor import CommandProcessor

config = AppConfig()

api = API()
from discord_tron_master.auth import Auth
auth = Auth()

# Provide references to each instance.
auth.set_app(api.app)
api.set_auth(auth)
import discord
from discord_tron_master.websocket_hub import WebSocketHub
from discord_tron_master.bot import DiscordBot
# Initialize the worker manager and add a few workers
worker_manager = WorkerManager()
# Initialize the Queue manager
queue_manager = QueueManager(worker_manager)
worker_manager.set_queue_manager(queue_manager)

# Begin to configure the Discord bot frontend.
intents = discord.Intents.default()
intents.typing = False
intents.presences = False
intents.message_content = True
PREFIX="!"
discord_bot = DiscordBot(token=config.get_discord_api_key())

# Initialize the command processor
command_processor = CommandProcessor(queue_manager, worker_manager, discord_bot)
# Now, the WebSocket Hub.
websocket_hub = WebSocketHub(auth_instance=auth, command_processor=command_processor, discord_bot=discord_bot)


import asyncio, concurrent
from concurrent.futures import ThreadPoolExecutor
asyncio.run(websocket_hub.set_queue_manager(queue_manager))
asyncio.run(websocket_hub.set_worker_manager(worker_manager))

asyncio.run(discord_bot.set_queue_manager(queue_manager))
asyncio.run(discord_bot.set_worker_manager(worker_manager))
asyncio.run(discord_bot.set_websocket_hub(websocket_hub))


def main():
    def run_websocket_hub():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(websocket_hub.run())

    def run_discord_bot():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(discord_bot.run())

    with ThreadPoolExecutor(max_workers=1) as executor:
        tasks = [
            executor.submit(run_discord_bot),
            # executor.submit(run_websocket_hub),
        ]

        for future in concurrent.futures.as_completed(tasks):
            try:
                future.result()
            except Exception as e:
                logging.error("An error occurred: %s", e)

# A simple wrapper to run Flask in a thread.
def run_flask_api():
    logging.info("Startup! Begin API.")
    api.run()

def create_worker_user(username: str, password: str, email = None):
    from discord_tron_master.models.base import db
    from discord_tron_master.models.user import User
    # Check if a user with the same username or email already exists
    with api.app.app_context():
        existing_user = User.query.filter_by(username=username).first()
        if existing_user is not None:
            return existing_user
        existing_user = User.query.filter_by(email=email).first()
        if existing_user is not None:
            return existing_user

        # Create a new user record if one did not already exist.
        if email is None:
            email=username + "@example.com"
        import bcrypt
        # Hash the password using bcrypt
        hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
        new_user = User(username=username, password=hashed_password, email=email)
        db.session.add(new_user)
        db.session.commit()
def delete_worker_user(username: str):
    from discord_tron_master.models.base import db
    from discord_tron_master.models.user import User
    # Check if a user with the same username or email already exists
    with api.app.app_context():
        existing_user = User.query.filter_by(username=username).first()
        db.session.delete(existing_user)
        db.session.commit()

from discord_tron_master.utils import generate_config_file
def create_client_tokens(username: str):
    from discord_tron_master.models.user import User
    from discord_tron_master.models.oauth_token import OAuthToken
    from discord_tron_master.models.api_key import ApiKey

    with api.app.app_context():
        import json
        # Do we have a user at all?
        existing_user = User.query.filter_by(username=username).first()
        if existing_user is None:
            logging.info(f"User {username} does not exist")
            return
        # Does it have a client?
        client = existing_user.has_client()
        if not client:
            logging.info(f"Client does not exist for user {existing_user.username} - we will try to create one.")
            client = existing_user.create_client()
        else:
            logging.info(f"User already had an OAuth Client registered. Using that: {client}")
        # Did we deploy them an API Key?
        logging.info("Checking for API Key...")
        api_key = ApiKey.query.filter_by(client_id=client.client_id, user_id=client.user_id).first()
        if api_key is None:
            logging.info("No API Key found, generating one...")
            api_key = ApiKey.generate_by_user_id(existing_user.id)
        logging.info(f"API key for client/user:\n" + json.dumps(api_key.to_dict(), indent=4))
        # Do we have tokens for this user?
        logging.info("Checking for existing tokens...")
        existing_tokens = OAuthToken.query.filter_by(user_id=existing_user.id).first()
        if existing_tokens is not None:
            logging.info(f"Tokens already exist for user {username}:\n" + json.dumps(existing_tokens.to_dict(), indent=4))
            return existing_tokens
    # It seems like we can proceed.
    logging.info(f"Creating tokens for user {username}")
    host = config.get_websocket_hub_host()
    port = config.get_websocket_hub_port()
    tls = config.get_websocket_hub_tls()
    with api.app.app_context():
        refresh_token = auth.create_refresh_token(client.client_id, existing_user.id)
    protocol = "ws"
    if tls:
        protocol = "wss"
    config_data = {
        "hub_url": f"{protocol}://{host}:{port}",
        "refresh_token": refresh_token
        # "server_cert_path": "path/to/server_cert.pem"
    }
    generate_config_file("client_config.json", config_data)


if __name__ == "__main__":
    try:
        import argparse
        # Parse command-line arguments
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")

        run_parser = subparsers.add_parser("run")
        create_client_tokens_parser = subparsers.add_parser("create_client_tokens")
        create_client_tokens_parser.add_argument("--username", required=True)
        create_worker_user_parser = subparsers.add_parser("create_worker_user")
        create_worker_user_parser.add_argument("--username", required=True)
        create_worker_user_parser.add_argument("--password", required=True)
        create_worker_user_parser.add_argument("--email")
        delete_worker_user_parser = subparsers.add_parser("delete_worker_user")
        delete_worker_user_parser.add_argument("--username", required=True)

        args = parser.parse_args()

        # Call the appropriate function based on the command-line argument
        if args.command == "create_client_tokens":
            create_client_tokens(args.username)
        elif args.command == "create_worker_user":
            create_worker_user(args.username, args.password, args.email)
        elif args.command == "delete_worker_user":
            delete_worker_user(args.username)
        elif args.command == "run":
            main()
    except KeyboardInterrupt:
        logging.info("Shutting down...")
        exit(0)
    except Exception as e:
        import traceback
        logging.error(f"Stack trace: {traceback.format_exc()}")
        exit(1)
