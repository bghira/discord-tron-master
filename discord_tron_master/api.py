from flask import Flask
from flask_restful import Api, Resource

class API:
    def __init__(self):
        self.app = Flask(__name__)
        self.api = Api(self.app)

    def add_resource(self, resource, route):
        self.api.add_resource(resource, route)

    def run(self, host='0.0.0.0', port=5000):
        self.app.run(host=host, port=port)
