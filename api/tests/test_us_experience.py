import unittest

from app.apps import infer_agent_blueprint
from app.config import settings
from app.model_gateway import DEFAULT_MODEL, list_providers, resolve_provider


class USExperienceTests(unittest.TestCase):
    def test_openai_is_the_primary_default_provider(self):
        providers = list_providers()
        self.assertEqual(providers["default"], settings.openai_model)
        self.assertEqual(providers["providers"][0]["id"], "openai")
        self.assertEqual(DEFAULT_MODEL, settings.openai_model)

    def test_unknown_model_falls_back_to_openai(self):
        base_url, api_key, model = resolve_provider(None)
        self.assertEqual(base_url, settings.openai_base_url)
        self.assertEqual(api_key, settings.openai_api_key)
        self.assertEqual(model, settings.openai_model)

    def test_english_support_request_builds_useful_blueprint(self):
        blueprint = infer_agent_blueprint(
            "Build a customer support agent that answers from our knowledge base and summarizes tickets"
        )
        self.assertEqual(blueprint["name"], "Customer support Agent")
        self.assertEqual(blueprint["domain"], "Customer support and ticket resolution")
        self.assertIn("customer-service", blueprint["skills"])
        self.assertIn("ticket-summary", blueprint["skills"])
        self.assertTrue(blueprint["prompt"].startswith("You are"))

    def test_github_request_enables_github_connector(self):
        blueprint = infer_agent_blueprint("Create an engineering agent for GitHub pull request review")
        self.assertIn("github", blueprint["connectors"])
        self.assertIn("pull-request-review", blueprint["skills"])


if __name__ == "__main__":
    unittest.main()
