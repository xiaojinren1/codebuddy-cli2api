"""Upstream error introspection: nested reason extraction + quota detection."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codebuddy_gateway.common import is_quota_exhausted, upstream_message

QUOTA_BODY = {"error": {"data": {"code": 14014,
                                 "msg": "企业总额度已全部用尽，请联系企业管理员购买加量包。",
                                 "requestId": "c322243c"}}}
BLOCK_BODY = {"code": 11128, "msg": "Illegal API invocation from an unapproved channel",
              "displayMsg": {"en": "The request was blocked by security policy."}}


class UpstreamMessageTest(unittest.TestCase):
    def test_extracts_nested_quota_reason(self):
        self.assertIn("额度已全部用尽", upstream_message(QUOTA_BODY))

    def test_extracts_top_level_reason(self):
        self.assertEqual(upstream_message(BLOCK_BODY),
                         "Illegal API invocation from an unapproved channel")

    def test_falls_back_to_display_msg(self):
        self.assertIn("blocked", upstream_message({"error": {"displayMsg": {"en": "blocked by policy"}}}))

    def test_plain_string_body(self):
        self.assertEqual(upstream_message("boom"), "boom")

    def test_default_when_empty(self):
        self.assertEqual(upstream_message(None), "upstream error")
        self.assertEqual(upstream_message({}, default="x"), "x")


class QuotaDetectionTest(unittest.TestCase):
    def test_detects_by_code(self):
        self.assertTrue(is_quota_exhausted(QUOTA_BODY))

    def test_detects_by_message(self):
        self.assertTrue(is_quota_exhausted({"msg": "企业总额度已全部用尽"}))

    def test_block_error_is_not_quota(self):
        self.assertFalse(is_quota_exhausted(BLOCK_BODY))

    def test_rate_limit_is_not_quota(self):
        self.assertFalse(is_quota_exhausted({"msg": "too many requests"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
