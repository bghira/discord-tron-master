# For fixing circular load issues.
from flask_sqlalchemy import SQLAlchemy
from discord_tron_master.classes.database_handler import DatabaseHandler
db = DatabaseHandler.get_db()
