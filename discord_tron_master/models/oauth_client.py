print("Inside OauthClient model")
from .base import db


class OAuthClient(db.Model):
    __tablename__ = 'oauth_client'
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.String(40), unique=True, nullable=False)
    client_secret = db.Column(db.String(128), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    _redirect_uris = db.Column(db.Text)
    _default_scopes = db.Column(db.Text)

    @property
    def client_type(self):
        return 'confidential'

    @property
    def redirect_uris(self):
        if self._redirect_uris:
            return self._redirect_uris.split()
        return []

    @property
    def default_redirect_uri(self):
        return self.redirect_uris[0]

    @property
    def default_scopes(self):
        if self._default_scopes:
            return self._default_scopes.split()
        return []

    def __init__(self, **kwargs):
        super(OAuthClient, self).__init__(**kwargs)

    def __repr__(self):
        return f'<OAuthClient {self.client_id}>'
