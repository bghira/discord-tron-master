import asyncio
import json
import logging
import os
import re
import subprocess
import threading

import openai
from openai import OpenAI

from discord_tron_master.classes.app_config import AppConfig

config = AppConfig()
logger = logging.getLogger(__name__)
logger.setLevel("INFO")

openai.api_key = config.get_openai_api_key()

_BACKEND_SEMAPHORES: dict[str, asyncio.Semaphore] = {}
_BACKEND_SEMAPHORE_LOCK = threading.Lock()
_BACKEND_CONCURRENCY_LIMIT = 4  # per backend
_PROMPT_SECTION_RE = re.compile(r"^([A-Z][A-Z0-9_]+):(?:\s*(.*))?$")


def _get_backend_semaphore(backend: str) -> asyncio.Semaphore:
    key = (backend or "zai").strip().lower()
    with _BACKEND_SEMAPHORE_LOCK:
        sem = _BACKEND_SEMAPHORES.get(key)
        if sem is None:
            sem = asyncio.Semaphore(_BACKEND_CONCURRENCY_LIMIT)
            _BACKEND_SEMAPHORES[key] = sem
        return sem


class GPT:
    _ZAI_MODEL = "glm-5"
    _CLI_TIMEOUT_SECONDS = 300.0
    _CLI_WORKDIR = "/tmp/discord-tron-master-gpt"
    _TEXT_COMPLETION_INSTRUCTIONS = (
        "You are being used as a text-completion backend, not as a coding agent. "
        "Do not inspect the workspace, read files, run shell commands, or infer hidden tasks "
        "from nearby repositories unless the prompt explicitly asks for that. "
        "Respond directly to the prompt content only."
    )

    def __init__(self):
        self.engine = "o3-mini"
        self.temperature = 0.9
        self.max_tokens = 4096
        self.discord_bot_role = "You are a Discord bot."
        self.config = AppConfig()
        self.backend = "zai"

    def set_values(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    async def sentiment_analysis(self, prompts):
        prompt = f"As a playful exercise, analyse the user who provided the following text: {prompts}"
        system_role = "You are a sentiment analysis bot. Provide ONLY up to two paragraphs explaining the averages. Do not use run-on sentences or make a wall of text. Do not explain what a sentiment analysis is. Just provide the paragraph. You can use Discord formatting or average percent values to describe trends, but keep it succinct."
        return await self.turbo_completion(system_role, prompt, temperature=1.18)

    async def updated_setting_response(self, name, value):
        prompt = f"Please provide a message to the user. They have updated setting '{name}' to be set to '{value}'"
        return await self.turbo_completion(self.discord_bot_role, prompt)

    async def compliment_user_selection(self):
        role = "You are Joe Rogan! Respond as he would."
        prompt = "Return just a compliment on a decision I've made. Maybe you can ask Jamie to pull a clip up about the image that's about to be generated. Short and sweet output only."
        return await self.turbo_completion(
            role, prompt, max_tokens=50, temperature=1.05, engine="text-davinci-003"
        )

    async def insult_user_selection(self):
        role = "You are Joe Rogan! We tease each other in non-offensive ways. We are friends. Keep it short and sweet."
        prompt = "Return just a playful, short and sweet tease me about a decision I've made, in the style of Joe Rogan."
        return await self.turbo_completion(
            role, prompt, temperature=1.05, max_tokens=50, engine="text-davinci-003"
        )

    async def insult_or_compliment_random(self):
        import random

        random_number = random.randint(1, 2)
        if random_number == 1:
            return await self.insult_user_selection()
        return await self.compliment_user_selection()

    async def random_image_prompt(self, theme: str = None):
        prompt = "Print an image caption on a single line."
        if theme is not None:
            prompt = prompt + ". Your theme for consideration: " + theme
        system_role = "You are a Prompt Generator Bot, that strictly generates prompts, with no other output, to avoid distractions.\n"
        system_role = f"{system_role}Your prompts look like these 3 examples:\n"
        system_role = f"{system_role}A 1983 photograph of astonishing daisies in the rolling hills of Some Location. The image has beautiful quality and kodachrome style.\n"
        system_role = f"{system_role}A high quality camera photo of great look up a rolling wave; the ocean is present in full view, as a surfer challenges himself by paddling out to the break.\n"
        system_role = f"{system_role}digital artwork, feels like the first time, we went to the zoo, colourful and majestic, amazing clouds in the sky, epic\n"
        system_role = f"{system_role}Natural language prompting works best with short and concise bits.\n"
        system_role = f"{system_role}Any additional output other than the prompt will damage the results. Stick to just the prompts."
        image_prompt_response = await self.turbo_completion(
            system_role, prompt, temperature=1.18
        )
        logger.setLevel(config.get_log_level())
        logger.debug(
            f"OpenAI returned the following response to the prompt: {image_prompt_response}"
        )
        return image_prompt_response

    async def auto_model_select(self, prompt: str, query_str: str = None):
        if query_str is None:
            query_str = (
                "\nModels:"
                "\n -> ptx0/terminus-xl-otaku-v1"
                "\n    -> Anime, cartoons, comics, manga, ghibli, watercolour."
                "\n -> ptx0/terminus-xl-gamma-v2"
                "\n    -> Requests for 'high quality' images go here, but it has some high frequency noise issues."
                "\n -> ptx0/terminus-xl-gamma-training"
                "\n    -> This was an attempt to resolve some issues in the v2 model, but the issues persist. It noticeably improves on some concepts, and the high freq noise issue appears less often than v2."
                "\n -> ptx0/terminus-xl-gamma-v2-1"
                "\n    -> Cinema, photographs, most images with text in them, adult content, etc. This is the default model, but if the request contains 'high quality', it should use gamma-v2 or training instead."
                "\n -> terminusresearch/fluxbooru-v0.3"
                "\n    -> Flux is a 12B parameter model, slow but very good for complex prompts and anime/drawn text. Typography requests and cinematic stuff do well here too."
                "\n -> stabilityai/stable-diffusion-3.5-medium"
                "\n    -> Needs longer more detailed prompts but can do really well for realism and typography if the text is shorter."
                "\n\n-----------\n\n"
                "Resolutions: "
                "\n| Square        | Landscape    | Portrait     |"
                "\n+---------------+--------------+--------------+"
                "\n|               | 1024x960     | 960x1088     | "
                "\n| 1024x1024     | 1088x896     | 960x1024     | "
                "\n|               | 1088x960     | 896x1152     | "
                "\n|               | 1152x832     | 704x1472     | "
                "\n|               | 1152x896     | 768x1280     | "
                "\n|               | 1216x832     | 768x1344     | "
                "\n|               | 1280x768     | 832x1152     | "
                "\n|               | 1344x704     | 832x1216     | "
                "\n|               | 1344x768     | 896x1088     | "
                "\n\n-----------\n\n"
                "Output format:\n"
                '{"model": <selected model>, "resolution": <selected resolution>}'
                "\n\n-----------\n\n"
                "Objective: Determine from the user prompt which model to use. The content can be better if an appropriate resolution/aspect are chosen - eg portraits are taller, pictures of book covers may be too, but try not to use extreme aspects unless the prompt demands it.."
                "\n\n-----------\n\n"
                "Analyze Prompt: " + prompt
            )

        system_role = "Print ONLY the specified JSON document WITHOUT any other markdown or formatting. Determine which model and resolution would work best for the user's prompt, ignoring any other issues. If anything but the JSON object and the defined keys are returned, THE APPLICATION WILL ERROR OUT."
        prediction = await self.turbo_completion(
            system_role, query_str, temperature=1.18
        )
        for line in prediction.split("\n"):
            if "```" in line:
                prediction = prediction.replace(line, "")

        try:
            result = json.loads(prediction)
            model_name = result["model"]
            raw_resolution = result["resolution"]
            width, heidht = raw_resolution.split("x")
            resolution = {"width": int(width), "height": int(heidht)}
        except Exception:
            logger.setLevel(config.get_log_level())
            logger.error(f"Error parsing JSON from prediction: {prediction}")
            return ("1280x768", "ptx0/terminus-xl-gamma-training")
        logger.setLevel(config.get_log_level())
        logger.debug(
            f"OpenAI returned the following response to the prompt: {model_name}"
        )
        if "/" not in model_name:
            logger.setLevel(config.get_log_level())
            logger.warning(
                "OpenAI refused to label our spicy model name. Lets default to ptx0/terminus-xl-gamma-training."
            )
            return ("1280x768", "ptx0/terminus-xl-gamma-training")

        return (resolution, model_name)

    async def discord_bot_response(self, prompt, ctx=None):
        user_role = self.discord_bot_role
        user_temperature = self.temperature
        if ctx is not None:
            user_role = self.config.get_user_setting(
                ctx.author.id, "gpt_role", self.discord_bot_role
            )
            user_temperature = self.config.get_user_setting(
                ctx.author.id, "temperature", self.temperature
            )
        return await self.turbo_completion(
            user_role, prompt, temperature=user_temperature, max_tokens=4096
        )

    @classmethod
    def _ensure_cli_workdir(cls) -> str:
        os.makedirs(cls._CLI_WORKDIR, exist_ok=True)
        return cls._CLI_WORKDIR

    def _normalize_backend(self) -> str:
        return self.config.normalize_zork_backend(getattr(self, "backend", "zai"))

    def _resolve_cli_model(self, backend: str) -> str | None:
        raw_model = str(getattr(self, "engine", "") or "").strip()
        if backend == "opencode":
            if not raw_model or raw_model in {"o3-mini", self._ZAI_MODEL}:
                return "opencode/gpt-5-nano"
            return raw_model
        if not raw_model or raw_model in {"o3-mini", self._ZAI_MODEL, "text-davinci-003"}:
            return None
        return raw_model

    def _send_zai_request(self, message_log):
        client = OpenAI(
            api_key=config.get_openai_api_key(),
            base_url="https://api.z.ai/api/coding/paas/v4",
        )
        return client.chat.completions.create(
            model=self._ZAI_MODEL,
            messages=message_log,
            max_completion_tokens=self.max_tokens,
            temperature=self.temperature,
            extra_body={
                "thinking": {
                    "type": "enabled",
                },
            },
        )

    @classmethod
    def _build_cli_prompt(cls, role: str, prompt: str) -> str:
        return cls._build_structured_user_prompt(prompt)

    @classmethod
    def _build_structured_system_instructions(cls, role: str) -> str:
        role_text = str(role or "").strip()
        parts = [cls._TEXT_COMPLETION_INSTRUCTIONS]
        parts.append(
            "<output_contract>\n"
            "- Follow the SYSTEM_INSTRUCTIONS block exactly.\n"
            "- Return only the answer requested by the prompt.\n"
            "- If the prompt requires a strict format, output only that format.\n"
            "</output_contract>"
        )
        parts.append(
            "<verbosity_controls>\n"
            "- Prefer concise, information-dense writing.\n"
            "- Avoid repeating the user's request.\n"
            "</verbosity_controls>"
        )
        parts.append(
            "<tool_boundary>\n"
            "- This call is a text-completion request, not an autonomous coding task.\n"
            "- Do not claim to inspect files, run commands, or use tools unless the prompt explicitly requires it.\n"
            "</tool_boundary>"
        )
        lower_role = role_text.lower()
        if role_text and (
            "json" in lower_role
            or "reasoning" in lower_role
            or "first key" in lower_role
        ):
            parts.append(
                "<structured_output_contract>\n"
                "- If SYSTEM_INSTRUCTIONS requires JSON, output exactly one JSON object and nothing else.\n"
                "- Never omit a required key just because it feels internal.\n"
                "- If SYSTEM_INSTRUCTIONS requires a reasoning field, include reasoning in every final JSON response.\n"
                "- If SYSTEM_INSTRUCTIONS specifies key order, preserve that order in the final JSON.\n"
                "</structured_output_contract>"
            )
        if role_text:
            parts.append(f"<system_instructions>\n{role_text}\n</system_instructions>")
        return "\n\n".join(part.strip() for part in parts if part.strip()).strip()

    @classmethod
    def _build_claude_structured_system_instructions(cls, role: str) -> str:
        role_text = str(role or "").strip()
        parts = [cls._TEXT_COMPLETION_INSTRUCTIONS]
        parts.append(
            "<output_contract>\n"
            "- Follow the SYSTEM_INSTRUCTIONS block exactly.\n"
            "- Return only the answer requested by the prompt.\n"
            "- If the prompt requires a strict format, output only that format.\n"
            "</output_contract>"
        )
        parts.append(
            "<verbosity_controls>\n"
            "- Prefer concise, information-dense writing.\n"
            "- Avoid repeating the user's request.\n"
            "</verbosity_controls>"
        )
        parts.append(
            "<tool_boundary>\n"
            "- This call is a text-completion request, not an autonomous coding task.\n"
            "- Do not claim to inspect files, run commands, or use tools unless the prompt explicitly requires it.\n"
            "</tool_boundary>"
        )
        lower_role = role_text.lower()
        if role_text and (
            "json" in lower_role
            or "reasoning" in lower_role
            or "first key" in lower_role
        ):
            parts.append(
                "<structured_output_contract>\n"
                "- If SYSTEM_INSTRUCTIONS requires JSON, output exactly one JSON object and nothing else.\n"
                "- Never omit a required key just because it feels internal.\n"
                "- If SYSTEM_INSTRUCTIONS requires a reasoning field, include reasoning in every final JSON response.\n"
                "- If SYSTEM_INSTRUCTIONS specifies key order, preserve that order in the final JSON.\n"
                "</structured_output_contract>"
            )
        if role_text:
            parts.append(
                f"<system_instructions>\n{cls._wrap_examples_for_claude(role_text)}\n</system_instructions>"
            )
        return "\n\n".join(part.strip() for part in parts if part.strip()).strip()

    @staticmethod
    def _build_structured_user_prompt(prompt: str) -> str:
        user_text = str(prompt or "").strip()
        return f"<user_request>\n{user_text}\n</user_request>".strip()

    @classmethod
    def _build_claude_structured_user_prompt(cls, prompt: str) -> str:
        user_text = str(prompt or "").strip()
        wrapped = cls._wrap_prompt_sections_as_xml(user_text)
        return f"<user_request>\n{wrapped}\n</user_request>".strip()

    @classmethod
    def _wrap_prompt_sections_as_xml(cls, text: str) -> str:
        raw_lines = str(text or "").splitlines()
        if not raw_lines:
            return ""
        blocks = []
        current_tag = "free_text"
        current_lines = []

        def flush():
            nonlocal current_tag, current_lines
            if current_lines:
                blocks.append((current_tag, current_lines[:]))
            current_tag = "free_text"
            current_lines = []

        for line in raw_lines:
            match = _PROMPT_SECTION_RE.match(line)
            if match:
                flush()
                current_tag = cls._section_tag_name(match.group(1))
                first_line = str(match.group(2) or "").strip()
                current_lines = [first_line] if first_line else []
                continue
            current_lines.append(line)
        flush()

        if not blocks:
            return str(text or "").strip()

        out = []
        for tag, lines in blocks:
            content = cls._wrap_examples_for_claude("\n".join(lines).strip())
            if not content:
                out.append(f"<{tag} />")
                continue
            out.append(f"<{tag}>")
            out.append(content)
            out.append(f"</{tag}>")
        return "\n".join(out).strip()

    @staticmethod
    def _section_tag_name(key: str) -> str:
        text = re.sub(r"[^a-z0-9]+", "_", str(key or "").strip().lower()).strip("_")
        return text or "section"

    @staticmethod
    def _wrap_examples_for_claude(text: str) -> str:
        lines = str(text or "").splitlines()
        if not lines:
            return ""
        out = []
        example_buf = []

        def flush():
            if not example_buf:
                return
            content = "\n".join(example_buf).strip()
            if content:
                out.append("<example>")
                out.append(content)
                out.append("</example>")
            example_buf.clear()

        for raw_line in lines:
            line = str(raw_line or "")
            if GPT._is_claude_example_line(line):
                example_buf.append(line)
                continue
            flush()
            out.append(line)
        flush()
        return "\n".join(out).strip()

    @staticmethod
    def _is_claude_example_line(line: str) -> bool:
        text = str(line or "").strip()
        if not text:
            return False
        if text.startswith(("Example:", "Examples:", "NOT:", "Output format:")):
            return True
        if text.startswith("{") and text.endswith("}"):
            return True
        if text.startswith('{"tool_call"'):
            return True
        return False

    @staticmethod
    def _extract_last_json_object(text: str):
        lines = str(text or "").splitlines()
        for idx in range(len(lines) - 1, -1, -1):
            candidate = "\n".join(lines[idx:]).strip()
            if not candidate.startswith("{"):
                continue
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        return None

    @staticmethod
    def _extract_jsonl_objects(text: str) -> list[dict]:
        objects = []
        for raw_line in str(text or "").splitlines():
            line = raw_line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                objects.append(payload)
        return objects

    def _run_codex_cli(self, role: str, prompt: str) -> str:
        workdir = self._ensure_cli_workdir()
        user_instructions = self._build_structured_system_instructions(role)
        command = [
            "codex",
            "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "-C",
            workdir,
            "-s",
            "read-only",
            "-c",
            f"user_instructions={json.dumps(user_instructions)}",
        ]
        model = self._resolve_cli_model("codex")
        if model:
            command.extend(["-m", model])
        result = subprocess.run(
            command,
            input=self._build_structured_user_prompt(prompt),
            text=True,
            capture_output=True,
            timeout=self._CLI_TIMEOUT_SECONDS,
            check=False,
        )
        events = self._extract_jsonl_objects(result.stdout)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
            raise RuntimeError(f"Codex CLI request failed: {detail}")
        messages = []
        for event in events:
            if str(event.get("type") or "").strip() != "item.completed":
                continue
            item = event.get("item")
            if isinstance(item, dict) and str(item.get("type") or "").strip() == "agent_message":
                text = str(item.get("text") or "").strip()
                if text:
                    messages.append(text)
        return messages[-1] if messages else result.stdout.strip()

    def _run_claude_cli(self, role: str, prompt: str) -> str:
        workdir = self._ensure_cli_workdir()
        command = [
            "claude",
            "-p",
            self._build_claude_structured_user_prompt(prompt),
            "--output-format",
            "json",
            "--permission-mode",
            "dontAsk",
            "--tools",
            "",
        ]
        model = self._resolve_cli_model("claude")
        if model:
            command.extend(["--model", model])
        if str(role or "").strip():
            command.extend(
                ["--system-prompt", self._build_claude_structured_system_instructions(role)]
            )
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=self._CLI_TIMEOUT_SECONDS,
            cwd=workdir,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
            raise RuntimeError(f"Claude CLI request failed: {detail}")
        payload = self._extract_last_json_object(result.stdout)
        if not isinstance(payload, dict):
            raise RuntimeError("Claude CLI returned no JSON result payload")
        if payload.get("is_error") is True:
            raise RuntimeError(f"Claude CLI request failed: {payload.get('result') or payload}")
        return str(payload.get("result") or "").strip()

    def _run_gemini_cli(self, role: str, prompt: str) -> str:
        workdir = self._ensure_cli_workdir()
        command = ["gemini", "-o", "json"]
        model = self._resolve_cli_model("gemini")
        if model:
            command.extend(["-m", model])
        structured_prompt = (
            f"{self._build_structured_system_instructions(role)}\n\n"
            f"{self._build_structured_user_prompt(prompt)}"
        )
        command.append(structured_prompt)
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=self._CLI_TIMEOUT_SECONDS,
            cwd=workdir,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
            raise RuntimeError(f"Gemini CLI request failed: {detail}")
        payload = self._extract_last_json_object(result.stdout)
        if not isinstance(payload, dict):
            raise RuntimeError("Gemini CLI returned no JSON response payload")
        return str(payload.get("response") or "").strip()

    def _run_opencode_cli(self, role: str, prompt: str) -> str:
        workdir = self._ensure_cli_workdir()
        command = ["opencode", "run", "--format", "json"]
        model = self._resolve_cli_model("opencode")
        if model:
            command.extend(["-m", model])
        structured_prompt = (
            f"{self._build_structured_system_instructions(role)}\n\n"
            f"{self._build_structured_user_prompt(prompt)}"
        )
        command.append(structured_prompt)
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=self._CLI_TIMEOUT_SECONDS,
            cwd=workdir,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
            raise RuntimeError(f"OpenCode CLI request failed: {detail}")
        events = self._extract_jsonl_objects(result.stdout)
        texts = []
        for event in events:
            if str(event.get("type") or "").strip() != "text":
                continue
            part = event.get("part")
            if isinstance(part, dict):
                text = str(part.get("text") or "").strip()
                if text:
                    texts.append(text)
        return "\n".join(texts).strip() or result.stdout.strip()

    def _run_cli_backend(self, backend: str, role: str, prompt: str) -> str:
        if backend == "codex":
            return self._run_codex_cli(role, prompt)
        if backend == "claude":
            return self._run_claude_cli(role, prompt)
        if backend == "gemini":
            return self._run_gemini_cli(role, prompt)
        if backend == "opencode":
            return self._run_opencode_cli(role, prompt)
        raise ValueError(f"Unsupported GPT backend: {backend}")

    async def turbo_completion(self, role, prompt, **kwargs):
        if kwargs:
            self.set_values(**kwargs)

        backend = self._normalize_backend()
        effective_role = str(role or "")
        effective_prompt = str(prompt or "")
        if backend != "zai" and not effective_prompt.strip() and effective_role.strip():
            effective_prompt = effective_role.strip()
            effective_role = ""
        semaphore = _get_backend_semaphore(backend)
        async with semaphore:
            if backend == "zai":
                message_log = [
                    {"role": "assistant", "content": effective_role},
                    {"role": "user", "content": effective_prompt},
                ]
                try:
                    response = await asyncio.to_thread(
                        self._send_zai_request, message_log
                    )
                except Exception as e:
                    logger.error(f"Error sending request to ZAI: {e}")
                    return None
                if response is None:
                    return None
                for choice in response.choices:
                    if "text" in choice:
                        return choice.text
                return response.choices[0].message.content

            try:
                return await asyncio.to_thread(
                    self._run_cli_backend, backend, effective_role, effective_prompt
                )
            except Exception as e:
                logger.error(f"Error sending request to {backend}: {e}")
                return None

    def retrieve_image(self, url: str):
        import requests

        response = requests.get(url)
        content = response.content

        from PIL import Image
        from io import BytesIO

        return Image.open(BytesIO(content)), content

    def dalle_image_generate(self, prompt, user_config: dict):
        resolution = (
            f"{user_config.get('width', 1024)}x{user_config.get('height', 1024)}"
        )
        try:
            response = openai.images.generate(
                model="dall-e-3",
                prompt=f"I NEED to test how the tool works with extremely simple prompts. DO NOT add any detail, just use it AS-IS: {prompt}",
                size=resolution,
                quality="standard",
                n=1,
            )
            logger.setLevel(config.get_log_level())
            if "error" in response:
                logger.error("API returned error result, returning black image")
                from PIL import Image

                image = Image.new(
                    "RGB",
                    (user_config.get("width", 1024), user_config.get("height", 1024)),
                    (0, 0, 0),
                )
                return image

            logger.debug(
                f"Received response from OpenAI image endpoint: {response}"
            )
            url = response.data[0].url
            logger.debug(f"Retrieving URL: {url}")
            image_obj, image_data = self.retrieve_image(url)
            logger.debug(f"Result: {image_obj}")
            if not hasattr(image_obj, "size"):
                logger.error(
                    "Image object does not have a size attribute. Returning None."
                )
                logger.debug(f"Response from OpenAI: {response}")
                return None
            logger.debug("Returning image_data from dalle")
            return image_data
        except Exception as e:
            logger.setLevel(config.get_log_level())
            logger.error(
                f"Exception while generating image, generating black image for result: {e}"
            )
            from PIL import Image

            image = Image.new(
                "RGB",
                (user_config.get("width", 1024), user_config.get("height", 1024)),
                (0, 0, 0),
            )
            return image
