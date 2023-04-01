# For fixing circular load issues.
print("Inside base model class")
from discord_tron_master.classes.database_handler import DatabaseHandler
db = DatabaseHandler.get_db()
