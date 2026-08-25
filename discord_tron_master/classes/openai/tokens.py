import logging

logger = logging.getLogger(__name__)


class TokenTester:
    def __init__(self, engine: str = "gpt-3.5-turbo-0613"):
        self.engine = str(engine or "").strip().lower()
        self.tokenizer = None
        if self.engine != "glm":
            import tiktoken

            self.tokenizer = tiktoken.encoding_for_model(engine)

    def tokenize(self, text):
        if self.engine == "glm":
            tokenizer = _get_glm_tokenizer()
            if tokenizer is None:
                return [0] * (len(str(text or "")) // 4)
            return tokenizer.encode(str(text or ""))
        return self.tokenizer.encode(text, allowed_special="all")

    def get_token_count(self, text):
        tokens = self.tokenize(text)
        return len(tokens)


# ── GLM tokenizer (lazy-loaded, CPU-only) ──────────────────────────────

_glm_tokenizer = None
_glm_tokenizer_load_attempted = False
_GLM_MODEL_ID = "zai-org/GLM-5"


def _get_glm_tokenizer():
    """Return the cached GLM tokenizer, loading on first call."""
    global _glm_tokenizer, _glm_tokenizer_load_attempted
    if _glm_tokenizer is None:
        if _glm_tokenizer_load_attempted:
            return None
        _glm_tokenizer_load_attempted = True
        try:
            from transformers import AutoTokenizer

            _glm_tokenizer = AutoTokenizer.from_pretrained(
                _GLM_MODEL_ID, trust_remote_code=True
            )
            logger.info("GLM tokenizer loaded from %s", _GLM_MODEL_ID)
        except Exception as e:
            logger.warning("Failed to load GLM tokenizer: %s", e)
    return _glm_tokenizer


def glm_token_count(text: str) -> int:
    """Return the token count for *text* using the GLM tokenizer.
    Falls back to len(text) // 4 if the tokenizer is unavailable."""
    return TokenTester(engine="glm").get_token_count(text)
