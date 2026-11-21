"""FAC-05: swagger.json generated from the code."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import ALL_ENV_NAMES

BACKEND = Path(__file__).resolve().parents[1]
VERSION = (BACKEND.parent / "VERSION.txt").read_text(encoding="utf-8").strip()
SCRIPT = BACKEND / "scripts" / "export_openapi.py"


def export() -> str:
    """Run the export script in a clean environment: no settings, no server."""
    env = {k: v for k, v in os.environ.items() if k not in ALL_ENV_NAMES and k != "APP_VERSION"}
    result = subprocess.run(  # noqa: S603 — fixed argv
        [sys.executable, str(SCRIPT)], cwd=BACKEND, capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_the_script_writes_the_openapi_description_without_a_server():
    schema = json.loads(export())
    assert schema["openapi"].startswith("3.1")
    assert schema["info"]["title"] == "Factual"
    assert schema["info"]["version"] == VERSION
    assert "Thoth" in schema["info"]["description"]
    assert {"/healthz", "/version"} <= set(schema["paths"])


def test_the_output_has_a_stable_key_order_and_indentation():
    first, second = export(), export()
    assert first == second
    assert first.endswith("\n")
    assert first.startswith('{\n  "')
    schema = json.loads(first)
    assert list(schema) == sorted(schema)
    assert list(schema["paths"]) == sorted(schema["paths"])


def test_the_committed_swagger_json_matches_the_generated_one():
    committed = (BACKEND / "swagger.json").read_text(encoding="utf-8")
    assert committed == export(), "run `make swagger` and commit the result"


def test_make_has_swagger_and_swagger_check_targets():
    text = (BACKEND / "Makefile").read_text(encoding="utf-8")
    assert "swagger: ##" in text and "swagger-check: ##" in text
    assert "scripts/export_openapi.py > swagger.json" in text
    assert "scripts/export_openapi.py | diff - swagger.json" in text


def test_swagger_ui_serves_the_same_api_with_the_version(settings):
    from app.main import create_app

    with TestClient(create_app(settings=settings)) as client:
        docs = client.get("/docs")
        assert docs.status_code == 200
        assert "swagger-ui" in docs.text and "/openapi.json" in docs.text
        live = client.get("/openapi.json").json()
    committed = json.loads((BACKEND / "swagger.json").read_text(encoding="utf-8"))
    assert live == committed
    assert live["info"]["version"] == VERSION


def test_swagger_check_fails_when_the_committed_file_differs(tmp_path):
    """The recipe `export | diff - swagger.json` exits non-zero on any difference."""
    stale = tmp_path / "swagger.json"
    stale.write_text(export().replace('"title": "Factual"', '"title": "Stale"'), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ALL_ENV_NAMES and k != "APP_VERSION"}
    check = subprocess.run(  # noqa: S602 — the Makefile recipe itself is a shell pipeline
        f"{sys.executable} {SCRIPT} | diff - {stale}",
        shell=True,
        cwd=BACKEND,
        capture_output=True,
        text=True,
        env=env,
    )
    assert check.returncode != 0
    assert "Stale" in check.stdout
