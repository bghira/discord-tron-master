from flask import ( Flask, request, jsonify )
from flask_restful import Api, Resource
from .classes.database_handler import DatabaseHandler
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from .classes.app_config import AppConfig

class API:
    def __init__(self):
        config = AppConfig()
        self.app = Flask(__name__)
        database_handler = DatabaseHandler(self.app, config)
        self.db = database_handler.db

        from discord_tron_master.models import User, OAuthClient, OAuthToken, ApiKey
        self.migrate = Migrate(self.app, self.db)
        self.register_routes()

    def add_resource(self, resource, route):
        self.api.add_resource(resource, route)

    def run(self, host='0.0.0.0', port=5000):
        self.app.run(host=host, port=port)

    def register_routes(self):
        @self.app.route("/introspect", methods=["POST"])
        def introspect():
            token = request.form.get("token")
            if not token:
                return jsonify({"error": "No token provided"}), 400

            # Verify the token using the tokengetter function from auth.py
            token_data = self.oauth._tokengetter(token)
            if not token_data:
                return jsonify({"active": False})

            # Return the token details if it's valid
            return jsonify({
                "active": True,
                "scope": token_data.scopes,
                "client_id": token_data.client_id,
                "user_id": token_data.user_id,
                "exp": token_data.expires
            })
    def create_db(self):
        with self.app.app_context():
            from .models import OAuthToken, ApiKey, User
            self.database_handler.db.create_all()