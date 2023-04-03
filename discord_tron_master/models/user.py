from .base import db
print("Inside User model")

class User(db.Model):
    __tablename__ = 'user'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(255), unique=True, nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)

    def __init__(self, username, email, password):
        self.username = username
        self.email = email
        self.password = password

    def has_client(self):
        from discord_tron_master.models import OAuthClient
        existing_client = OAuthClient.query.filter_by(user_id=self.id).first()
        if existing_client is None:
            return False
        return existing_client

    # Create an OAuth Client for a user. They'll get an existing one if it's there already.
    def create_client(self):
        from discord_tron_master.models import OAuthClient
        existing_client = OAuthClient.query.filter_by(user_id=self.id).first()
        if existing_client is not None:
            return existing_client
        client = OAuthClient(user_id=self.id, client_id=OAuthClient.generate_client_id(), client_secret=OAuthClient.generate_client_secret())
        db.session.add(client)
        db.session.commit()
        return client

    def get_by_api_key(self, api_key):
        from discord_tron_master.models import ApiKey
        api_key = ApiKey.query.filter_by(api_key=api_key).first()
        if api_key is None:
            return None
        return User.query.filter_by(id=api_key.user_id).first()