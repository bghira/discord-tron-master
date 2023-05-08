import logging
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
        self.app = None

    def set_app(self, app):
        self.app = app
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

    def create_api_key(self, client_id, user_id):
        api_key = str(uuid.uuid4())
        with self.app.app_context():
            new_api_key = ApiKey(api_key=api_key, client_id=client_id, user_id=user_id, expires=datetime.datetime.utcnow() + datetime.timedelta(days=30))
            db.session.add(new_api_key)
            db.session.commit()
            return api_key

    def validate_api_key(self, api_key):
        with self.app.app_context():
            key_data = ApiKey.query.filter_by(api_key=api_key).first()
            if key_data and key_data.expires is None:
                # We found a perpetual key. Continue.
                logging.info("Key is perpetually active.")
                return True
            elif key_data and key_data.expires > datetime.datetime.utcnow():
                logging.debug("The api key has not expired yet.")
                return True
            logging.error("API Key was Invalid: %s" % api_key)
        return False

    def validate_access_token(self, access_token):
        with self.app.app_context():
            token_data = OAuthToken.query.filter_by(access_token=access_token).first()
            if token_data and token_data.expires_in is None:
                # We found a perpetual token. This is probably bad.
                raise Exception("Token is perpetually active.")
            elif token_data and (datetime.datetime.timestamp(token_data.issued_at)*1000 + token_data.expires_in) > datetime.datetime.utcnow().timestamp():
                logging.debug("The token has not expired yet.")
                return True
        logging.error("Access token was Invalid: %s" % access_token)
        return False

    # As far as I can tell, this is the most important aspect.
    def create_refresh_token(self, client_id, user_id, scopes=None, expires_in=None):
        import secrets
        # Don't do this method if you have a token already.
        token = OAuthToken.query.filter_by(client_id=client_id, user_id=user_id).first()
        if not token:
            token = OAuthToken(client_id, user_id, scopes=scopes, expires_in=expires_in)
            # Currently, we're only generating refresh_token once, at deploy.
            # This is less secure, but simpler for now.
            token.refresh_token = OAuthToken.make_token()
            with self.app.app_context():
                db.session.add(token)
                db.session.commit()
            return token

    # Refresh the auth link using the refresh_token
    def refresh_authorization(self, token_data, expires_in=None):
        logging.debug("Refreshing access token!")
        token_data.access_token = OAuthToken.make_token()
        logging.debug("Updating token for client_id: %s, user_id: %s, previous issued_at was %s" % (token_data.client_id, token_data.user_id, token_data.issued_at))
        token_data.set_issue_timestamp()
        logging.debug("After update, access_token is now %s" % token_data.access_token)
        logging.debug("After setting timestamp, issued_at is now %s" % token_data.issued_at)
        db.session.add(token_data)
        db.session.commit()
        return token_data

    # An existing access_token can be updated.
    def refresh_access_token(self, token_data):
        logging.debug("Refreshing access token!")
        token_data.access_token = OAuthToken.make_token()
        logging.debug("Updating token for client_id: %s, user_id: %s, previous issued_at was %s" % (token_data.client_id, token_data.user_id, token_data.issued_at))
        token_data.set_issue_timestamp()
        logging.debug("After setting timestamp, issued_at is now %s" % token_data.issued_at)
        db.session.add(token_data)
        db.session.commit()
        return token_data