"""Load the gateway's optional repository-root ``.env`` file.

This deliberately implements the small dotenv subset needed by the gateway so
startup remains dependency-free. Existing process environment values always
win over values from the file.
"""

from __future__ import annotations

import os
from pathlib import Path
import re


_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_value(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""

    if value[0] in "'\"":
        quote = value[0]
        if len(value) < 2 or value[-1] != quote:
            raise ValueError("unterminated quoted value")
        value = value[1:-1]
        if quote == '"':
            value = (
                value.replace("\\n", "\n")
                .replace("\\r", "\r")
                .replace("\\t", "\t")
                .replace('\\"', '"')
                .replace("\\\\", "\\")
            )
        return value

    # An inline comment starts after whitespace; a '#' in a URL or token is
    # otherwise part of the value.
    value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
    return value


def load_dotenv(path: str | os.PathLike[str] | None = None) -> bool:
    """Fill unset environment variables from ``path`` or the current ``.env``.

    Returns ``True`` when a file was found and loaded. Malformed assignments
    raise ``ValueError`` with the file and line number, without echoing values.
    """

    dotenv_path = Path(path) if path is not None else Path.cwd() / ".env"
    if not dotenv_path.is_file():
        return False

    for line_number, line in enumerate(dotenv_path.read_text(encoding="utf-8-sig").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if "=" not in stripped:
            raise ValueError(f"invalid dotenv assignment at {dotenv_path}:{line_number}")

        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        if not _KEY.fullmatch(key):
            raise ValueError(f"invalid dotenv variable name at {dotenv_path}:{line_number}")
        os.environ.setdefault(key, _parse_value(raw_value))
    return True
