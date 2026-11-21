"""FAC-06: the CI pipeline checks every change."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def workflow() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    data["on"] = data.pop(True, None) or data["on"]  # PyYAML reads the key `on` as True
    return data


def runs(job: dict) -> list[str]:
    return [step.get("run", "") for step in job["steps"]]


def test_every_push_runs_lint_test_and_swagger_check():
    data = workflow()
    assert "push" in data["on"]
    check = data["jobs"]["check"]
    assert runs(check)[-3:] == ["make lint", "make test", "make swagger-check"]
    assert check["defaults"]["run"]["working-directory"] == "backend"


def test_the_pipeline_caches_dependencies_and_provides_postgres():
    check = workflow()["jobs"]["check"]
    setup = next(
        s for s in check["steps"] if str(s.get("uses", "")).startswith("actions/setup-python")
    )
    assert setup["with"]["cache"] == "pip"
    assert check["services"]["postgres"]["image"].startswith("postgres:")


def test_each_merge_to_main_builds_both_architectures():
    build = workflow()["jobs"]["build"]
    assert "refs/heads/main" in build["if"] and "push" in build["if"]
    assert build["needs"] == "check"
    assert "make build" in runs(build)
    uses = [s["uses"] for s in build["steps"] if "uses" in s]
    assert any(u.startswith("docker/setup-qemu-action") for u in uses)
    assert any(u.startswith("docker/setup-buildx-action") for u in uses)


def test_actions_are_pinned_to_a_major_version():
    text = WORKFLOW.read_text(encoding="utf-8")
    for used in re.findall(r"uses:\s*(\S+)", text):
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@v\d+", used), used


def test_the_root_readme_shows_the_status_badge():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "actions/workflows/ci.yml/badge.svg" in readme
