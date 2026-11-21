"""Typed settings, read from environment variables and validated at start-up."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
MIN_SECRET_LENGTH = 8


class ConfigurationError(RuntimeError):
    """The service cannot start: a required variable is missing or invalid."""


class Settings(BaseSettings):
    """Every variable from the specification's configuration table.

    Field names are the lower-case form of the environment variable names; the
    alias generator maps them back, so validation errors name the variable as the
    operator knows it (`DATABASE_URL`, not `database_url`).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        alias_generator=str.upper,
        validate_by_name=True,
        validate_by_alias=True,
    )

    log_level: LogLevel = "INFO"
    telegram_bot_token: str
    telegram_capture_chat_id: int | None = None
    database_url: str
    s3_endpoint: str
    s3_bucket: str
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_object_lock_days: int = 3650
    anthropic_api_key: str
    claude_model: str = "claude-sonnet-5-5"
    claude_effort: Literal["low", "medium", "high"] = "low"
    ask_daily_limit: int = 200
    signing_key_file: Path
    master_key_file: Path
    tsa_urls: Annotated[list[str], NoDecode]
    qualified_tsa_url: str | None = None
    seal_interval_seconds: int = 300
    timezone: str = "Europe/Amsterdam"
    daily_help_time: str = "06:00"
    ack_reaction: str = "✍"
    api_token: str | None = None
    audit_on_start: Literal["restore", "always", "never"] = "restore"

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_case_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("audit_on_start", "claude_effort", mode="before")
    @classmethod
    def _lower_case_mode(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("tsa_urls", mode="before")
    @classmethod
    def _split_tsa_urls(cls, value: object) -> object:
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("tsa_urls")
    @classmethod
    def _two_tsas(cls, value: list[str]) -> list[str]:
        if len(value) != 2:
            raise ValueError("two RFC 3161 TSA addresses are required, separated by a comma")
        return value

    @field_validator("daily_help_time")
    @classmethod
    def _hh_mm(cls, value: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("must be a time of day as HH:MM")
        return value

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    def secret_values(self) -> list[str]:
        """Every credential the logs must never show.

        Identifiers (bucket, access key id) are not secrets. Values shorter than 8
        characters are left out: redacting a short, common word would blank out
        ordinary text in every line.
        """
        candidates = (
            self.telegram_bot_token,
            self.anthropic_api_key,
            self.s3_secret_access_key,
            self.api_token,
        )
        return [value for value in candidates if value and len(value) >= MIN_SECRET_LENGTH]

    @classmethod
    def load(cls, env_file: str | Path | None = ".env") -> Settings:
        """Read the settings, or raise ConfigurationError naming each bad variable."""
        try:
            return cls(_env_file=env_file)
        except ValidationError as exc:
            problems = ", ".join(
                f"{'.'.join(str(part) for part in err['loc'])} ({err['msg']})"
                for err in exc.errors()
            )
            raise ConfigurationError(
                f"cannot start: missing or invalid environment variables: {problems}"
            ) from exc
