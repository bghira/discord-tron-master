from discord_tron_master.classes.database_handler import DatabaseHandler
from discord_tron_master.api import API
from discord_tron_master.bot import DiscordBot
from discord_tron_master.classes.app_config import AppConfig
from flask_migrate import Migrate
from flask import Flask

config = AppConfig()
api = API()
from discord_tron_master.auth import Auth
auth = Auth()
from discord_tron_master.websocket_hub import WebSocketHub
websocket_hub = WebSocketHub(auth_instance=auth)
discord_bot = DiscordBot(config.get_discord_api_key())


def main():
    api.run()
    websocket_hub.run()
    discord_bot.run()

from discord_tron_master.utils import generate_config_file

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
def delete_worker_user(username):
    from discord_tron_master.models.base import db
    from discord_tron_master.models.user import User
    # Check if a user with the same username or email already exists
    with api.app.app_context():
        existing_user = User.query.filter_by(username=username).first()
        db.session.delete(existing_user)
        db.session.commit()
def create_client_config():
    host = config.get_websocket_hub_url()
    port = config.get_websocket_hub_port()
    tls = config.get_websocket_hub_tls()
    refresh_token = auth.
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
    import argparse
    # Parse command-line arguments
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")

    create_client_config_parser = subparsers.add_parser("create_client_config")
    create_worker_user_parser = subparsers.add_parser("create_worker_user")
    create_worker_user_parser.add_argument("--username", required=True)
    create_worker_user_parser.add_argument("--password", required=True)
    create_worker_user_parser.add_argument("--email")
    delete_worker_user_parser = subparsers.add_parser("delete_worker_user")
    delete_worker_user_parser.add_argument("--username", required=True)

    args = parser.parse_args()

    # Call the appropriate function based on the command-line argument
    if args.command == "create_client_config":
        create_client_config()
    elif args.command == "create_worker_user":
        create_worker_user(args.username, args.password, args.email)
    elif args.command == "delete_worker_user":
        delete_worker_user(args.username)
    else:
        print("Unknown command:", args.command)