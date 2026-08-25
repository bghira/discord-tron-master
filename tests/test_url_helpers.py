import unittest

from discord_tron_master.classes.discord.url_helpers import (
    find_urls,
    is_direct_image_url,
    remove_url,
)


class URLHelperTests(unittest.TestCase):
    def test_github_repository_url_is_preserved(self):
        prompt = (
            "explain https://github.com/SimpleTuner-io/huggingface-hub-rvc "
            "and its functionality"
        )

        urls = find_urls(prompt)

        self.assertEqual(
            urls,
            ["https://github.com/SimpleTuner-io/huggingface-hub-rvc"],
        )
        self.assertFalse(is_direct_image_url(urls[0]))
        self.assertIn(urls[0], prompt)

    def test_direct_image_url_is_detected_with_query_string(self):
        url = "https://example.com/reference.PNG?width=1024"

        self.assertTrue(is_direct_image_url(url))

    def test_only_consumed_image_url_is_removed(self):
        image_url = "https://example.com/reference.png"
        repo_url = "https://github.com/example/project"
        prompt = f"compare <{image_url}> with {repo_url}"

        cleaned = remove_url(prompt, image_url)

        self.assertNotIn(image_url, cleaned)
        self.assertIn(repo_url, cleaned)


if __name__ == "__main__":
    unittest.main()
