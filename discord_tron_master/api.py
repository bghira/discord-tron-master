import logging

from flask import ( Flask, request, jsonify )
from flask_restful import Api, Resource
from discord_tron_master.classes.database_handler import DatabaseHandler
from discord_tron_master.classes.command_processors import discord as DiscordCommandProcessor
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from discord_tron_master.classes.app_config import AppConfig
from PIL import Image, UnidentifiedImageError, PngImagePlugin
from io import BytesIO
import io, asyncio
from scipy.io.wavfile import read as read_wav
from scipy.io.wavfile import write as write_wav
import base64

class API:
    def __init__(self):
        logging.debug("Loaded Flask API")
        self.config = AppConfig()
        self.app = Flask(__name__)
        AppConfig.set_flask(self.app)
        database_handler = DatabaseHandler(self.app, self.config)
        self.db = database_handler.db
        from discord_tron_master.models.conversation import Conversations
        from discord_tron_master.models.transformers import Transformers
        from discord_tron_master.models.schedulers import Schedulers
        self.migrate = Migrate(self.app, self.db)
        self.register_routes()
        self.auth = None

    def add_resource(self, resource, route):
        self.api.add_resource(resource, route)

    def run(self, host='0.0.0.0', port=5000):
        import ssl
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(self.config.project_root + '/config/server_cert.pem', self.config.project_root + '/config/server_key.pem')
        # Set the correct SSL/TLS version (You can change PROTOCOL_TLS to the appropriate version if needed)
        ssl_context.options |= ssl.OP_NO_SSLv2 | ssl.OP_NO_SSLv3 | ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
        self.app.run(host=host, port=port, ssl_context=ssl_context)

    def set_auth(self, auth):
        self.auth = auth

    def register_routes(self):
        # assuming you have 'app' defined as your Flask instance
        @self.app.route("/refresh_token", methods=["POST"])
        def refresh_token():
            logging.debug("refresh_token endpoint hit")
            refresh_token = request.json.get("refresh_token")
            if not refresh_token:
                return jsonify({"error": "refresh_token is required"}), 400
            from discord_tron_master.models import OAuthToken
            token_data = OAuthToken.query.filter_by(refresh_token=refresh_token).first()
            if not token_data:
                return jsonify({"error": "Invalid refresh token"}), 400
            logging.debug(f"Refreshed access token requested from {token_data.client_id}")
            # Logic to refresh the access token using the provided refresh_token
            new_ticket = self.auth.refresh_access_token(token_data)
            response = new_ticket.to_dict()

            return jsonify(response)
        @self.app.route("/authorize", methods=["POST"])
        def authorize():
            logging.debug("authorize endpoint hit")
            client_id = request.json.get("client_id")
            api_key = request.json.get("api_key")
            logging.debug(f"Received {client_id} client_id and {api_key} api_key")
            if not all([client_id, api_key]):
                return jsonify({"error": "client_id and api_key are required"}), 400

            from discord_tron_master.models import OAuthClient
            client = OAuthClient.query.filter_by(client_id=client_id).first()
            if not client:
                return jsonify({"error": "Invalid client_id"}), 400

            from discord_tron_master.models import ApiKey
            api_key_data = ApiKey.query.filter_by(api_key=api_key).first()
            if not api_key_data:
                return jsonify({"error": "Invalid api_key", "api_key": api_key}), 401

            user_id = api_key_data.user_id
            from discord_tron_master.models import OAuthToken
            token_data = OAuthToken.query.filter_by(client_id=client_id, user_id=user_id).first()
            if not token_data:
                logging.debug(f"Could not find token_data for client_id {client_id} and user_id {user_id}")
                return jsonify({"error": "No token data found"}), 401
            new_token = self.auth.refresh_access_token(token_data)

            return jsonify({"access_token": new_token.to_dict()})

        @self.app.route("/upload_image", methods=["POST"])
        def upload_image():
            logging.debug(f"upload_image endpoint hit with params: {request.args}")
            image_metadata = {}
            if "user_config" in request.args:
                import json
                image_metadata["user_config"] = json.loads(request.args["user_config"])
            if "prompt" in request.args:
                image_metadata["prompt"] = request.args["prompt"].replace("\\n", "\n")
            if not self.check_auth(request):
                return jsonify({"error": "Authentication required"}), 401
            image = request.files.get("image")
            if not image:
                return jsonify({"error": "image is required"}), 400

            # Read the image and convert it to a base64-encoded string
            try:
                img = Image.open(image.stream)
                logging.debug(f'Image metadata: {image_metadata}')
                # Create pnginfo:
                pnginfo = PngImagePlugin.PngInfo()
                for key, value in image_metadata.items():
                    pnginfo.add_text(key, json.dumps(value), 0)
            except UnidentifiedImageError as e:
                logging.debug(f'Malformed image was supplied: {image.stream}')
                logging.error(f"Could not open image: {e}")
                return jsonify({"error": "Malformed image was supplied", "error_class": "UnidentifiedImageError"}), 400
            buffered = BytesIO()
            img.save(buffered, format="PNG")
            base64_encoded_image = base64.b64encode(buffered.getvalue()).decode('utf-8')
            image_url = asyncio.run(DiscordCommandProcessor.get_image_embed(base64_encoded_image, pnginfo, create_embed=False))
            return jsonify({"image_url": image_url.strip()})

        @self.app.route("/upload_audio", methods=["POST"])
        def upload_audio():
            logging.debug(f"upload_audio endpoint hit with data: {request}")
            if not self.check_auth(request):
                return jsonify({"error": "Authentication required"}), 401
            audio_buffer = request.files.get("audio_buffer")
            if not audio_buffer:
                return jsonify({"error": "audio_buffer is required"}), 400
            audio_url = asyncio.run(DiscordCommandProcessor.get_audio_url(audio_buffer.read()))
            return jsonify({"audio_url": audio_url.strip()})

    def check_auth(self, request):
        try:
            access_token = request.headers.get("Authorization")
            if access_token is None:
                raise Exception("No access token provided")
            logging.debug(f"Checking auth for access_token: {access_token}")
            token_type, access_token = access_token.split(' ', 1)
            if token_type.lower() != "bearer":
                # Invalid token type
                return

            if not access_token or not self.auth.validate_access_token(access_token):
                logging.error(f"Client provided invalid access token to REST API: {access_token}")
                return False
            return True
        except Exception as e:
            logging.error(f"Error checking auth: {e}")
            return False

    def create_db(self):
        with self.app.app_context():
            self.database_handler.db.create_all()