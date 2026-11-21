"""FAC-02: health and version routes, app factory and lifespan."""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import BACKEND_DIR


@pytest.fixture
def client(settings):
    from app.main import create_app

    with TestClient(create_app(settings=settings, start_services=False)) as client:
        yield client


def test_healthz_returns_200(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version_returns_the_content_of_version_txt(client, clean_env):
    expected = (BACKEND_DIR.parent / "VERSION.txt").read_text(encoding="utf-8").strip()
    response = client.get("/version")
    assert response.status_code == 200
    assert response.json() == {"version": expected}


def test_app_version_env_overrides_the_file(settings, clean_env):
    from app.main import create_app

    clean_env.setenv("APP_VERSION", "9.9.9")
    with TestClient(create_app(settings=settings, start_services=False)) as client:
        assert client.get("/version").json() == {"version": "9.9.9"}


def test_read_version_strips_whitespace(clean_env, tmp_path):
    from app.core.version import read_version

    (tmp_path / "VERSION.txt").write_text("  1.2.3 \n", encoding="utf-8")
    assert read_version(version_file=tmp_path / "VERSION.txt") == "1.2.3"


def test_the_service_refuses_to_start_when_a_required_variable_is_missing(required_env, tmp_path):
    from app.main import create_app

    required_env.chdir(tmp_path)  # no .env file here
    required_env.delenv("DATABASE_URL")
    with pytest.raises(Exception) as excinfo:
        with TestClient(create_app(start_services=False)):
            pass
    assert "DATABASE_URL" in str(excinfo.value)


def test_the_app_carries_title_and_version(settings):
    from app.core.version import read_version
    from app.main import create_app

    app = create_app(settings=settings, start_services=False)
    assert app.title == "Factual"
    assert app.version == read_version()
