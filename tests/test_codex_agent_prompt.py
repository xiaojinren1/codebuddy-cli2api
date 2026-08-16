import os
import unittest
from unittest.mock import patch

from codebuddy_gateway.formats.codex import parse


class CodexAgentPromptTests(unittest.TestCase):
    def test_compact_prompt_requires_tool_use_before_final_answer(self):
        with patch.dict(os.environ, {"CODEBUDDY_CODEX_INSTRUCTIONS_MODE": "compact"}):
            parsed = parse({
                "model": "glm-5.3",
                "instructions": "internal prompt",
                "input": "inspect the repository",
            }, {}, "/v1/responses")

        prompt = parsed["messages"][0]["content"]
        self.assertIn("Do not stop after stating a plan", prompt)
        self.assertIn("call the relevant tool", prompt)


if __name__ == "__main__":
    unittest.main()
