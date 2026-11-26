"""Run the configured LiteLLM Grok app on explicit interfaces, without a front."""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from uc_browser.listeners import add_listener_argument, serve


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="UC Grok OpenAI-compatible HTTP proxy")
    parser.add_argument("--config", type=Path, required=True, help="LiteLLM YAML configuration")
    parser.add_argument("--port", type=int, default=4000)
    add_listener_argument(parser)
    args = parser.parse_args(argv)
    config = args.config.resolve()
    if not config.is_file() or config.suffix.lower() not in (".yaml", ".yml"):
        parser.error("--config must name an existing YAML file")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    def application():
        # LiteLLM's own ASGI lifespan loads this configuration, providers and
        # router. No second proxy, forwarding layer or local-browser fallback.
        os.environ["CONFIG_FILE_PATH"] = str(config)
        from litellm.proxy.proxy_server import app
        return app

    serve(application, port=args.port, addresses=args.listen_address)


if __name__ == "__main__":
    main()
