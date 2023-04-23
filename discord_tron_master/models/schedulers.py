from .base import db
import json

class Schedulers(db.Model):
    __tablename__ = 'schedulers'
    id = db.Column(db.Integer, primary_key=True)
    # The "name" of a scheduler, e.g. "fast", "medium", "slow", "best"
    name = db.Column(db.String(255), unique=True, nullable=False)
    # The "internal" name of a scheduler, e.g. DPMSolverMultistepScheduler
    scheduler = db.Column(db.String(255), nullable=False)
    # A JSON array of use cases, e.g. [ "txt2img" ]
    use_cases = db.Column(db.Text(), nullable=True, default="[ \"txt2img\" ]")
    # The beginning and end of a range this scheduler is effective between for 'steps' parameter.
    steps_range_begin = db.Column(db.Integer)
    steps_range_end = db.Column(db.Integer)
    description = db.Column(db.String(255), nullable=True)
    # A JSON object to use for updating the scheduler config with, when it is applied.
    config_blob = db.Column(db.Text(), nullable=True)

    @staticmethod
    def get_all():
        return Schedulers.query.all()
    @staticmethod
    def get_by_name(name):
        return Schedulers.query.filter_by(name=name).first()
    @staticmethod
    def get_by_scheduler(scheduler):
        return Schedulers.query.filter_by(scheduler=scheduler).first()
    @staticmethod
    def get_by_use_case(use_case: str):
        all_schedulers = Schedulers.query.all()
        output = []
        for scheduler in all_schedulers:
            use_cases = json.loads(scheduler.use_cases)
            if use_case in use_cases:
                output.append(scheduler)
        return output

    @staticmethod
    def set_use_case(name, use_case):
        type_is = type(use_case)
        if type_is != "list" or type_is != "dict":
            raise ValueError(f"Received {type_is} for use_case record method, and needed list or dict")
        new_value = json.dumps(use_case)
        existing_definition = Schedulers.get_by_name(name)
        if existing_definition is not None:
            existing_definition.use_case = new_value
            db.session.commit()
        return existing_definition
    @staticmethod
    def set_description(name, description):
        existing_definition = Schedulers.get_by_name(name)
        if existing_definition is not None:
            existing_definition.description = description
            db.session.commit()
        return existing_definition
    @staticmethod
    def set_steps_range(name, steps_range_begin: int, steps_range_end: int):
        existing_definition = Schedulers.get_by_name(name)
        if existing_definition is not None:
            existing_definition.steps_range_begin = steps_range_begin
            existing_definition.steps_range_end = steps_range_end
            db.session.commit()
        return existing_definition

    @staticmethod
    def get_user_scheduler(user_config: dict):
        scheduler_name = user_config.get("scheduler", "default")
        scheduler = Schedulers.get_by_name(scheduler_name)
        if not scheduler or scheduler is None:
            raise RuntimeError(f"Could not find scheduler {scheduler_name} in database")
        return scheduler

    @staticmethod
    def delete_by_name(name):
        existing_definition = Schedulers.get_by_name(name)
        if existing_definition is not None:
            db.session.delete(existing_definition)
            db.session.commit()
        return existing_definition
    @staticmethod
    def create(name: str, scheduler: str, use_cases: str, description: str = None, config_blob: str = None, steps_range_begin: int = 10, steps_range_end: int = 500):
        existing_definition = Schedulers.get_by_name(name)
        if existing_definition is not None:
            db.session.delete(existing_definition)
            db.session.commit()
        scheduler = Schedulers(name=name, scheduler=scheduler, description=description, use_cases=json.dumps(use_cases), config_blob=config_blob, steps_range_begin=steps_range_begin, steps_range_end=steps_range_end)
        db.session.add(scheduler)
        db.session.commit()
        return scheduler
   
    def to_dict(self):
        return {
            "name": self.name,
            "scheduler": self.scheduler,
            "use_cases": self.use_cases,
            "description": self.description,
            "config_blob": self.config_blob,
            "steps_range_begin": self.steps_range_begin,
            "steps_range_end": self.steps_range_end
        }
    
    def to_json(self):
        import json
        return json.dumps(self.to_dict())