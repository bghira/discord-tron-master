from .base import db
import json, logging

logger = logging.getLogger('UserHistory')
logger.setLevel('DEBUG')

class UserHistory(db.Model):
    """
    Contains a history of user jobs, their message IDs.
    """
    __tablename__ = 'user_history'
    id = db.Column(db.Integer, primary_key=True)
    user = db.Column(db.String(255), unique=False, nullable=False, index=True)
    message = db.Column(db.String(255), unique=True, nullable=False)
    prompt = db.Column(db.String(255), nullable=False)
    date_created = db.Column(db.Integer, nullable=False)
    config_blob = db.Column(db.Text(), nullable=True)

    @staticmethod
    def get_all():
        return UserHistory.query.all()

    @staticmethod
    def get_by_user(user, return_all:bool = False):
        if not return_all:
            results = UserHistory.query.filter_by(user=user).first()
        else:
            results = UserHistory.query.filter_by(user=user).all()
        if not results or results is None:
            raise RuntimeError(f"Could not find results for {user} in database")
        return results

    @staticmethod
    def get_by_message(message):
        return UserHistory.query.filter_by(message=message).first()

    @staticmethod
    def clear_by_user(user):
        """
        Clear out the user generation history for a single user.
        """
        all_user_history = UserHistory.get_by_user(user, return_all=True)
        if not all_user_history or all_user_history is None:
            raise RuntimeError(f"Could not find results for {user} in database")
        for user_history in all_user_history:
            db.session.delete(user_history)

    @staticmethod
    def clear_all():
        """
        Clear the entire history table.
        """
        all_user_history = UserHistory.get_all()
        if not all_user_history or all_user_history is None:
            raise RuntimeError(f"Could not find results in database")
        for user_history in all_user_history:
            db.session.delete(user_history)

    @staticmethod
    def create(user: str, message: str, prompt: str, config_blob: dict = {}):
        existing_definition = UserHistory.get_by_message(message)
        if existing_definition is not None:
            logger.warning(f"User history entry already exists for message {message}, ignoring.")
            return
        import time
        user_history = UserHistory(user=user, message=message, prompt=prompt, user_history=user_history, config_blob=json.dumps(config_blob), date_created=int(time.time()))
        db.session.add(user_history)
        db.session.commit()
        return user_history


    @staticmethod
    def add_entry(user: str, message: str, prompt: str, config_blob: dict = {}):
        existing_entry = UserHistory.get_by_message(message)
        if existing_entry:
            logger.warn(f"User history entry already exists for message {message}, ignoring.")
            return
        result = UserHistory.create(user, message, prompt, config_blob=json.dumps(config_blob))
        
        return result
        

    def to_dict(self):
        return {
            "user": self.user,
            "message": self.message,
            "prompt": self.prompt,
            "date_created": self.date_created,
            "config_blob": self.config_blob,
        }
    
    def to_json(self):
        import json
        return json.dumps(self.to_dict())