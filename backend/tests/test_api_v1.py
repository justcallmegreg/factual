"""FAC-22: the read-only HTTP API for the future frontend and tools such as Claude Code."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.connectors.null import NullConnector
from tests.chat_harness import Chat

BACKEND = Path(__file__).resolve().parents[1]
GREG = {"id": 51234568, "is_bot": False, "first_name": "Greg", "last_name": "S."}
TOKEN = "s3cret-api-token-for-tests"


@pytest.fixture
async def seeded(database_url, key_files):
    """A database with projects, records, a filing and a seal, through the chat harness."""
    from app.ledger.keys import load_signing_key

    chat = await Chat.open(database_url, seals=True, signing_key=load_signing_key(key_files[0]))
    await chat.say("/project new client-acme")
    await chat.say("/project new family")
    await chat.say("Kickoff on 5 February at the office", user=GREG)
    first = chat.rows("SELECT max(seq) FROM record")[0][0]
    await chat.tap(chat.button("client-acme").data, on_message=chat.last_message_id)
    chat.clock.advance(30)
    await chat.say("Dinner with the family on Sunday")
    second = chat.rows("SELECT max(seq) FROM record")[0][0]
    chat.clock.advance(300)
    await chat.sealer.seal_once()
    await chat.close()
    return {"first": first, "second": second, "url": database_url}


def client_for(settings, seeded, token: str | None):
    from app.main import create_app

    configured = settings.model_copy(update={"database_url": seeded["url"], "api_token": token})
    return TestClient(create_app(settings=configured, connector_factory=lambda _: NullConnector()))


@pytest.fixture
def api(db_settings, seeded):
    with client_for(db_settings, seeded, TOKEN) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        yield client


def test_status(api, seeded):
    body = api.get("/api/v1/status").json()
    assert body["ledger"]["head_seq"] >= seeded["second"]
    assert body["last_seal"]["no"] == 1 and body["last_seal"]["up_to_seq"] >= seeded["second"]
    assert body["records_without_project"] == 1
    assert body["last_audit"] is None
    assert body["signing_key"].startswith("sha256:")


def test_projects_with_record_counts(api):
    body = api.get("/api/v1/projects").json()
    assert body["projects"] == [
        {"name": "client-acme", "records": 1, "archived_at": None},
        {"name": "family", "records": 0, "archived_at": None},
    ]


def test_records_list_filters_and_detail(api, seeded):
    body = api.get("/api/v1/records").json()
    assert [r["seq"] for r in body["records"]] == [seeded["second"], seeded["first"]], (
        "newest first"
    )
    item = next(r for r in body["records"] if r["seq"] == seeded["first"])
    assert item["project"] == "client-acme" and item["author"]["name"] == "Greg S."
    assert item["text"] == "Kickoff on 5 February at the office" and item["origin"] == "note"
    assert item["status"]["label"] == "Verified" and item["status"]["seal_no"] == 1
    filtered = api.get("/api/v1/records", params={"project": "client-acme"}).json()
    assert [r["seq"] for r in filtered["records"]] == [seeded["first"]]
    texted = api.get("/api/v1/records", params={"text": "family"}).json()
    assert [r["seq"] for r in texted["records"]] == [seeded["second"]]
    dated = api.get("/api/v1/records", params={"from": "2026-10-29T13:03:30Z"}).json()
    assert [r["seq"] for r in dated["records"]] == [seeded["second"]]
    none = api.get("/api/v1/records", params={"to": "2020-01-01T00:00:00Z"}).json()
    assert none["records"] == []
    detail = api.get(f"/api/v1/records/{seeded['first']}").json()
    assert detail["seq"] == seeded["first"] and detail["versions"] == 1
    assert detail["filing_history"][0]["project"] == "client-acme"
    assert detail["entry"]["type"] == "record.created" and detail["entry"]["seq"] == seeded["first"]
    assert api.get("/api/v1/records/999").status_code == 404


def test_search_gives_the_same_hits_as_ask(api, seeded):
    body = api.get("/api/v1/search", params={"q": "kickoff office"}).json()
    assert body["words"] == ["kickoff", "office"]
    assert [h["seq"] for h in body["hits"]] == [seeded["first"]]
    assert body["hits"][0]["text"] == "Kickoff on 5 February at the office"
    assert body["hits"][0]["status"]["label"] == "Verified"
    assert api.get("/api/v1/search").status_code == 422


def test_proof_returns_the_evidence_pack_as_a_zip(api, seeded):
    response = api.get(f"/api/v1/records/{seeded['first']}/proof")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert (
        response.headers["content-disposition"]
        == f'attachment; filename="proof-E{seeded["first"]}.zip"'
    )
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        names = set(zf.namelist())
    assert {"README.txt", "verify.py", "manifest.json"} <= names
    assert api.get("/api/v1/records/999/proof").status_code == 404


@pytest.mark.parametrize(
    "route", ["/api/v1/status", "/api/v1/projects", "/api/v1/records", "/api/v1/search?q=x"]
)
def test_every_route_needs_the_bearer_token(db_settings, seeded, route):
    with client_for(db_settings, seeded, TOKEN) as client:
        assert client.get(route).status_code == 401
        assert client.get(route, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get(route, headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
        assert client.get(route, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_the_api_is_switched_off_without_a_token(db_settings, seeded):
    with client_for(db_settings, seeded, None) as client:
        response = client.get("/api/v1/status", headers={"Authorization": "Bearer anything"})
        assert response.status_code == 404
        assert response.json()["detail"] == "The API is switched off: API_TOKEN is not set."
        assert client.get("/healthz").status_code == 200


def test_swagger_json_describes_every_route(settings):
    schema = json.loads((BACKEND / "swagger.json").read_text())
    paths = set(schema["paths"])
    assert {
        "/api/v1/status",
        "/api/v1/projects",
        "/api/v1/records",
        "/api/v1/records/{seq}",
        "/api/v1/search",
        "/api/v1/records/{seq}/proof",
    } <= paths
    assert "HTTPBearer" in schema["components"]["securitySchemes"]
    check = subprocess.run(
        [sys.executable, "scripts/export_openapi.py"], cwd=BACKEND, capture_output=True, text=True
    )
    assert check.stdout == (BACKEND / "swagger.json").read_text()
