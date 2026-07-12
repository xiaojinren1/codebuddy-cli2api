"""Entry point: ``python -m codebuddy_gateway``."""

from __future__ import annotations

import argparse

from .common import DEFAULT_HOST, DEFAULT_PORT, MODELS
from .server import serve


def main():
    ap = argparse.ArgumentParser(prog="codebuddy_gateway", description="CodeBuddy universal API gateway")
    ap.add_argument("--host", default=DEFAULT_HOST, help=f"bind host (default {DEFAULT_HOST})")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"bind port (default {DEFAULT_PORT})")
    ap.add_argument("--print-models", action="store_true", help="print the model catalog and exit")
    args = ap.parse_args()

    if args.print_models:
        for m in MODELS:
            flag = "[vision]" if m["img"] else "       "
            print(f"{flag} {m['id']:28s} {m['name']}  (max_out={m['max_out']})")
        return

    serve(args.host, args.port)


if __name__ == "__main__":
    main()
