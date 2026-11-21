"""Fix pass after the whole-branch review: start-up, CI and documentation.

The process answers /healthz while the start-up audit runs and /readyz says so; a
failing audit leaves the process alive but not ready; CI installs the PostgreSQL 17
client from PGDG and the drill fails rather than skips there; the READMEs say what the
code does.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.connectors.null import NullConnector

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


def test_health_answers_and_readiness_says_auditing_while_the_start_up_audit_runs(
    db_settings, monkeypatch
):
    from app.ledger import audit as audit_module
    from app.main import create_app

    real_run = audit_module.DeepAudit.run

    async def slow_run(self, reason="nightly"):
        await asyncio.sleep(0.6)
        return await real_run(self, reason=reason)

    monkeypatch.setattr(audit_module.DeepAudit, "run", slow_run)
    forced = db_settings.model_copy(update={"audit_on_start": "always"})
    app = create_app(settings=forced, connector_factory=lambda _: NullConnector())
    with TestClient(app) as client:
        started = time.monotonic()
        assert client.get("/healthz").status_code == 200
        early = client.get("/readyz")
        state = early.json()["startup"]
        assert early.status_code == 503 and (state == "starting" or state.startswith("auditing"))
        assert early.json()["status"] == "starting" and early.json()["database"] == "ok"
        while not client.get("/readyz").json()["startup"].startswith("auditing"):
            assert time.monotonic() - started < 30
            time.sleep(0.02)
        while client.get("/readyz").json()["startup"] != "ready":
            assert time.monotonic() - started < 30
            time.sleep(0.05)
        assert client.get("/readyz").json() == {
            "status": "ready",
            "database": "ok",
            "startup": "ready",
        }


def test_ci_installs_the_postgres_17_client_from_pgdg_and_fails_loudly():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "apt.postgresql.org.sh" in text and "postgresql-client-17" in text
    assert "|| echo" not in text
    drill = (BACKEND / "tests" / "test_backup_restore.py").read_text()
    assert 'os.environ.get("CI")' in drill and "pytest.fail" in drill


def test_the_readmes_say_what_the_code_does():
    backend_readme = (BACKEND / "README.md").read_text()
    env_example = (BACKEND / ".env.example").read_text()
    assert "Without `ANTHROPIC_API_KEY`" not in backend_readme, "the key is required at start"
    assert "ANTHROPIC_API_KEY` is required" in backend_readme
    assert "MinIO uses" not in env_example and "RustFS" in env_example
    assert "ruff format --check" in backend_readme
    assert "PostgreSQL 17" in backend_readme.split("## Set-up")[0], "prerequisites name 17"
    assert "/readyz" in backend_readme and "auditing" in backend_readme
