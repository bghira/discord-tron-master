from .base import db
import datetime, json

class Conversations(db.Model):
    __tablename__ = 'conversations'
    id = db.Column(db.Integer, primary_key=True)
    owner = db.Column(db.BigInteger(), unique=False, nullable=False)
    role = db.Column(db.String(255), nullable=False)
    history = db.Column(db.Text(), nullable=False, default='{}')
    created = db.Column(db.DateTime, nullable=False, default=db.func.now())
    updated = db.Column(db.DateTime, nullable=False, default=db.func.now())

    @staticmethod
    def get_all():
        return Conversations.query.all()

    @staticmethod
    def delete_all():
        all = Conversations.get_all()
        for conversation in all:
            db.session.delete(conversation)
        db.session.commit()

    @staticmethod
    def clear_history_by_owner(owner: int):
        conversation = Conversations.get_by_owner(owner)
        import logging
        logging.debug(f"Conversation before clearing: {conversation.history}")
        conversation.history = json.dumps(Conversations.get_new_history())
        logging.debug(f"Cleared conversation. New history: {conversation.history}")
        # Update Flask DB timestamp
        conversation.updated = db.func.now()
        db.session.commit()

    @staticmethod
    def create(owner: int, role: str, history: dict = None):
        existing_definition = Conversations.query.filter_by(owner=owner).first()
        if existing_definition is not None:
            return existing_definition
        if history is None:
            raise ValueError("History must be provided when creating a new conversation")
        conversation = Conversations(owner=owner, role="", history=json.dumps(history))
        db.session.add(conversation)
        db.session.commit()
        return conversation

    @staticmethod
    def get_by_owner(owner: int):
        conversation = Conversations.query.filter_by(owner=owner).first()
        conversation.history = json.loads(conversation.history)
        return conversation
    
    @staticmethod
    def set_history(owner: int, history: dict):
        conversation = Conversations.get_by_owner(owner)
        conversation.history = json.dumps(history)
        conversation.updated = db.func.now()
        db.session.commit()
        return conversation

    @staticmethod
    def get_new_history(role: str = None) -> list:
        if role is None:
            return []
        return [{"role": "system", "message": role}]

    @staticmethod
    def get_history(owner: int):
        conversation = Conversations.get_by_owner(owner)
        # Unload it if it is a string:
        if isinstance(conversation.history, str):
            conversation.history = json.loads(conversation.history)
        return conversation.history
    
    @staticmethod
    def set_role(owner: int, role: str):
        conversation = Conversations.get_by_owner(owner)
        conversation.role = role
        conversation.updated = db.func.now()
        db.session.commit()
        return conversation    
     
    @staticmethod
    def get_role(owner: int):
        conversation = Conversations.get_by_owner(owner)
        return conversation.role

    def to_dict(self):
        return {
            'owner': self.owner,
            'role': self.role,
            'history': self.history,
            'created': self.created,
            'updated': self.updated
        }    
    def to_json(self):
        import json
        return json.dumps(self.to_dict())