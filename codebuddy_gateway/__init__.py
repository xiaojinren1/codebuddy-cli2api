"""CodeBuddy universal gateway - expose Tencent CodeBuddy Code's CLI inference
API as OpenAI / Anthropic / Gemini / Codex(Responses) endpoints.

Run with:  python -m codebuddy_gateway
"""

from .env import load_dotenv

load_dotenv()

from .common import DEFAULT_MODEL  # noqa: E402,F401

__version__ = "2.0.0"
