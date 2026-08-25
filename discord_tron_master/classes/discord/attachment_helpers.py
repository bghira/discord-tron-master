"""Turn Discord file attachments into bounded LLM prompt context."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence

MAX_ATTACHMENT_BYTES = 500_000
MAX_TOTAL_ATTACHMENT_BYTES = 600_000

_TEXT_EXTENSIONS = {
    ".c",
    ".cfg",
    ".conf",
    ".cpp",
    ".css",
    ".csv",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".log",
    ".md",
    ".py",
    ".rs",
    ".rst",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsv",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_TEXT_CONTENT_TYPES = {
    "application/javascript",
    "application/json",
    "application/sql",
    "application/toml",
    "application/x-httpd-php",
    "application/x-javascript",
    "application/x-sh",
    "application/x-yaml",
    "application/xml",
}


def is_image_attachment(attachment: object) -> bool:
    content_type = str(getattr(attachment, "content_type", "") or "").lower()
    return content_type.startswith("image/")


def is_text_attachment(attachment: object) -> bool:
    content_type = str(getattr(attachment, "content_type", "") or "").lower()
    if content_type.startswith("text/") or content_type in _TEXT_CONTENT_TYPES:
        return True
    filename = str(getattr(attachment, "filename", "") or "")
    return os.path.splitext(filename.lower())[1] in _TEXT_EXTENSIONS


def _attachment_metadata(attachment: object) -> str:
    return json.dumps(
        {
            "filename": str(getattr(attachment, "filename", "") or "attachment"),
            "content_type": str(
                getattr(attachment, "content_type", "") or "application/octet-stream"
            ),
            "size": getattr(attachment, "size", None),
            "url": str(getattr(attachment, "url", "") or ""),
        },
        ensure_ascii=False,
    )


async def build_attachment_context(
    attachments: Sequence[object] | None,
    *,
    max_attachment_bytes: int = MAX_ATTACHMENT_BYTES,
    max_total_bytes: int = MAX_TOTAL_ATTACHMENT_BYTES,
) -> str:
    """Return direct text plus metadata/URLs for unsupported Discord files."""
    sections: list[str] = []
    total_bytes = 0
    for attachment in attachments or []:
        if is_image_attachment(attachment):
            continue
        metadata = _attachment_metadata(attachment)
        if not is_text_attachment(attachment):
            sections.append(
                "ATTACHMENT_REFERENCE "
                f"{metadata}\nBinary content was not decoded. Use the supplied URL with reading tools "
                "if the format is supported."
            )
            continue

        declared_size = getattr(attachment, "size", None)
        if isinstance(declared_size, int) and declared_size > max_attachment_bytes:
            sections.append(
                f"ATTACHMENT_REFERENCE {metadata}\nAttachment exceeds the direct-read limit "
                f"of {max_attachment_bytes} bytes."
            )
            continue
        if total_bytes >= max_total_bytes:
            sections.append(
                f"ATTACHMENT_REFERENCE {metadata}\nSkipped because the combined attachment "
                f"limit is {max_total_bytes} bytes."
            )
            continue

        try:
            raw = await attachment.read()
        except Exception as exc:
            sections.append(
                f"ATTACHMENT_REFERENCE {metadata}\nDirect read failed: "
                f"{type(exc).__name__}. The supplied URL may still be readable with tools."
            )
            continue
        if not isinstance(raw, bytes):
            raw = bytes(raw or b"")
        remaining = max_total_bytes - total_bytes
        raw = raw[: min(max_attachment_bytes, remaining)]
        total_bytes += len(raw)
        if b"\x00" in raw:
            sections.append(
                f"ATTACHMENT_REFERENCE {metadata}\nThe file appears binary; direct text decoding "
                "was skipped. Use the supplied URL with reading tools if supported."
            )
            continue
        text = raw.decode("utf-8-sig", errors="replace").replace(
            "</attachment_text>", "<\\/attachment_text>"
        )
        sections.append(
            f"ATTACHMENT_CONTENT {metadata}\n"
            "<attachment_text>\n"
            f"{text}\n"
            "</attachment_text>"
        )
    return "\n\n".join(sections).strip()
