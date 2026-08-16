import json
from pathlib import Path
import unittest


class PiConfigTemplateTests(unittest.TestCase):
    def test_provider_uses_gateway_and_maps_supported_reasoning_levels(self):
        path = Path(__file__).parents[1] / "pi-models.example.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        provider = config["providers"]["codebuddy-local"]
        self.assertEqual(provider["baseUrl"], "http://127.0.0.1:8787/v1")
        self.assertEqual(provider["api"], "openai-completions")

        models = {model["id"]: model for model in provider["models"]}
        self.assertEqual(
            models["glm-5.3"]["thinkingLevelMap"],
            {
                "off": "none",
                "minimal": "low",
                "low": "low",
                "medium": "high",
                "high": "high",
                "xhigh": "high",
                "max": "high",
            },
        )
        self.assertFalse(models["kimi-k3-2"]["reasoning"])
        self.assertEqual(models["kimi-k3-2"]["samplingParams"]["reasoning_effort"], "none")


if __name__ == "__main__":
    unittest.main()
