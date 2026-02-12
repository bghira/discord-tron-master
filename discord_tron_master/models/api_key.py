from .base import db


class ApiKey(db.Model):
    __tablename__ = "api_key"
    id = db.Column(db.Integer, primary_key=True)
    api_key = db.Column(db.String(255), unique=True, nullable=False)
    client_id = db.Column(
        db.String(255), db.ForeignKey("oauth_client.client_id"), nullable=False
    )
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    expires = db.Column(db.DateTime, nullable=True)

    client = db.relationship("OAuthClient")
    user = db.relationship("User")

    @staticmethod
    def get_by_user_id(user_id):
        return ApiKey.query.filter_by(user_id=user_id).first()

    @staticmethod
    def generate_by_user_id(user_id):
        from discord_tron_master.models import OAuthClient

        existing_key = ApiKey.query.filter_by(user_id=user_id).first()
        if existing_key is not None:
            db.session.delete(existing_key)
            db.session.commit()
        client = OAuthClient.query.filter_by(user_id=user_id).first()
        if client is None:
            client = OAuthClient(
                user_id=user_id,
                client_id=OAuthClient.generate_client_id(),
                client_secret=OAuthClient.generate_client_secret(),
            )
            db.session.add(client)
            db.session.commit()
        api_key = ApiKey(
            api_key=ApiKey.generate_api_key(),
            client_id=client.client_id,
            user_id=user_id,
        )
        db.session.add(api_key)
        db.session.commit()
        return api_key

    @staticmethod
    def generate_api_key():
        import secrets

        return secrets.token_urlsafe(32)

    def to_dict(self):
        return {
            "api_key": self.api_key,
            "client_id": self.client_id,
            "user_id": self.user_id,
            "expires": self.expires,
        }
