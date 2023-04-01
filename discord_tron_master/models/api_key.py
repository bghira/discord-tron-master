from datetime import datetime
from .base import db

class ApiKey(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    api_key = db.Column(db.String(255), unique=True, nullable=False)
    client_id = db.Column(db.String(255), db.ForeignKey('oauth_client.client_id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    expires = db.Column(db.DateTime)
    
    client = db.relationship('OAuthClient')
    user = db.relationship('User')