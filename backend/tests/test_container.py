"""FAC-04: container image, Compose stack and Makefile."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml

BACKEND = Path(__file__).resolve().parents[1]
VERSION = (BACKEND.parent / "VERSION.txt").read_text(encoding="utf-8").strip()


def make(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "-s", *args], cwd=BACKEND, capture_output=True, text=True, check=False
    )


def documented_targets() -> set[str]:
    text = (BACKEND / "Makefile").read_text(encoding="utf-8")
    return set(re.findall(r"^([a-z][a-z-]*):.*## ", text, re.MULTILINE))


def test_make_help_lists_every_target_with_a_description():
    result = make("help")
    assert result.returncode == 0, result.stderr
    listed = {line.split()[0] for line in result.stdout.splitlines() if line.strip()}
    assert listed == documented_targets()
    for target in ("help", "build", "up", "down", "run", "test", "lint"):
        assert target in listed


def test_make_alone_prints_the_same_list():
    assert make().stdout.strip()
    assert make().stdout == make("help").stdout


def test_make_test_and_lint_wrap_pytest_and_ruff():
    text = (BACKEND / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^test:.*\n\t.*pytest", text, re.MULTILINE)
    assert re.search(r"^lint:.*\n\t.*ruff check", text, re.MULTILINE)


def test_make_exports_version_to_every_compose_command():
    text = (BACKEND / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^export VERSION", text, re.MULTILINE)


def test_make_build_targets_both_architectures_tagged_with_the_version():
    text = (BACKEND / "Makefile").read_text(encoding="utf-8")
    assert "linux/amd64,linux/arm64" in text
    assert "--build-arg VERSION=$(VERSION)" in text
    assert "$(IMAGE):$(VERSION)" in text
    assert "VERSION   := $(shell cat ../VERSION.txt)" in text or "cat ../VERSION.txt" in text


def test_make_run_starts_the_backend_with_auto_reload():
    text = (BACKEND / "Makefile").read_text(encoding="utf-8")
    run_recipe = re.search(r"^run:.*\n((?:\t.*\n)+)", text, re.MULTILINE).group(1)
    assert "--reload" in run_recipe
    assert "APP_VERSION=$(VERSION)" in run_recipe


def test_dockerfile_takes_the_version_runs_as_non_root_and_checks_health():
    text = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^ARG VERSION", text, re.MULTILINE)
    assert re.search(r"^ENV APP_VERSION=\$\{?VERSION\}?", text, re.MULTILINE)
    user = re.search(r"^USER (\S+)", text, re.MULTILINE)
    assert user and user.group(1) not in ("root", "0")
    assert re.search(r"^HEALTHCHECK .*healthz", text, re.MULTILINE | re.DOTALL)
    assert re.search(r"^EXPOSE 8000", text, re.MULTILINE)
    assert re.search(r'^CMD \["python", "-m", "app"\]', text, re.MULTILINE)


def test_compose_runs_backend_postgres_and_an_s3_store():
    compose = yaml.safe_load((BACKEND / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert set(services) >= {"backend", "postgres", "s3"}
    backend = services["backend"]
    assert compose["name"] == "factual"
    assert backend["build"]["args"]["VERSION"] == "${VERSION:-dev}"
    assert backend["env_file"] == ".env" or ".env" in backend["env_file"]
    assert "${BACKEND_PORT:-8000}:8000" in backend["ports"]
    assert "${POSTGRES_PORT:-5432}:5432" in services["postgres"]["ports"]
    assert "${S3_PORT:-9000}:9000" in services["s3"]["ports"]
    assert services["s3"]["image"] == "rustfs/rustfs:1.0.6"
    assert set(backend["depends_on"]) >= {"postgres", "s3"}
    assert services["postgres"]["image"].startswith("postgres:")
    assert any("pgdata" in v for v in services["postgres"]["volumes"])
    assert any("s3data" in v for v in services["s3"]["volumes"])
    assert set(compose["volumes"]) >= {"pgdata", "s3data"}


# --- the image itself; needs a Docker daemon -------------------------------------------


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    probe = subprocess.run(
        ["docker", "info"], capture_output=True, text=True, check=False, timeout=30
    )
    return probe.returncode == 0


needs_docker = pytest.mark.docker  # auto-skipped in conftest when no daemon answers


@pytest.fixture(scope="module")
def built_image():
    tag = f"factual-backend-test:{uuid.uuid4().hex[:8]}"
    build = subprocess.run(
        ["docker", "build", "--build-arg", f"VERSION={VERSION}", "-t", tag, "."],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    assert build.returncode == 0, build.stderr[-3000:]
    yield tag
    subprocess.run(["docker", "image", "rm", "-f", tag], capture_output=True, check=False)


@needs_docker
def test_the_image_runs_as_a_non_root_user(built_image):
    out = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "id", built_image, "-u"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() != "0"


@needs_docker
def test_the_image_has_a_health_check_on_healthz(built_image):
    out = subprocess.run(
        ["docker", "inspect", "--format", "{{json .Config.Healthcheck}}", built_image],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "/healthz" in out.stdout
    assert out.stdout.strip() != "null"


@needs_docker
def test_the_container_serves_the_version_from_the_build_argument(built_image):
    env_file = BACKEND / ".env.example"
    name = f"factual-test-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            "0:8000",
            "--env-file",
            str(env_file),
            built_image,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    try:
        port = (
            subprocess.run(
                ["docker", "port", name, "8000/tcp"], capture_output=True, text=True, check=True
            )
            .stdout.strip()
            .splitlines()[0]
            .rsplit(":", 1)[1]
        )
        deadline = time.monotonic() + 60
        last = None
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/version", timeout=2)
                if response.status_code == 200:
                    assert response.json() == {"version": VERSION}
                    break
            except httpx.HTTPError as exc:
                last = exc
            time.sleep(1)
        else:
            logs = subprocess.run(
                ["docker", "logs", name], capture_output=True, text=True, check=False
            )
            pytest.fail(f"container never answered: {last}\n{logs.stdout}\n{logs.stderr}")
        health = subprocess.run(
            ["docker", "logs", name], capture_output=True, text=True, check=False
        ).stdout
        assert f'"service":{{"name":"factual-backend","version":"{VERSION}"}}' in health
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
