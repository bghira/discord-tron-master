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
    message = db.Column(db.String(255), unique=False, nullable=False, index=True)
    prompt = db.Column(db.String(768), nullable=False)
    date_created = db.Column(db.Integer, nullable=False)
    config_blob = db.Column(db.Text(), nullable=True)

    @staticmethod
    def get_all():
        return UserHistory.query.all()

    @staticmethod
    def get_all_prompts():
        return UserHistory.query.with_entities(UserHistory.prompt).distinct().all()

    @staticmethod
    def search_all_prompts(search_term: str = None, excludes: list = None) -> list:
        """
        Searches for prompts that match 'search_term' (with wildcards), excluding any in 'excludes'.
        - Wildcards: '*' -> '%', '?' -> '_'
        - Exclusions: each exclude term is also wildcard-translated and used with a NOT ILIKE condition.
        Returns a list of distinct prompt strings.
        """
        query = UserHistory.query

        # If there's a search term, convert * -> %, ? -> _
        if search_term:
            wildcard_search = search_term.replace('*', '%').replace('?', '_')
            # We can wrap with '%' to allow for substring matching
            if not wildcard_search.startswith('%'):
                wildcard_search = f"%{wildcard_search}"
            if not wildcard_search.endswith('%'):
                wildcard_search = f"{wildcard_search}%"
            query = query.filter(UserHistory.prompt.ilike(wildcard_search))

        # If excludes is not empty, apply each as a NOT ILIKE
        if excludes:
            for exclude_term in excludes:
                exclude_term = exclude_term.replace('*', '%').replace('?', '_')
                # Similarly handle substring searching
                if not exclude_term.startswith('%'):
                    exclude_term = f"%{exclude_term}"
                if not exclude_term.endswith('%'):
                    exclude_term = f"{exclude_term}%"
                query = query.filter(~UserHistory.prompt.ilike(exclude_term))
        
        # Return distinct prompt strings
        return query.with_entities(UserHistory.prompt).distinct().all()

    @staticmethod
    def get_all_user_prompts(user: str):
        return UserHistory.query.with_entities(UserHistory.prompt).filter_by(user=user).distinct().all()

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
        import time
        user_history = UserHistory(user=user, message=message, prompt=prompt, config_blob=json.dumps(config_blob), date_created=int(time.time()))
        db.session.add(user_history)
        db.session.commit()
        return user_history


    @staticmethod
    def add_entry(user: str, message: str, prompt: str, config_blob: dict = {}):
        result = UserHistory.create(user, message, prompt, config_blob=json.dumps(config_blob))
        
        return result
        

    @staticmethod
    def get_user_statistics(user: str) -> dict:
        """
        Return a dict of statistics for a user.
        """
        user_history = UserHistory.get_by_user(user, return_all=True)
        if not user_history or user_history is None:
            raise RuntimeError(f"Could not find results for {user} in database")
        return {
            "total": len(user_history),
            "unique": len(set([entry.prompt for entry in user_history])),
            "history": [entry.to_dict() for entry in user_history],
            "common_terms": UserHistory.get_user_most_common_terms(user_history),
            "frequent_prompts": UserHistory.get_user_most_common_prompts(user, limit=3)
        }

    @staticmethod
    def get_user_most_common_prompts(user: str, limit: int = 10):
        """
        Return a dict of the limit number of most common prompts for a user.
        
        Sort by most to least frequent.
        """
        user_history = UserHistory.get_by_user(user, return_all=True)
        if not user_history or user_history is None:
            raise RuntimeError(f"Could not find results for {user} in database")
        prompts = {}
        for entry in user_history:
            if entry.prompt not in prompts:
                prompts[entry.prompt] = 0
            prompts[entry.prompt] += 1
        # Sort prompts by count:
        sorted_prompts = sorted(prompts.items(), key=lambda x: x[1], reverse=True)
        logger.debug(f"Sorted prompts: {sorted_prompts}")
        output = f"{len(sorted_prompts[:limit])} most frequently used prompts are:\n"
        for prompt, count in sorted_prompts[:limit]:
            output = f"{output}- **{prompt}** with _*{count}*_ uses\n"
        return output

    @staticmethod
    def get_user_most_common_terms(user_history, term_limit: int = 10, search_limit: int = 10000) -> dict:
        """
        Return a dict of the term_limit number of most common terms in the most recent 'search_limit' number of history entries.
        
        Sort by most to least frequent.
        """
        terms = {} # Will be a key-indexed dict of counts for each term.
        stop_words = [
            "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "could", "do",
            "does", "doing", "done", "for", "from", "had", "has", "have", "he", "her",
            "here", "hers", "his", "i", "in", "is", "it", "its", "may", "me", "might",
            "must", "my", "no", "not", "of", "on", "or", "our", "shall", "she", "should",
            "that", "the", "their", "them", "there", "they", "this", "to", "us", "was",
            "we", "were", "where", "when", "will", "with", "would", "yes", "you", "your"
        ]
        counter = 0
        for entry in user_history:
            counter += 1
            if counter > search_limit:
                break
            if not entry.prompt:
                logging.warning(f"Entry {entry} had no prompt. Not including in statistics.")
                continue
            # Split prompt into terms by whitespace:
            prompt_terms = entry.prompt.lower().split(" ")
            # Remove punctuation from the prompt:
            prompt_terms = [term.strip("():.,?!") for term in prompt_terms]
            for term in prompt_terms:
                if term in stop_words or term == '':
                    continue
                if len(term) < 4:
                    continue
                if term not in terms:
                    terms[term] = 0
                terms[term] += 1
        # Sort terms by count:
        sorted_terms = sorted(terms.items(), key=lambda x: x[1], reverse=True)
        logger.debug(f"Sorted terms: {sorted_terms}")
        output = f"{len(sorted_terms[:term_limit])} most frequently used terms are:\n"
        for term, count in sorted_terms[:term_limit]:
            output = f"{output}- **{term}** with _*{count}*_ uses\n"
        return output

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