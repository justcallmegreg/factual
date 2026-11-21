"""FAC-01: repository skeleton and versioning."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_root_holds_the_required_files_and_folders():
    for name in ("VERSION.txt", "README.md", "backend", "frontend", ".gitignore", ".editorconfig"):
        assert (ROOT / name).exists(), name
    assert (ROOT / "backend").is_dir()
    assert (ROOT / "frontend").is_dir()


def test_version_file_is_one_semantic_version_line():
    text = (ROOT / "VERSION.txt").read_text(encoding="utf-8")
    assert text.endswith("\n") and text.count("\n") == 1, "exactly one line"
    assert re.fullmatch(r"\d+\.\d+\.\d+", text.strip()), text
    assert tuple(int(p) for p in text.strip().split(".")) >= (0, 1, 0)


def test_version_is_written_nowhere_else():
    pyproject = (ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    assert not re.search(r"^version\s*=", pyproject, re.MULTILINE), "pyproject carries no version"


def test_root_readme_describes_factual_structure_and_version():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for needle in (
        "Factual",
        "Thoth",
        "VERSION.txt",
        "backend/",
        "frontend/",
        "Semantic Versioning",
    ):
        assert needle in text, needle


def test_backend_and_frontend_readmes_exist_and_frontend_is_a_placeholder():
    assert (ROOT / "backend" / "README.md").is_file()
    text = (ROOT / "frontend" / "README.md").read_text(encoding="utf-8")
    assert "empty for now" in text
    assert "VERSION.txt" in text
