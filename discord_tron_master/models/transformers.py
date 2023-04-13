from .base import db

class Transformers(db.Model):
    __tablename__ = 'transformers'
    id = db.Column(db.Integer, primary_key=True)
    model_owner = db.Column(db.String(255), unique=False, nullable=False)
    model_id = db.Column(db.String(255), unique=True, nullable=False)
    model_type = db.Column(db.String(16), nullable=False)
    preferred_ar = db.Column(db.String(4))
    enforced_ar = db.Column(db.String(4), nullable=False)
    description = db.Column(db.String(255), nullable=True)
    recommended_positive = db.Column(db.String(255), nullable=True)
    recommended_negative = db.Column(db.String(255), nullable=True)
    approved = db.Column(db.Boolean, default=False)
    added_by = db.Column(db.String(255), nullable=False)
    tags = db.Column(db.String(255), nullable=True)
    config_blob = db.Column(db.Text(), nullable=True)
    sag_capable = db.Column(db.Boolean, default=False)

    @staticmethod
    def get_all():
        return Transformers.query.all()

    @staticmethod
    def get_all_approved():
        return Transformers.query.filter_by(approved=True).all()

    @staticmethod
    def get_all_unapproved():
        return Transformers.query.filter_by(approved=False).all()

    @staticmethod
    def delete_all_unapproved():
        unapproved = Transformers.get_all_unapproved()
        for transformer in unapproved:
            db.session.delete(transformer)
        db.session.commit()

    @staticmethod
    def get_all_by_model_type(model_type):
        return Transformers.query.filter_by(model_type=model_type).all()
    @staticmethod
    def delete_by_full_model_id(full_model_id):
        model_id = full_model_id.split('/')[1]
        model_owner = full_model_id.split('/')[0]
        return Transformers.delete_by_model_id(model_id)
    @staticmethod
    def delete_by_model_id(model_id):
        existing_definition = Transformers.query.filter_by(model_id=model_id).first()
        if existing_definition is not None:
            db.session.delete(existing_definition)
            db.session.commit()
        return existing_definition
    @staticmethod
    def set_description(model_id, description):
        model_id = model_id.split('/')[1]
        existing_definition = Transformers.query.filter_by(model_id=model_id).first()
        if existing_definition is not None:
            existing_definition.description = description
            db.session.commit()
        return existing_definition
    @staticmethod
    def get_by_model_id(model_id):
        return Transformers.query.filter_by(model_id=model_id).first()
    @staticmethod
    def get_by_full_model_id(full_model_id):
        model_id = full_model_id.split('/')[1]
        model_owner = full_model_id.split('/')[0]
        return Transformers.query.filter_by(model_id=model_id, model_owner=model_owner).first()
    @staticmethod
    def create(model_id: str, model_type: str, added_by: str, approved: bool, preferred_ar: str = None, description: str = None, recommended_positive: str = None, recommended_negative: str = None, tags: str = None):
        existing_definition = Transformers.query.filter_by(model_id=model_id).first()
        if existing_definition is not None:
            db.session.delete(existing_definition)
            db.session.commit()
        model_owner = model_id.split('/')[0]
        model_id = model_id.split('/')[1]
        transformer = Transformers(model_id=model_id, model_owner=model_owner, model_type=model_type, approved=approved, preferred_ar=preferred_ar, description=description, recommended_positive=recommended_positive, recommended_negative=recommended_negative, tags=tags, added_by=added_by)
        db.session.add(transformer)
        db.session.commit()
        return transformer
   
    def to_dict(self):
        return {
            'model_id': self.model_id,
            'model_type': self.model_type,
            'preferred_ar': self.preferred_ar,
            'description': self.description,
            'recommended_positive': self.recommended_positive,
            'recommended_negative': self.recommended_negative,
            'config_blob': self.config_blob
        }
    
    def to_json(self):
        import json
        return json.dumps(self.to_dict())