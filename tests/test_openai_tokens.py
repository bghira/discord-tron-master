import unittest
from unittest.mock import Mock, patch

from discord_tron_master.classes.openai.tokens import TokenTester, glm_token_count


class GLMTokenTesterTests(unittest.TestCase):
    def test_token_tester_uses_glm_tokenizer(self):
        tokenizer = Mock()
        tokenizer.encode.return_value = [10, 20, 30]

        with patch(
            "discord_tron_master.classes.openai.tokens._get_glm_tokenizer",
            return_value=tokenizer,
        ):
            tester = TokenTester(engine="glm")
            self.assertEqual(tester.get_token_count("hello"), 3)

        tokenizer.encode.assert_called_once_with("hello")

    def test_glm_token_count_uses_same_tokenizer_path(self):
        tokenizer = Mock()
        tokenizer.encode.return_value = [1, 2]

        with patch(
            "discord_tron_master.classes.openai.tokens._get_glm_tokenizer",
            return_value=tokenizer,
        ):
            self.assertEqual(glm_token_count("hello"), 2)


if __name__ == "__main__":
    unittest.main()
