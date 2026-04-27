# AGENTS.md - discord-tron-master rolodex

Quick map of where things live and how data flows.

## Entry points
- `python -m discord_tron_master run` -> `discord_tron_master/__main__.py` (wires bot + websocket hub + managers)
- Flask API (auth + uploads) -> `discord_tron_master/api.py` served via `discord_tron_master/gunicorn_entry.py`

## Runtime topology
- Discord bot core: `discord_tron_master/bot.py` (loads cogs from `discord_tron_master/cogs`)
- WebSocket hub: `discord_tron_master/websocket_hub.py` (worker connections, auth, dispatch)
- Job + queue system:
  - `discord_tron_master/classes/job.py`
  - `discord_tron_master/classes/job_queue.py`
  - `discord_tron_master/classes/queue_manager.py`
  - `discord_tron_master/classes/worker_manager.py`
  - `discord_tron_master/classes/worker.py`
- Job types: `discord_tron_master/classes/jobs/*.py`
  - Image: `image_generation_job.py`, `webui_image_job.py` (HTTP callback instead of Discord)
  - Variations: `prompt_variation_job.py`, `promptless_variation_job.py`
  - Upscaling: `image_upscaling_job.py`
  - LLM: `llama_prediction_job.py`, `stablelm_prediction_job.py`, `stablevicuna_prediction_job.py`, `ollama_completion_job.py`
  - TTS: `bark_tts_job.py`

## Message flow (on_message)
- Listener: `discord_tron_master/cogs/image/img2img.py` -> `Img2img.on_message`
  - ignores bots (except SimpleTuner update cleanup)
  - thread owned by bot -> `Generate.generate` via `*prompt`
  - bot mention -> `_handle_mentioned_message`
    - image attachment or URL -> `_handle_image_attachment` -> job enqueue
    - text-only -> GPT chat via `classes/openai/*` + `models/conversation.py`

## Worker + WebSocket flow
- `WebSocketHub.handler` validates bearer token (via `auth.py`) and parses payloads
- `CommandProcessor` routes by `module_name` + `module_command`
  - `worker.register/unregister` -> `WorkerManager`
  - `job_queue.finish/acknowledge` -> `WorkerManager`
  - `message.send/edit/delete` -> `classes/command_processors/discord.py`
  - `ollama.complete` -> `classes/command_processors/ollama.py` -> `remote_ollama_broker`
  - `hardware.update` -> `classes/command_processors/hardware.py`
- Jobs are enqueued to `QueueManager`, picked by `WorkerManager`, processed by `Worker.process_jobs`, then sent to worker over websocket

## Data + config
- App config + user settings: `discord_tron_master/classes/app_config.py`
- Guild config: `discord_tron_master/classes/guilds.py`
- DB + models: `discord_tron_master/classes/database_handler.py`, `discord_tron_master/models/*.py`
  - Core models: `models/base.py`, `models/user.py`, `models/conversation.py`
  - OAuth: `models/oauth_token.py`, `models/oauth_client.py`, `models/api_key.py`
  - ML helpers: `models/transformers.py`, `models/schedulers.py`
  - History: `models/user_history.py`
- Migrations: `discord_tron_master/migrations/`
- OpenAI text client + prompt helpers: `discord_tron_master/classes/openai/text.py`, `classes/openai/tokens.py`, `classes/openai/chat_ml.py`
- Utilities: `classes/resolution.py`, `classes/text_replies.py`, `classes/message.py`, `classes/log_format.py`, `classes/custom_help.py`
- Ollama broker: `discord_tron_master/classes/remote_ollama_broker.py`
- Text game web UI: `discord_tron_master/classes/text_game_webui_runner.py`

## Cogs (Discord UX surface)
- Image gen: `discord_tron_master/cogs/image/generate.py`, `discord_tron_master/cogs/image/img2img.py`
- LLMs: `discord_tron_master/cogs/llama/predict.py`, `discord_tron_master/cogs/stablelm/stablelm_predict.py`, `discord_tron_master/cogs/stablevicuna/stablevicuna_predict.py`
- TTS: `discord_tron_master/cogs/tts/audio_generation.py`
- User + settings: `discord_tron_master/cogs/user/user.py`, `discord_tron_master/cogs/user/settings.py`
- Model + scheduler management: `discord_tron_master/cogs/models/model.py`, `discord_tron_master/cogs/models/scheduler.py`
- Workers: `discord_tron_master/cogs/workers/worker.py`
- Events: `discord_tron_master/cogs/events/reactions.py`
- Zork RPG: `discord_tron_master/cogs/zork.py`, `discord_tron_master/classes/zork_memory.py`

## Common trace paths
- `!generate` -> `Generate.generate` -> `ImageGenerationJob` -> `QueueManager.enqueue_job` -> `Worker.process_jobs` -> websocket send
- Mention + image -> `Img2img.on_message` -> `PromptVariationJob` / `PromptlessVariationJob` / `ImageUpscalingJob`
- Mention + text -> `Img2img._handle_mentioned_message` -> `ChatML` + `GPT.discord_bot_response`
