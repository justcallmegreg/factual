"""FAC-03: structured JSON logs with ECS field names."""

from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def captured():
    """Configure logging into a buffer and return (buffer, reader)."""
    from app.core.logging import configure_logging

    buf = io.StringIO()
    configure_logging(
        "INFO", version="0.1.0", stream=buf, secrets=["123456:TEST-TOKEN", "sk-ant-test"]
    )
    yield buf
    configure_logging("WARNING", version="0.1.0", stream=io.StringIO())


def lines(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line]


def test_every_line_is_one_json_object_with_ecs_fields(captured):
    logging.getLogger("factual.sealer").info("seal written")
    (record,) = lines(captured)
    assert record["log.level"] == "info"
    assert record["message"] == "seal written"
    assert record["log"]["logger"] == "factual.sealer"
    assert record["service"] == {"name": "factual-backend", "version": "0.1.0"}
    assert "@timestamp" in record
    assert record["ecs.version"]


def test_the_level_filters_lower_levels(captured):
    from app.core.logging import configure_logging

    configure_logging("WARNING", version="0.1.0", stream=captured)
    logging.getLogger("factual.x").info("hidden")
    logging.getLogger("factual.x").warning("shown")
    assert [r["message"] for r in lines(captured)] == ["shown"]


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
def test_each_level_from_the_specification_is_accepted(level, captured):
    from app.core.logging import configure_logging

    configure_logging(level, version="0.1.0", stream=captured)
    assert logging.getLogger().level == getattr(logging, level)


def test_uvicorn_access_and_error_logs_use_the_same_handler(captured):
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:50000", "GET", "/healthz", "1.1", 200
    )
    logging.getLogger("uvicorn.error").warning("worker restarted")
    access, error = lines(captured)
    assert access["log"]["logger"] == "uvicorn.access"
    assert access["message"] == '127.0.0.1:50000 - "GET /healthz HTTP/1.1" 200'
    assert access["service"]["name"] == "factual-backend"
    assert error["log"]["logger"] == "uvicorn.error"
    assert error["log.level"] == "warning"


def test_context_helper_adds_action_and_entry_fields(captured):
    from app.core.logging import ctx

    logging.getLogger("factual.ledger").info(
        "record recorded", extra=ctx(action="record.created", entry=41233, seal=482)
    )
    (record,) = lines(captured)
    assert record["event"]["action"] == "record.created"
    assert record["factual"] == {"entry": 41233, "seal": 482}


def test_context_helper_refuses_fields_that_could_carry_record_text():
    from app.core.logging import ctx

    with pytest.raises(ValueError, match="text"):
        ctx(action="record.created", text="Greg called about the kickoff")
    with pytest.raises(ValueError):
        ctx(payload={"text": "x"})


def test_registered_secrets_never_appear_in_log_lines(captured):
    logging.getLogger("factual.telegram").error(
        "call failed: https://api.telegram.org/bot123456:TEST-TOKEN/getUpdates"
    )
    try:
        raise RuntimeError("api key sk-ant-test was rejected")
    except RuntimeError:
        logging.getLogger("factual.ai").exception("claude call failed")
    output = captured.getvalue()
    assert "123456:TEST-TOKEN" not in output
    assert "sk-ant-test" not in output
    assert output.count("[redacted]") >= 2
    assert all(json.loads(line) for line in output.splitlines())


def test_errors_carry_ecs_error_fields(captured):
    try:
        raise ValueError("bad seal")
    except ValueError:
        logging.getLogger("factual.sealer").exception("seal failed")
    (record,) = lines(captured)
    assert record["log.level"] == "error"
    assert record["error"]["type"] == "ValueError"
    assert record["error"]["message"] == "bad seal"
    assert "stack_trace" in record["error"]


def test_the_app_configures_logging_at_start_up(settings, monkeypatch):
    from app.core.logging import EcsFormatter
    from app.main import create_app

    settings = settings.model_copy(update={"log_level": "ERROR"})
    with TestClient(create_app(settings=settings, start_services=False)):
        root = logging.getLogger()
        assert root.level == logging.ERROR
        assert any(isinstance(h.formatter, EcsFormatter) for h in root.handlers)
        formatter = next(
            h.formatter for h in root.handlers if isinstance(h.formatter, EcsFormatter)
        )
        assert settings.telegram_bot_token in formatter.secrets


def test_settings_expose_their_secret_values(settings):
    secrets = settings.secret_values()
    assert settings.telegram_bot_token in secrets
    assert settings.anthropic_api_key in secrets
    assert settings.s3_secret_access_key in secrets
    assert "" not in secrets and None not in secrets


def test_python_m_app_serves_with_uvicorn_logging_switched_off(required_env, monkeypatch, tmp_path):
    import uvicorn

    from app import __main__ as entrypoint

    required_env.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    assert entrypoint.main(["--port", "8123"]) == 0
    ((target,), kwargs) = calls[0]
    assert target == "app.main:app"
    assert kwargs["log_config"] is None
    assert kwargs["port"] == 8123
    assert logging.getLogger().level == logging.INFO


def test_python_m_app_refuses_to_start_without_a_required_variable(
    required_env, monkeypatch, tmp_path
):
    import uvicorn

    from app import __main__ as entrypoint

    required_env.chdir(tmp_path)
    required_env.delenv("TSA_URLS")
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: pytest.fail("must not serve"))
    assert entrypoint.main([]) == 2


def test_short_values_and_identifiers_are_not_redacted(settings):
    """A 7-letter access key id must not blank out `factual-backend` in every line."""
    from app.core.logging import configure_logging

    settings = settings.model_copy(update={"s3_access_key_id": "factual", "api_token": "short"})
    buf = io.StringIO()
    configure_logging("INFO", version="0.1.0", stream=buf, secrets=settings.secret_values())
    logging.getLogger("factual.demo").info("hello from factual")
    (record,) = lines(buf)
    assert record["service"]["name"] == "factual-backend"
    assert record["message"] == "hello from factual"
    assert "factual" not in settings.secret_values()
    assert "short" not in settings.secret_values()


def test_uvicorn_colour_message_is_dropped(captured):
    logging.getLogger("uvicorn.error").info(
        "Started server process [%d]", 1, extra={"color_message": "Started \x1b[36m%d\x1b[0m"}
    )
    (record,) = lines(captured)
    assert "color_message" not in record
