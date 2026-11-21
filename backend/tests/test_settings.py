"""FAC-02: typed settings from environment variables."""

import pytest
from pydantic import ValidationError

from tests.conftest import ALL_ENV_NAMES, BACKEND_DIR, REQUIRED_ENV, env_names_in


def test_required_variables_are_read_and_defaults_applied(settings):
    assert settings.telegram_bot_token == REQUIRED_ENV["TELEGRAM_BOT_TOKEN"]
    assert settings.database_url == REQUIRED_ENV["DATABASE_URL"]
    assert settings.log_level == "INFO"
    assert settings.telegram_capture_chat_id is None
    assert settings.s3_object_lock_days == 3650
    assert settings.claude_model == "claude-sonnet-5-5"
    assert settings.ask_daily_limit == 200
    assert settings.qualified_tsa_url is None
    assert settings.seal_interval_seconds == 300
    assert settings.timezone == "Europe/Amsterdam"
    assert settings.daily_help_time == "06:00"
    assert settings.ack_reaction == "✍"
    assert settings.api_token is None


def test_tsa_urls_is_split_into_two_addresses(settings):
    assert settings.tsa_urls == ["http://tsa-one.example/tsr", "http://tsa-two.example/tsr"]


@pytest.mark.parametrize("missing", sorted(REQUIRED_ENV))
def test_a_missing_required_variable_is_refused_by_name(required_env, missing):
    from app.core.settings import Settings

    required_env.delenv(missing)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    assert missing in str(excinfo.value)


def test_an_unknown_log_level_is_refused(required_env):
    from app.core.settings import Settings

    required_env.setenv("LOG_LEVEL", "VERBOSE")
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    assert "LOG_LEVEL" in str(excinfo.value)


def test_log_level_is_normalised_to_upper_case(required_env):
    from app.core.settings import Settings

    required_env.setenv("LOG_LEVEL", "debug")
    assert Settings(_env_file=None).log_level == "DEBUG"


def test_a_single_tsa_url_is_refused(required_env):
    from app.core.settings import Settings

    required_env.setenv("TSA_URLS", "http://only-one.example/tsr")
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    assert "TSA_URLS" in str(excinfo.value)


def test_capture_chat_id_is_an_integer_when_set(required_env):
    from app.core.settings import Settings

    required_env.setenv("TELEGRAM_CAPTURE_CHAT_ID", "-1001234567890")
    assert Settings(_env_file=None).telegram_capture_chat_id == -1001234567890


def test_env_example_lists_every_variable_and_nothing_else():
    names = env_names_in(BACKEND_DIR / ".env.example")
    assert names == set(ALL_ENV_NAMES)


def test_settings_fields_cover_every_variable():
    from app.core.settings import Settings

    fields = {name.upper() for name in Settings.model_fields}
    assert fields == set(ALL_ENV_NAMES)
