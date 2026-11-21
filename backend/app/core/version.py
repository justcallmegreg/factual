"""The version: from APP_VERSION, else from VERSION.txt at the repository root."""

from __future__ import annotations

import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_VERSION_FILE = BACKEND_DIR.parent / "VERSION.txt"


def read_version(version_file: Path | None = None) -> str:
    """Return the version string.

    `APP_VERSION` wins when it is set (the container image sets it from the build
    argument). Otherwise the version is the single line of `../VERSION.txt`, the only
    place where the version is written.
    """
    from_env = os.environ.get("APP_VERSION", "").strip()
    if from_env:
        return from_env
    path = version_file or DEFAULT_VERSION_FILE
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise RuntimeError(f"no version: set APP_VERSION or provide {path}") from exc
