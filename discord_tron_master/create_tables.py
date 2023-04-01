from models import db
from api import API
import sys, os
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

api = API()
with api.app.app_context():
    db.create_all()
