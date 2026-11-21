"""Fix pass after the whole-branch review: evidence packs and the offline verifier.

verify.py refuses an attachment or an entry whose payload is not in the pack (labels
are not evidence); a multi-seal pack names the right chain file for every token; each
authority shows its root certificate and fingerprint, to compare with what the authority
publishes; the renewal fallback parses the manifest's ISO times; the README says that a
reply or a forward discloses what it quotes.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.chat_harness import Chat
from tests.test_pack import FILE_BYTES, SYSTEM_PYTHON, build, load_verify_module, scene, unzip
from tests.tsa_server import LocalTsa


@pytest.fixture(scope="module")
def tsa():
    server = LocalTsa()
    yield server
    server.close()


@pytest.fixture
async def chat(database_url, tsa):
    chat = await Chat.open(
        database_url, seals=True, attachments=True, tsa_urls=[tsa.url("tsa1"), tsa.url("tsa2")]
    )
    chat.connector.files["BQACDOC1"] = FILE_BYTES
    await chat.say("/project new client-acme")
    yield chat
    await chat.close()


def run_verify(where: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [SYSTEM_PYTHON, "-I", "-S", "verify.py"], cwd=where, capture_output=True, text=True
    )


def manifest_of(where: Path) -> dict:
    return json.loads((where / "manifest.json").read_text())


def write_manifest(where: Path, manifest: dict) -> None:
    (where / "manifest.json").write_text(json.dumps(manifest, indent=2))


async def test_verify_py_refuses_an_attachment_whose_payload_is_not_in_the_pack(chat, tmp_path):
    s = await scene(chat)
    where = unzip(await build(chat, [s["doc_seq"]]), tmp_path)
    manifest = manifest_of(where)
    (item,) = manifest["attachments"]
    (where / f"payloads/{item['entry_seq']}.json").unlink()
    for entry in manifest["entries"]:
        if entry["seq"] == item["entry_seq"]:
            entry["payload"] = None
    forged = b"a different document"
    (where / item["file"]).write_bytes(forged)
    item["sha256_plain"] = hashlib.sha256(forged).hexdigest()
    write_manifest(where, manifest)
    result = run_verify(where)
    assert result.returncode != 0, result.stdout
    assert "FAIL" in result.stdout and "payload" in result.stdout.lower()


async def test_verify_py_refuses_an_entry_listed_without_its_payload(chat, tmp_path):
    s = await scene(chat)
    where = unzip(await build(chat, [s["record_seq"]]), tmp_path)
    manifest = manifest_of(where)
    (where / f"payloads/{s['record_seq']}.json").unlink()
    for entry in manifest["entries"]:
        if entry["seq"] == s["record_seq"]:
            entry["payload"] = None
    write_manifest(where, manifest)
    result = run_verify(where)
    assert result.returncode != 0 and "without its payload" in result.stdout


async def test_a_multi_seal_pack_names_the_right_chain_file_for_every_token(chat, tmp_path):
    s = await scene(chat)
    chat.clock.advance(300)
    await chat.say("a later note, sealed separately")
    second = chat.rows("SELECT max(seq) FROM record")[0][0]
    await chat.sealer.seal_once()
    while await chat.worker.run_once():
        pass
    where = unzip(await build(chat, [s["record_seq"], second]), tmp_path)
    manifest = manifest_of(where)
    assert len(manifest["seals"]) == 2
    expected = {(a["token"], a["chain"]) for seal in manifest["seals"] for a in seal["anchors"]}
    assert {(a["token"], a["chain"]) for a in manifest["authorities"]} == expected
    readme = (where / "README.txt").read_text()
    by_hand = re.findall(r"openssl ts -verify -in (\S+) -data \S+ -CAfile (\S+)", readme)
    assert set(by_hand) == expected and all((where / chain).exists() for _, chain in by_hand)
    result = run_verify(where)
    assert result.returncode == 0, result.stdout


async def test_each_authority_names_its_root_certificate_and_verify_py_shows_it(chat, tmp_path):
    s = await scene(chat)
    where = unzip(await build(chat, [s["record_seq"]]), tmp_path)
    manifest = manifest_of(where)
    for authority in manifest["authorities"]:
        assert authority["subject"] == "CN=Factual Test TSA"
        assert authority["root_subject"] == "CN=Factual Test CA"
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", authority["root_fingerprint"])
    readme = (where / "README.txt").read_text()
    fingerprint = manifest["authorities"][0]["root_fingerprint"]
    assert fingerprint in readme and "compare" in readme.lower() and "root" in readme.lower()
    result = run_verify(where)
    assert result.returncode == 0, result.stdout
    assert "root CN=Factual Test CA" in result.stdout and fingerprint in result.stdout
    module = load_verify_module()
    summary = module.chain_summary(Path(manifest["authorities"][0]["chain"]), where)
    assert summary["subject"] == "CN=Factual Test TSA"
    assert summary["issuer"] == "CN=Factual Test CA"
    assert summary["root_subject"] == "CN=Factual Test CA"
    assert summary["root_fingerprint"] == fingerprint


def test_tsa_epoch_parses_openssl_and_iso_times():
    module = load_verify_module()
    expected = calendar.timegm((2026, 11, 21, 6, 40, 0, 0, 0, 0))
    assert module.tsa_epoch("Nov 21 06:40:00 2026 GMT") == expected
    assert module.tsa_epoch("Nov 21 06:40:00.123 2026 GMT") == expected
    assert module.tsa_epoch("2026-11-21T06:40:00Z") == expected
    assert module.tsa_epoch("not a time") is None


async def test_the_readme_says_that_a_reply_discloses_what_it_quotes(chat, tmp_path):
    s = await scene(chat)
    where = unzip(await build(chat, [s["doc_seq"]]), tmp_path)
    limits = (where / "README.txt").read_text().split("LIMITS", 1)[1]
    assert "reply" in limits and "quoted" in limits and "forward" in limits
