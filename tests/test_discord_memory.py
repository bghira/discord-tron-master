import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

from discord_tron_master.classes.discord_memory import DiscordMemory, _embed


def fake_embed(text: str, *, query: bool = False) -> bytes:
    value = str(text or "").lower()
    vector = np.zeros(384, dtype=np.float32)
    if "blue widget" in value:
        vector[0] = 1.0
    elif "garden" in value:
        vector[1] = 1.0
    else:
        vector[2] = 1.0
    return vector.tobytes()


class DiscordMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        DiscordMemory.configure(
            db_path=os.path.join(self.temp_dir.name, "discord-memory.db")
        )

    def tearDown(self):
        DiscordMemory.configure(db_path=None)
        self.temp_dir.cleanup()

    def test_store_search_scope_and_delete(self):
        with patch(
            "discord_tron_master.classes.discord_memory._embed",
            side_effect=fake_embed,
        ):
            DiscordMemory.store_exchange(
                conversation_id=10,
                user_message_id=1001,
                assistant_message_id=1002,
                author_id=20,
                author_name="Kash",
                user_text="What color was the blue widget?",
                assistant_text="Cobalt blue.",
            )
            DiscordMemory.store_exchange(
                conversation_id=11,
                user_message_id=2001,
                author_name="Someone else",
                user_text="Where is the blue widget?",
                assistant_text="Private scope.",
            )

            hits = DiscordMemory.search(
                conversation_id=10,
                queries=["blue widget color"],
                top_k=5,
            )

        self.assertEqual(len(hits), 1)
        self.assertIn("Cobalt blue", hits[0]["content"])
        self.assertNotIn("Private scope", hits[0]["content"])
        self.assertEqual(DiscordMemory.delete_conversation(10), 1)
        with patch(
            "discord_tron_master.classes.discord_memory._embed",
            side_effect=fake_embed,
        ):
            self.assertEqual(
                DiscordMemory.search(
                    conversation_id=10,
                    queries=["blue widget"],
                ),
                [],
            )

    def test_snowflake_query_prefix_is_used_only_for_queries(self):
        model = Mock()
        model.encode.return_value = np.zeros(384, dtype=np.float32)
        with patch(
            "discord_tron_master.classes.discord_memory._get_model",
            return_value=model,
        ):
            _embed("old conversation", query=True)
            query_text = model.encode.call_args.args[0]
            _embed("stored conversation", query=False)
            document_text = model.encode.call_args.args[0]

        self.assertTrue(
            query_text.startswith(
                "Represent this sentence for searching relevant passages: "
            )
        )
        self.assertEqual(document_text, "stored conversation")


if __name__ == "__main__":
    unittest.main()
