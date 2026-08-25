import re
from urllib.parse import urlsplit


URL_RE = re.compile(r"(?:<|\(|\[)?(https?://[^\s<>\)\]]+)(?:>|\)|\])?")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
BARE_URL_RE = re.compile(r"(?<!<)(https?://[^\s<>\)\]]+)")
TRAILING_URL_PUNCTUATION = ".,!?;:"


def find_urls(text: str) -> list[str]:
    return URL_RE.findall(str(text or ""))


def is_direct_image_url(url: str) -> bool:
    return urlsplit(str(url or "")).path.lower().endswith(IMAGE_SUFFIXES)


def remove_url(text: str, url: str) -> str:
    """Remove one consumed URL and its simple Discord wrapper from a prompt."""
    value = str(text or "")
    for candidate in (f"<{url}>", f"({url})", f"[{url}]", url):
        if candidate in value:
            return value.replace(candidate, "", 1).strip()
    return value.strip()


def suppress_url_embeds(text: str) -> str:
    """Wrap outbound URLs in angle brackets so Discord does not embed them."""

    def wrap(match: re.Match) -> str:
        url = match.group(1)
        trimmed = url.rstrip(TRAILING_URL_PUNCTUATION)
        punctuation = url[len(trimmed) :]
        return f"<{trimmed}>{punctuation}"

    return BARE_URL_RE.sub(wrap, str(text or ""))
