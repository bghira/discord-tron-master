import logging
import tiktoken

logger = logging.getLogger(__name__)


class TokenTester:
    def __init__(self, engine: str = "gpt-3.5-turbo-0613"):
        self.tokenizer = tiktoken.encoding_for_model(engine)

    def tokenize(self, text):
        return self.tokenizer.encode(text, allowed_special="all")

    def get_token_count(self, text):
        tokens = self.tokenize(text)
        return len(tokens)


# ── GLM tokenizer (lazy-loaded, CPU-only) ──────────────────────────────

_glm_tokenizer = None
_GLM_MODEL_ID = "zai-org/GLM-5"


def _get_glm_tokenizer():
    """Return the cached GLM tokenizer, loading on first call."""
    global _glm_tokenizer
    if _glm_tokenizer is None:
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
    tok = _get_glm_tokenizer()
    if tok is None:
        return len(text) // 4
    return len(tok.encode(text))
