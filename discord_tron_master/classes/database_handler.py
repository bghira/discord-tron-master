from flask_sqlalchemy import SQLAlchemy
from .app_config import AppConfig

class DatabaseHandler:
    database_instance = None
    def __init__(self, app, config: AppConfig):
        mysql_user = config.get_mysql_user()
        mysql_password = config.get_mysql_password()
        mysql_hostname = config.get_mysql_hostname()
        mysql_dbname = config.get_mysql_dbname()
        app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+mysqlconnector://' + str(mysql_user) + ':' + str(mysql_password) + '@' + str(mysql_hostname) + '/' + str(mysql_dbname)
        # app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        self.app = app
        self.db = SQLAlchemy(self.app)
        DatabaseHandler.database_instance = self.db
    @classmethod
    def get_db(cls):
        return cls.database_instance