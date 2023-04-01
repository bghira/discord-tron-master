from datetime import datetime
from .base import db

class OAuthToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    access_token = db.Column(db.String(255), unique=True, nullable=False)
    refresh_token = db.Column(db.String(255), unique=True)
    expires_in = db.Column(db.Integer)
    client_id = db.Column(db.String(255), db.ForeignKey('oauth_client.client_id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    scopes = db.Column(db.Text)
    issued_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    client = db.relationship('OAuthClient')
    user = db.relationship('User')