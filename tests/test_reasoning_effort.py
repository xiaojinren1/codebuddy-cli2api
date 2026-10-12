"""apply_reasoning_effort: omit = deep CoT, low disables, max/xhigh passthrough."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from codebuddy_gateway.common import GatewayError
from codebuddy_gateway.upstream import apply_reasoning_effort


class ApplyReasoningEffortTests(unittest.TestCase):
    def test_omit_injects_max(self):
        # deepseek-v4-* emit no CoT unless an effort is present, so the gateway
        # must inject one; ``max`` is the only value both families think on.
        body = apply_reasoning_effort({"messages": []})
        self.assertEqual(body["reasoning_effort"], "max")

    def test_low_passthrough(self):
        self.assertEqual(apply_reasoning_effort({"reasoning_effort": "low"})["reasoning_effort"], "low")

    def test_medium_and_high_passthrough(self):
        self.assertEqual(apply_reasoning_effort({"reasoning_effort": "medium"})["reasoning_effort"], "medium")
        self.assertEqual(apply_reasoning_effort({"reasoning_effort": "high"})["reasoning_effort"], "high")

    def test_max_aliases(self):
        for token in ("max", "xhigh", "ultra"):
            self.assertEqual(apply_reasoning_effort({"reasoning_effort": token})["reasoning_effort"], "max")

    def test_off_becomes_low(self):
        for token in ("off", "none", "false", "minimal"):
            self.assertEqual(apply_reasoning_effort({"reasoning_effort": token})["reasoning_effort"], "low")

    def test_enable_thinking_false_disables(self):
        self.assertEqual(apply_reasoning_effort({"enable_thinking": False})["reasoning_effort"], "low")

    def test_explicit_wins_over_env(self):
        with patch.dict(os.environ, {"CODEBUDDY_REASONING_EFFORT": "low"}):
            self.assertEqual(apply_reasoning_effort({"reasoning_effort": "max"})["reasoning_effort"], "max")

    def test_env_fills_when_omitted(self):
        with patch.dict(os.environ, {"CODEBUDDY_REASONING_EFFORT": "max"}):
            self.assertEqual(apply_reasoning_effort({})["reasoning_effort"], "max")

    def test_empty_env_does_not_inject(self):
        with patch.dict(os.environ, {"CODEBUDDY_REASONING_EFFORT": ""}, clear=False):
            os.environ["CODEBUDDY_REASONING_EFFORT"] = ""
            self.assertNotIn("reasoning_effort", apply_reasoning_effort({}))

    def test_null_effort_rejected(self):
        with self.assertRaises(GatewayError):
            apply_reasoning_effort({"reasoning_effort": None})

    def test_unknown_effort_rejected(self):
        with self.assertRaises(GatewayError):
            apply_reasoning_effort({"reasoning_effort": "banana"})


class CatalogFlashTests(unittest.TestCase):
    def test_flash_is_in_fallback_and_overlay(self):
        from codebuddy_gateway.common import MODELS, _merge_catalog_extras
        self.assertTrue(any(m["id"] == "glm-5.3-flash" and m["img"] for m in MODELS))
        merged = _merge_catalog_extras([{"id": "glm-5.3", "img": False}])
        flash = next(m for m in merged if m["id"] == "glm-5.3-flash")
        self.assertTrue(flash["img"])
        already = _merge_catalog_extras([{"id": "glm-5.3-flash", "img": True}])
        self.assertEqual(sum(1 for m in already if m["id"] == "glm-5.3-flash"), 1)
    def test_glm53_does_not_call_kimi_proxy(self):
        from codebuddy_gateway.augment import inject_vision_descriptions
        body = {"model": "glm-5.3", "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}},
        ]}]}
        out = inject_vision_descriptions(body, "ck_x")
        self.assertIs(out, body)





if __name__ == "__main__":
    unittest.main()
