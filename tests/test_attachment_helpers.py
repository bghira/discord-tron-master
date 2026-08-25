import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from discord_tron_master.classes.discord.attachment_helpers import (
    build_attachment_context,
    is_image_attachment,
    is_text_attachment,
)


class AttachmentHelperTests(unittest.TestCase):
    def test_text_attachment_is_read_into_context(self):
        attachment = SimpleNamespace(
            filename="notes.md",
            content_type="text/markdown",
            size=28,
            url="https://cdn.discord.test/notes.md",
            read=AsyncMock(return_value=b"# Plan\nUse the cobalt widget."),
        )

        context = asyncio.run(build_attachment_context([attachment]))

        self.assertIn('"filename": "notes.md"', context)
        self.assertIn("# Plan\nUse the cobalt widget.", context)
        self.assertIn("<attachment_text>", context)
        attachment.read.assert_awaited_once()

    def test_code_extension_is_read_when_content_type_is_missing(self):
        attachment = SimpleNamespace(
            filename="worker.py",
            content_type=None,
            size=12,
            url="https://cdn.discord.test/worker.py",
            read=AsyncMock(return_value=b"print('ok')"),
        )

        self.assertTrue(is_text_attachment(attachment))
        context = asyncio.run(build_attachment_context([attachment]))
        self.assertIn("print('ok')", context)

    def test_binary_attachment_keeps_metadata_and_url(self):
        attachment = SimpleNamespace(
            filename="paper.pdf",
            content_type="application/pdf",
            size=100,
            url="https://cdn.discord.test/paper.pdf",
            read=AsyncMock(),
        )

        context = asyncio.run(build_attachment_context([attachment]))

        self.assertIn("ATTACHMENT_REFERENCE", context)
        self.assertIn("https://cdn.discord.test/paper.pdf", context)
        attachment.read.assert_not_awaited()

    def test_images_are_left_for_existing_image_workflow(self):
        attachment = SimpleNamespace(
            filename="cat.png",
            content_type="image/png",
            size=100,
            url="https://cdn.discord.test/cat.png",
            read=AsyncMock(),
        )

        self.assertTrue(is_image_attachment(attachment))
        self.assertEqual(asyncio.run(build_attachment_context([attachment])), "")
        attachment.read.assert_not_awaited()

    def test_oversized_text_attachment_is_not_downloaded(self):
        attachment = SimpleNamespace(
            filename="huge.txt",
            content_type="text/plain",
            size=101,
            url="https://cdn.discord.test/huge.txt",
            read=AsyncMock(),
        )

        context = asyncio.run(
            build_attachment_context([attachment], max_attachment_bytes=100)
        )

        self.assertIn("exceeds the direct-read limit", context)
        attachment.read.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
