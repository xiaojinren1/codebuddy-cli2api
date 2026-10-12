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
                "medium": "medium",
                "high": "high",
                "xhigh": "max",
                "max": "max",
            },
        )
        self.assertIn("glm-5.3-flash", models)
        self.assertIn("image", models["glm-5.3-flash"].get("input") or [])
        # deepseek honours max upstream; the template must not collapse it to high.
        for mid in ("deepseek-v4-pro", "deepseek-v4-flash"):
            self.assertEqual(models[mid]["thinkingLevelMap"]["max"], "max", mid)
            self.assertEqual(models[mid]["thinkingLevelMap"]["xhigh"], "max", mid)
        self.assertFalse(models["kimi-k3-2"]["reasoning"])
        self.assertEqual(models["kimi-k3-2"]["samplingParams"]["reasoning_effort"], "none")


if __name__ == "__main__":
    unittest.main()
