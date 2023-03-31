# Inside models/oauth_client.py
from discord_tron_master.classes.database_handler import DatabaseHandler
from discord_tron_master.api import API

db = API.database_handler.db

# Example OauthClient model.
class OAuthClient(db.Model):
    id = db.Column(db.String(40), primary_key=True)
    client_secret = db.Column(db.String(55), nullable=False)
    # ... other fields ...

    def __init__(self, **kwargs):
        # Initialize the OAuthClient instance