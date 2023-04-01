from discord_tron_master.classes.command_processor import CommandProcessor
from discord_tron_master.classes.job import Job
from discord_tron_master.classes.queue_manager import QueueManager
from discord_tron_master.classes.websocket_handler import WebSocketHandler
from discord_tron_master.classes.websocket_server import WebSocketServer
from discord_tron_master.classes.worker import Worker
from discord_tron_master.classes.worker_manager import WorkerManager
import asyncio

# Initialize the worker manager and add a few workers
worker_manager = WorkerManager()
worker1 = Worker("worker1", ["job_type_1", "job_type_2"])
worker2 = Worker("worker2", ["job_type_2", "job_type_3"])
worker_manager.add_worker(worker1)
worker_manager.add_worker(worker2)

# Initialize the command processor
command_processor = CommandProcessor(worker_manager)

# Initialize the WebSocket handler
websocket_handler = WebSocketHandler(command_processor)

# Initialize the WebSocket server
websocket_server = WebSocketServer(websocket_handler)

# Add some example jobs
job1 = Job("job_type_1", {"data": "Job 1 data"})
job2 = Job("job_type_2", {"data": "Job 2 data"})

# Start the workers and WebSocket server
async def main():
    worker1.start()
    worker2.start()

    # Add jobs to the queue
    worker1.add_job(job1)
    worker2.add_job(job2)

    # Start the WebSocket server
    await websocket_server.start()

# Run the example
asyncio.run(main())