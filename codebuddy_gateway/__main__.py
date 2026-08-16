"""Entry point: ``python -m codebuddy_gateway``."""

from __future__ import annotations

import argparse
import json

from .auth import login_interactive
from .common import DEFAULT_BASE_URL, DEFAULT_HOST, DEFAULT_PORT, load_model_catalog, model_catalog_json, models_json
from .server import serve


def main():
    ap = argparse.ArgumentParser(prog="codebuddy_gateway", description="CodeBuddy universal API gateway")
    ap.add_argument("--host", default=DEFAULT_HOST, help=f"bind host (default {DEFAULT_HOST})")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"bind port (default {DEFAULT_PORT})")
    ap.add_argument("--print-models", action="store_true", help="print the model catalog and exit")
    ap.add_argument("--print-codex-models", action="store_true", help="print the Codex model catalog JSON and exit")
    ap.add_argument("--login", action="store_true",
                    help="run the interactive login flow (external-link) and exit")
    args = ap.parse_args()

    if args.login:
        login_interactive()
        return

    if args.print_codex_models:
        print(json.dumps(model_catalog_json(), ensure_ascii=False, indent=2))
        return

    if args.print_models:
        for m in load_model_catalog():
            flag = "[vision]" if m["img"] else "       "
            print(f"{flag} {m['id']:28s} {m['name']}  (max_out={m['max_out']})")
        return

    serve(args.host, args.port)


if __name__ == "__main__":
    main()
