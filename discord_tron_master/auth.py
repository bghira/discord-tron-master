from flask import request, jsonify
from functools import wraps
from flask_oauthlib.provider import OAuth2Provider
import uuid
import datetime
from .models import OAuthClient, OAuthToken, ApiKey
from .models.base import db

class Auth:
    def __init__(self):
        self._clientgetter = None
        self._tokengetter = None

    def set_app(self, app):
        self.oauth = OAuth2Provider(app)

    def clientgetter(self, func):
        self._clientgetter = func
        self.oauth.clientgetter(func)

    def tokengetter(self, func):
        self._tokengetter = func
        self.oauth.tokengetter(func)

    def require_oauth(self, scopes=None):
        def wrapper(f):
            @wraps(f)
            def decorated_function(*args, **kwargs):
                if not self._clientgetter or not self._tokengetter:
                    raise Exception("clientgetter and tokengetter not defined")

                valid, req = self.oauth.verify_request(scopes)
                if not valid:
                    return jsonify({"error": "Unauthorized access"}), 401

                return f(*args, **kwargs)
            return decorated_function
        return wrapper

    def validate_access_token(self, access_token):
        return self.validate_api_key(access_token)

    def create_api_key(self, client_id, user_id):
        api_key = str(uuid.uuid4())
        new_api_key = ApiKey(api_key=api_key, client_id=client_id, user_id=user_id, expires=datetime.datetime.utcnow() + datetime.timedelta(days=30))
        db.session.add(new_api_key)
        db.session.commit()
        return api_key

    def validate_api_key(self, api_key):
        key_data = ApiKey.query.filter_by(api_key=api_key).first()
        if key_data and key_data.expires > datetime.datetime.utcnow():
            return True
        return False
