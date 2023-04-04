from discord_tron_master.classes.job import Job
import logging

class ImageGenerationJob(Job):
    def __init__(self, payload):
        super().__init__("gpu", payload)
