# TRON Discord Bot 🤖

This project encompasses the "master hub" for the [TRON worker.](https://github.com/bghira/discord-tron-client)

This codebase is capable of:

* Distributing PyTorch and other jobs to worker nodes
  + Stable Diffusion, Bark TTS, WizardLM, and more.
* Interfacing with users via a Discord bot frontend
* Flexible pipeline configuration, with optimized defaults
  + Create incredible results with little effort!
* Self-hosting images, when accompanied with a companion webserver
* Integrating OpenAI's GPT API into a Discord interface with conversation
  history tracking and pruning, so that the history properly rolls over
  without overrunning the API token limit.
* Img2Img and upscaling via Stable Diffusion workers
* As-deterministic-as-possible generation, when desired
* Reliable and exceptional results, out of the box, compared to other
  tools such as `automatic1111/stable-diffusion-webui`

This master code base does not **require** the client code to work.
However, its utility is greatly reduced without GPU worker nodes.

## Current State

This project is undergoing active development. It has no internal API
for extensions, or REST API integration within other projects.
Currently, extending for new functionality is an involved process.

⚠️ **Disclaimer**: Expect to have things break on update sometimes,
especially through the more experimental features.
Older stuff is unlikely to break, and newer things more likely to break.

## Requirements

This portion of the codebase has lightweight requirements.

A Raspberry Pi 3 with a running MySQL server and nginx webserver,
could easily handle running the master node.

* MySQL: Used for storing OAuth details, diffusion models, etc.
* nginx: If the server has a public hostname, it can be used to
  host its own images, rather than sending them to Discord or Imgur.

## Installation

1. Create a python venv:

```bash
python -m venv .venv/
```

2. Enter the venv:

```bash
. .venv/bin/activate
```

3. Install poetry:

```bash
pip install poetry
```

4. Install all project requirements:

```bash
poetry install
```

## Configuring

1. Copy `discord_tron_client/config/example.json` to `discord_tron_client/config/config.json`

2. Create a MySQL database and user. Update the values in `config.json` accordingly.

3. Inside your venv, execute:

```bash
flask db init
flask db upgrade
```

Or, the `create_tables.py` script may be of some use to you.

4. Update `config.json` to contain your relevant API keys:
  + OpenAI
  + Huggingface Hub
  + Discord

5. Update the values in `config.json` to point to your WebSocket server host and port:

```json
   "websocket_hub": {
        "host": "example.net",
        "port": 6789,
        "tls": true,
        "protocol": "wss"
   }
```

**This produces more accurate config templates when adding worker nodes later.**

6. Run the master **from the top level git directory** (*this* folder).

This codebase has **two** components you will run:

```bash
. .venv/bin/activate # Always ensure you're in the virtual environment first.
python -m discord_tron_master run > master.log 2>&1
```

And, in another terminal (or `tmux` / `screen` session):

```bash
. .venv/bin/activate
gunicorn -w 4 -b 0.0.0.0:5000 --certfile=discord_tron_master/config/server_cert.pem \
         --keyfile=discord_tron_master/config/server_key.pem \
         discord_tron_master.gunicorn_entry:api -t 120 > web.log 2>&1
```

You must ensure that your firewall, if any, is not blocking ports:

* TCP **5000**
* TCP **6789**

## Adding a client worker

Google Colab can be used successfully as a worker for this project.

Kaggle has not been tested.

Any remote Linux system with a GPU would, in theory, work, as long
as you can install the proper library versions, and run python scripts.

1. Inside `discord-tron-master` (this folder):

```bash
python -m 'discord_tron_master' create_worker_user --username 'colab_worker' --password 'example.pass' --email 'colab@example.net'
```

2. Inside the same directory:

```bash
. .venv/bin/activate
python -m discord_tron_master create_client_tokens --username colab_worker
```

Which prints, something like:

```
[INFO] Client does not exist for user throwaway - we will try to create one.
[INFO] Checking for API Key...
[INFO] No API Key found, generating one...
[INFO] API key for client/user:
{
    "api_key": "...",
    "client_id": "...",
    "user_id": 1,
    "expires": null
}
[INFO] Checking for existing tokens...
[INFO] Creating tokens for user throwaway
{
    "id": 1,
    "access_token": "...",
    "refresh_token": "...",
    "expires_in": 3600,
    "client_id": "...",
    "user_id": 1,
    "scopes": null,
    "issued_at": "..."
}
```
3. The first block contains `api_key`, which needs to be **transferred to the client**
   and placed in `discord-tron-client/discord_tron_client/config/config.json`:

```json
...
    "master_api_key": "... place the api key here ...",
...
```
3. The second block, **in its entirety** must be **transferred to the client**
   and placed in `discord-tron-client/discord_tron_client/config/auth.json`

4. Place the resulting SSL key and certificate files `server_key.pem` and
   `server_cert.pem` **on the client system** under the `config/` directory.

## Project Structure 🏗️

* `classes/`: A somewhat-structured folder for many useful classes.
  + `command_processors/`: Process incoming worker commands via WebSocket.
* `discord/`: Some helpers to make life easier.
* `cogs/`: A structured way to store Discord bot commands.
  + **Example**: `cogs/user/settings.py` handles the `!settings` command.
* `config/`: Pretty self-explanatory.
* `exceptions/`: Basic error handling classes for flow control.
* `migrations/`: Automatically-generated SQLAlchemy-Flask migrations.
* `models/`: Code representation of Database objects.
* `api.py`: Flask API routes, currently a very basic and subpar layout.
* `websocket_hub.py`: The WebSocket hub code which handles auth and connection.
* `bot.py`: The Discord frontend code that handles basic initialization and
   command routing / cog registration and loading.
* `LICENSE`: The Silly Use License (SUL-1.0), because why not have some fun
  while coding? 😜

## Extending the Project 🚀

To add a new !command to the bot:

1. Add the !command cog processor and, if needed, a Job class.
2. If the client will be sending a new data type that is currently unhandled,
   add a new entry to the `command_processor` module that uses your handler.
   + If the existing data matches an existing workflow for the user, eg,
     some text or an image to send - you can reuse the existing WebSocket
     command handlers on the master backend.
4. Test your changes extensively. No one wants to accept broken code.
5. Open a pull request, and hope for the best! 🤞

## Limitations 😬

### Discord + Flask integration

When designing this application, the need to run Flask via Gunicorn
resulted in the Discord bot no longer having direct access to the
Flask context, and vice versa.

In other words, the HTTP endpoints can not send a message to Discord.

This isn't a huge deal in practice, because currently, the only use
of the HTTP endpoints are for authentication exchanges and uploading
binary data that might be larger than the 32M window for WebSockets.

In testing, it was determined that sending large binary data over
this type of WebSocket hub design would result in stalled messages
even when using threaded design.