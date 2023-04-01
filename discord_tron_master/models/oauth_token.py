from discord_tron_master.models.base import db
from datetime import datetime, timedelta
import secrets

class OAuthToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    access_token = db.Column(db.String(255), unique=True, nullable=False)
    refresh_token = db.Column(db.String(255), unique=True, nullable=False)
    expires_in = db.Column(db.Integer)
    client_id = db.Column(db.String(255), db.ForeignKey('oauth_client.client_id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    scopes = db.Column(db.Text)
    issued_at = db.Column(db.DateTime, default=datetime.utcnow)

    client = db.relationship('OAuthClient')
    user = db.relationship('User')

    def __init__(self, client_id, user_id, scopes=None, expires_in=None):
        self.access_token = secrets.token_urlsafe(32)
        self.refresh_token = secrets.token_urlsafe(32)
        self.client_id = client_id
        self.user_id = user_id
        self.scopes = scopes
        self.expires_in = expires_in or 3600
        self.issued_at = datetime.utcnow()

    def is_expired(self):
        return datetime.utcnow() > self.issued_at + timedelta(seconds=self.expires_in)

    def to_dict(self):
        return {
            "id": self.id,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_in": self.expires_in,
            "client_id": self.client_id,
            "user_id": self.user_id,
            "scopes": self.scopes,
            "issued_at": self.issued_at.isoformat() if self.issued_at else None
        }

    # Update the issuance timestamp when we're refreshing a token.
    def set_issue_timestamp(self):
        self.issued_at = datetime.utcnow()
    
    @staticmethod
    def make_token():
        return secrets.token_urlsafe(32)

