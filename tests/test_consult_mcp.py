import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from codebuddy_gateway import consult_mcp


class ConsultMcpTests(unittest.TestCase):
    def _request(self, message):
        output = io.StringIO()
        with redirect_stdout(output):
            consult_mcp._handle(message)
        return json.loads(output.getvalue())

    def test_initialize_and_tool_listing(self):
        response = self._request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(response["result"]["serverInfo"]["name"], "codebuddy-consult")
        response = self._request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(response["result"]["tools"][0]["name"], "codebuddy_consult")

    def test_consult_is_text_only_and_passes_model(self):
        with patch.object(consult_mcp, "_consult", return_value="advice") as mocked:
            response = self._request({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "codebuddy_consult", "arguments": {"prompt": "x", "model": "kimi-k3-2"}},
            })
        mocked.assert_called_once_with({"prompt": "x", "model": "kimi-k3-2"})
        self.assertEqual(response["result"]["content"], [{"type": "text", "text": "advice"}])


if __name__ == "__main__":
    unittest.main()
