import json
import os
import unittest
from unittest.mock import patch

from codebuddy_gateway.common import _norm_model, model_catalog_json, models_json
from codebuddy_gateway.common import GatewayError
from codebuddy_gateway.formats.codex import emit_nonstream, emit_stream, map_reasoning_effort, parse
from codebuddy_gateway.upstream import stream_codebuddy


class ReasoningMappingTests(unittest.TestCase):
    def test_effort_mapping(self):
        for value in ("low",):
            self.assertEqual(map_reasoning_effort(value), "low")
        self.assertEqual(map_reasoning_effort("medium"), "medium")
        self.assertEqual(map_reasoning_effort("high"), "high")
        for value in ("xhigh", "max", "ultra"):
            self.assertEqual(map_reasoning_effort(value), "max")
        for value in ("none", "minimal", "off"):
            self.assertEqual(map_reasoning_effort(value), "none")
        with self.assertRaises(GatewayError):
            map_reasoning_effort("unsupported")

    def test_nested_and_top_level_effort(self):
        self.assertEqual(parse({"input": "x", "reasoning": {"effort": "xhigh"}}, {}, "")["reasoning_effort"], "max")
        self.assertEqual(parse({"input": "x", "reasoning_effort": "low"}, {}, "")["reasoning_effort"], "low")
        self.assertEqual(parse({"input": "x", "reasoning": {"effort": "off"}}, {}, "")["reasoning_effort"], "none")
        with self.assertRaises(GatewayError):
            parse({"input": "x", "reasoning": {"effort": "bogus"}}, {}, "")


class ModelCatalogTests(unittest.TestCase):
    def test_normalization_preserves_capabilities(self):
        model = _norm_model({
            "slug": "demo", "displayName": "Demo", "descriptionZh": "中文说明",
            "maxInputTokens": 123, "maxOutputTokens": 456, "supportsVision": True,
            "reasoning": {"effort": "low", "supported_levels": ["low"]},
            "onlyReasoning": True, "supportsToolCall": True, "credits": 2, "vendor": "acme",
        })
        self.assertEqual(model["id"], "demo")
        self.assertEqual(model["description_zh"], "中文说明")
        self.assertEqual(model["max_in"], 123)
        self.assertTrue(model["img"])
        self.assertTrue(model["onlyReasoning"])
        self.assertEqual(model["vendor"], "acme")

    def test_codex_schema_and_known_levels(self):
        catalog = model_catalog_json()
        self.assertIsInstance(catalog["models"], list)
        item = next(x for x in catalog["models"] if x["slug"] == "deepseek-v4-pro")
        self.assertEqual([x["effort"] for x in item["supported_reasoning_levels"]],
                         ["low", "medium", "high", "xhigh"])
        for key in ("display_name", "description", "default_reasoning_level", "shell_type", "visibility",
                    "supported_in_api", "priority", "context_window", "max_context_window",
                    "effective_context_window_percent", "input_modalities", "default_reasoning_summary"):
            self.assertIn(key, item)

    def test_models_json_keeps_openai_shape(self):
        payload = models_json()
        self.assertEqual(payload["object"], "list")
        self.assertIn("capabilities", payload["data"][0])

    def test_catalog_override_does_not_duplicate_aliases(self):
        from unittest.mock import patch
        base = model_catalog_json()
        with patch.dict(os.environ, {"CODEBUDDY_MODEL_CATALOG_JSON": json.dumps(base)}):
            slugs = [m["slug"] for m in model_catalog_json()["models"]]
        self.assertEqual(len(slugs), len(set(slugs)))

    def test_model_alias_override_changes_catalog_target(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"CODEBUDDY_MODEL_ALIASES": "gpt-5.6-sol=glm-5.2"}):
            # MODEL_ALIASES is import-time state; assert the catalog logic's
            # explicit target handling through a direct temporary override.
            with patch("codebuddy_gateway.common.MODEL_ALIASES", {"gpt-5.6-sol": "glm-5.2"}):
                item = next(x for x in model_catalog_json()["models"] if x["slug"] == "gpt-5.6-sol")
        self.assertEqual(item["display_name"], "gpt-5.6-sol")


class CodexReasoningOutputTests(unittest.TestCase):
    def _chunks(self):
        return iter([
            {"id": "x", "created": 1, "model": "glm-5.3", "choices": [{"delta": {"reasoning_content": "think "}, "finish_reason": None}]},
            {"choices": [{"delta": {"reasoning_content": "done", "content": "answer"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}},
        ])

    def test_nonstream_reasoning_item(self):
        out = emit_nonstream(self._chunks())
        self.assertEqual(out["output"][0]["type"], "reasoning")
        self.assertEqual(out["output"][0]["summary"][0]["text"], "think done")

    def test_stream_reasoning_summary_events(self):
        raw = b"".join(emit_stream(self._chunks())).decode()
        self.assertIn("response.reasoning_summary_text.delta", raw)
        self.assertIn("think ", raw)
        self.assertIn("response.completed", raw)

    def test_same_delta_reasoning_and_text_have_distinct_indices(self):
        chunks = iter([{
            "id": "x", "created": 1, "model": "glm-5.3",
            "choices": [{"delta": {"reasoning_content": "think", "content": "answer"},
                         "finish_reason": "stop"}],
        }])
        events = b"".join(emit_stream(chunks)).decode()
        self.assertIn('"output_index": 0', events)
        self.assertIn('"output_index": 1', events)

    def test_reasoning_after_text_gets_a_new_output_index(self):
        chunks = iter([
            {"id": "x", "created": 1, "model": "glm-5.3", "choices": [{
                "delta": {"content": "answer"}, "finish_reason": None,
            }]},
            {"choices": [{"delta": {"reasoning_content": "late think"}, "finish_reason": "stop"}]},
        ])
        events = b"".join(emit_stream(chunks)).decode()
        self.assertIn('"output_index": 0', events)
        self.assertIn('"output_index": 1', events)

    def test_interleaved_output_segments_use_unique_item_ids(self):
        chunks = iter([
            {"id": "x", "created": 1, "model": "m", "choices": [{"delta": {"reasoning_content": "r1"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "a"}, "finish_reason": None}]},
            {"choices": [{"delta": {"reasoning_content": "r2"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "b"}, "finish_reason": "stop"}]},
        ])
        raw = b"".join(emit_stream(chunks)).decode()
        self.assertIn('"id": "reasoning_item_0"', raw)
        self.assertIn('"id": "reasoning_item_1"', raw)
        self.assertIn('"id": "msg_item_0"', raw)
        self.assertIn('"id": "msg_item_1"', raw)

    def test_tool_arguments_before_tool_metadata_are_retained(self):
        chunks = iter([
            {"id": "x", "created": 1, "model": "glm-5.3", "choices": [{
                "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\\\"a\\\":"}}]},
                "finish_reason": None,
            }]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1",
                "function": {"name": "f", "arguments": "1}"}}]}, "finish_reason": "tool_calls"}]},
        ])
        events = b"".join(emit_stream(chunks)).decode()
        self.assertIn('"arguments":', events)
        self.assertIn('1}', events)


if __name__ == "__main__":
    unittest.main()
