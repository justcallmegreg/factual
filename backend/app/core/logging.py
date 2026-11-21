"""Structured logging: one JSON object per line with ECS field names.

Every module logs through the standard `logging` module; the root handler writes
ECS JSON to standard output and Uvicorn's loggers are routed through it. Two
guards enforce the content rule (logs carry entry numbers, never record text,
file contents, tokens or keys): `ctx()` only accepts whitelisted context fields,
and the formatter redacts every registered secret from the finished line.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable
from typing import IO, Any

import ecs_logging

from app.core.settings import MIN_SECRET_LENGTH

SERVICE_NAME = "factual-backend"
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
REDACTED = "[redacted]"

# Context fields a log line may carry. Identifiers and counts only.
ALLOWED_CONTEXT = frozenset(
    {
        "entry",
        "entries",
        "seal",
        "job",
        "project",
        "update_id",
        "chat_id",
        "message_id",
        "user_id",
        "count",
        "duration_ms",
        "status",
        "attempt",
        "kind",
        "tsa",
        "object_key",
        "connector",
        "command",
        "model",
        "path",
        "size_bytes",
        "retry_in_s",
        "run",
        "reason",
    }
)


class EcsFormatter(ecs_logging.StdlibFormatter):
    """ECS JSON lines with `service.name`/`service.version`, secrets redacted."""

    def __init__(self, version: str, secrets: Iterable[str] = ()) -> None:
        super().__init__(
            extra={"service": {"name": SERVICE_NAME, "version": version}},
            exclude_fields=["process", "log.original", "color_message"],
        )
        self.version = version
        self.secrets: set[str] = set()
        self.add_secrets(secrets)

    def add_secrets(self, secrets: Iterable[str]) -> None:
        for secret in secrets:
            if secret and len(secret) >= MIN_SECRET_LENGTH:
                self.secrets.add(secret)

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        for secret in self.secrets:
            if secret in line:
                line = line.replace(secret, REDACTED)
            escaped = json.dumps(secret)[1:-1]
            if escaped != secret and escaped in line:
                line = line.replace(escaped, REDACTED)
        return line


def configure_logging(
    level: str,
    version: str,
    stream: IO[str] | None = None,
    secrets: Iterable[str] = (),
) -> EcsFormatter:
    """Install the ECS handler on the root logger and route Uvicorn through it."""
    formatter = EcsFormatter(version, secrets)
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    for name in UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        for existing in list(uvicorn_logger.handlers):
            uvicorn_logger.removeHandler(existing)
        uvicorn_logger.propagate = True
        uvicorn_logger.setLevel(logging.NOTSET)
    return formatter


def current_formatter() -> EcsFormatter | None:
    for handler in logging.getLogger().handlers:
        if isinstance(handler.formatter, EcsFormatter):
            return handler.formatter
    return None


def register_secrets(secrets: Iterable[str]) -> None:
    """Add secrets learned after start-up (for example key material) to the redaction set."""
    formatter = current_formatter()
    if formatter is not None:
        formatter.add_secrets(secrets)


def ctx(action: str | None = None, **fields: Any) -> dict[str, Any]:
    """Context fields for a log line: `event.action` plus whitelisted identifiers.

    Pass the result as `extra=`. A field name outside the whitelist is refused, so
    record text, payloads, tokens and keys cannot reach a log line by accident.
    """
    extra: dict[str, Any] = {}
    if action:
        extra["event.action"] = action
    for name, value in fields.items():
        if name not in ALLOWED_CONTEXT:
            raise ValueError(
                f"log context field {name!r} is not allowed: logs carry entry numbers, "
                "never record text, file contents, tokens or keys"
            )
        extra[f"factual.{name}"] = value
    return extra
