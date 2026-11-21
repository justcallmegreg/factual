"""`python -m app`: configure logging, then serve the app with Uvicorn.

Uvicorn's own logging configuration is switched off (`log_config=None`), so its
access and error logs, including the start-up lines, go through the same ECS
handler as everything else.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

import uvicorn

from app.core.logging import configure_logging
from app.core.settings import ConfigurationError, Settings
from app.core.version import read_version


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app", description="Run the Factual backend.")
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 — the container's own port
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="restart on code changes")
    args = parser.parse_args(argv)

    version = read_version()
    configure_logging("INFO", version)
    try:
        settings = Settings.load()
    except ConfigurationError as exc:
        logging.getLogger("factual").critical(str(exc))
        return 2
    configure_logging(settings.log_level, version, secrets=settings.secret_values())

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
