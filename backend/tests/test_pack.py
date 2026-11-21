"""FAC-19: evidence packs that anyone verifies offline with Python and openssl."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hypothesis import given, settings
from hypothesis import strategies as st

from app.connectors.telegram.mapping import map_update
from app.ledger.canonical import canonicalize
from app.ledger.keys import public_key_bytes
from tests.chat_harness import Chat
from tests.fixtures.telegram import CAPTURE_CHAT_ID, load
from tests.tsa_server import LocalTsa

BACKEND = Path(__file__).resolve().parents[1]
VERIFY_PY = BACKEND / "scripts" / "verify.py"
FILE_BYTES = b"%PDF-1.7\n" + b"contract bytes " * 500
SYSTEM_PYTHON = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable


def load_verify_module():
    spec = importlib.util.spec_from_file_location("factual_verify", VERIFY_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


async def scene(chat: Chat) -> dict:
    """A record with an edit, a filing and a file, then an unrelated record; sealed and stamped."""
    note = await chat.say("@09:30 Greg called. Kickoff may move to 12 February. #call")
    record_seq = chat.rows("SELECT max(seq) FROM record")[0][0]
    await chat.tap(chat.button("client-acme").data, on_message=chat.last_message_id)
    raw = chat._raw_message(
        "@09:30 Greg called. Kickoff may move to 12 February, he confirms by mail. #call"
    )
    raw["message"]["message_id"] = note
    raw["edited_message"] = raw.pop("message")
    raw["edited_message"]["edit_date"] = raw["edited_message"]["date"] + 60
    await chat.ingestion.handle(
        map_update(raw, capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot")
    )
    doc = load("message_document")
    doc["message"]["reply_to_message"] = {
        "message_id": note,
        "chat": doc["message"]["chat"],
        "date": doc["message"]["date"] - 10,
        "text": "…",
    }
    await chat.ingestion.handle(
        map_update(doc, capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot")
    )
    doc_seq = chat.rows("SELECT max(seq) FROM record")[0][0]
    chat.clock.advance(20)
    await chat.say(
        "Completely unrelated secret note about the budget",
        user={"id": 51234568, "is_bot": False, "first_name": "Greg"},
    )
    while await chat.worker.run_once():
        pass
    chat.clock.advance(300)
    seal = await chat.sealer.seal_once()
    while await chat.worker.run_once():
        pass
    return {"note": note, "record_seq": record_seq, "doc_seq": doc_seq, "seal": seal}


async def build(chat: Chat, seqs: list[int]):
    from app.ledger.pack import PackBuilder

    builder = PackBuilder(
        chat.db,
        chat.ledger,
        public_key_pem=chat.public_key_pem,
        store=chat.store,
        master_key=chat.master_key,
        timezone="Europe/Amsterdam",
    )
    return await builder.build(seqs)


def unzip(pack, where: Path) -> Path:
    with zipfile.ZipFile(io.BytesIO(pack.data)) as zf:
        zf.extractall(where)
    return where


async def test_the_pack_holds_entries_payloads_blobs_proofs_seals_tokens_and_keys(
    chat, tsa, tmp_path
):
    s = await scene(chat)
    pack = await build(chat, [s["record_seq"], s["doc_seq"]])
    assert pack.filename == f"proof-E{s['record_seq']}-E{s['doc_seq']}.zip"
    with zipfile.ZipFile(io.BytesIO(pack.data)) as zf:
        names = set(zf.namelist())
    seal_no = s["seal"].no
    rec, doc = s["record_seq"], s["doc_seq"]
    edit_seq, filing_seq = (
        chat.rows(
            "SELECT seq FROM ledger_entry WHERE type IN ('record.edited','record.filed')"
            " ORDER BY seq"
        )[0][0],
        chat.rows("SELECT seq FROM ledger_entry WHERE type = 'record.filed'")[0][0],
    )
    stored_seq = chat.rows("SELECT seq FROM ledger_entry WHERE type = 'attachment.stored'")[0][0]
    for seq in (rec, doc, edit_seq, filing_seq, stored_seq):
        assert f"entries/{seq}.json" in names and f"payloads/{seq}.json" in names
        assert f"proofs/{seq}-in-{seal_no}.json" in names
    assert {"README.txt", "verify.py", "manifest.json", "keys/instance-ed25519.pub"} <= names
    assert {f"seals/{seal_no}.json", f"seals/{seal_no}.sig", f"seals/{seal_no}.stamped"} <= names
    assert {
        f"seals/{seal_no}-1.tsr",
        f"seals/{seal_no}-2.tsr",
        "tsa/chain-1.pem",
        "tsa/chain-2.pem",
    } <= names
    blob_name = f"blobs/{hashlib.sha256(FILE_BYTES).hexdigest()}.pdf"
    assert blob_name in names
    where = unzip(pack, tmp_path)
    assert (where / blob_name).read_bytes() == FILE_BYTES
    entry = json.loads((where / f"entries/{rec}.json").read_bytes())
    assert (
        entry["seq"] == rec and canonicalize(entry) == (where / f"entries/{rec}.json").read_bytes()
    )
    payload = json.loads((where / f"payloads/{rec}.json").read_text())
    assert len(bytes.fromhex(payload["salt"])) == 32 and "Greg called" in json.dumps(
        payload["payload"]
    )


async def test_the_pack_discloses_only_the_requested_records(chat, tmp_path):
    s = await scene(chat)
    pack = await build(chat, [s["record_seq"]])
    assert b"unrelated secret note" not in pack.data
    assert b"budget" not in pack.data
    where = unzip(pack, tmp_path)
    proofs = list((where / "proofs").glob("*.json"))
    assert proofs
    proof = json.loads(proofs[0].read_text())
    assert proof["later_entry_hashes"] and all(
        h.startswith("sha256:") for h in proof["later_entry_hashes"]
    )
    disclosed = {int(p.stem) for p in (where / "entries").glob("*.json")}
    unrelated = chat.rows("SELECT seq FROM record WHERE text LIKE %s", ("Completely%",))[0][0]
    assert unrelated not in disclosed
    assert s["doc_seq"] not in disclosed, "the reply is a separate record, not requested"


async def test_verify_py_passes_with_only_the_standard_library_and_openssl(chat, tmp_path):
    s = await scene(chat)
    pack = await build(chat, [s["record_seq"], s["doc_seq"]])
    where = unzip(pack, tmp_path)
    result = subprocess.run(
        [SYSTEM_PYTHON, "-I", "-S", "verify.py"], cwd=where, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert "FAIL" not in out
    for word in (
        "entry hash",
        "payload commitment",
        "chain",
        "seal signature",
        "timestamp",
        "attachment",
    ):
        assert word in out.lower(), word
    assert "VERIFIED" in out
    assert f"E{s['record_seq']}" in out and "2026" in out


@pytest.mark.parametrize(
    "tamper",
    ["payload", "entry", "proof", "seal_signature", "blob", "tsa_chain", "seal_body"],
)
async def test_verify_py_fails_after_any_change(chat, tmp_path, tamper):
    s = await scene(chat)
    pack = await build(chat, [s["doc_seq"]])
    where = unzip(pack, tmp_path)
    seal_no = s["seal"].no
    seq = s["doc_seq"]
    if tamper == "payload":
        p = where / f"payloads/{seq}.json"
        data = json.loads(p.read_text())
        data["payload"]["normalized"]["caption"] = "a different caption"
        p.write_text(json.dumps(data))
    elif tamper == "entry":
        p = where / f"entries/{seq}.json"
        p.write_bytes(p.read_bytes().replace(b'"record.created"', b'"record.deleted"'))
    elif tamper == "proof":
        p = where / f"proofs/{seq}-in-{seal_no}.json"
        data = json.loads(p.read_text())
        if data["later_entry_hashes"]:
            data["later_entry_hashes"][0] = "sha256:" + "00" * 32
        else:
            data["chain_before"] = "sha256:" + "00" * 32
        p.write_text(json.dumps(data))
    elif tamper == "seal_signature":
        p = where / f"seals/{seal_no}.sig"
        raw = bytearray(p.read_bytes())
        raw[0] ^= 0x01
        p.write_bytes(bytes(raw))
    elif tamper == "blob":
        blob = next((where / "blobs").iterdir())
        blob.write_bytes(blob.read_bytes() + b"x")
    elif tamper == "tsa_chain":
        other = Ed25519PrivateKey.generate()  # any unrelated PEM breaks the chain file
        from cryptography.hazmat.primitives import serialization

        (where / "tsa/chain-1.pem").write_bytes(
            other.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )
    elif tamper == "seal_body":
        p = where / f"seals/{seal_no}.json"
        data = json.loads(p.read_text())
        data["up_to_seq"] += 1
        p.write_bytes(canonicalize(data))
    result = subprocess.run(
        [SYSTEM_PYTHON, "-I", "-S", "verify.py"], cwd=where, capture_output=True, text=True
    )
    assert result.returncode != 0, result.stdout
    assert "FAIL" in result.stdout


async def test_a_pack_of_an_unsealed_record_says_so(chat, tmp_path):
    await scene(chat)
    chat.clock.advance(5)
    await chat.say("fresh note, not yet sealed")
    seq = chat.rows("SELECT max(seq) FROM record")[0][0]
    pack = await build(chat, [seq])
    where = unzip(pack, tmp_path)
    readme = (where / "README.txt").read_text()
    assert "not yet sealed" in readme.lower()
    result = subprocess.run(
        [SYSTEM_PYTHON, "-I", "-S", "verify.py"], cwd=where, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not yet sealed" in result.stdout.lower()


def test_verify_py_canonicalizes_like_the_backend():
    module = load_verify_module()
    json_values = st.recursive(
        st.none()
        | st.booleans()
        | st.integers(min_value=-(2**53) + 1, max_value=2**53 - 1)
        | st.floats(allow_nan=False, allow_infinity=False)
        | st.text(),
        lambda children: (
            st.lists(children, max_size=4)
            | st.dictionaries(st.text(max_size=6), children, max_size=4)
        ),
        max_leaves=20,
    )

    @settings(max_examples=300, deadline=None)
    @given(json_values)
    def check(value):
        assert module.canonicalize(value) == canonicalize(value)

    check()


def test_verify_py_checks_ed25519_signatures_in_pure_python():
    module = load_verify_module()
    key = Ed25519PrivateKey.generate()
    message = b"seal body"
    signature = key.sign(message)
    public = public_key_bytes(key)
    assert module.ed25519_verify(public, message, signature)
    assert not module.ed25519_verify(public, message + b"x", signature)
    assert not module.ed25519_verify(
        public_key_bytes(Ed25519PrivateKey.generate()), message, signature
    )
    from app.ledger.keys import public_key_pem

    assert module.public_key_from_pem(public_key_pem(key)) == public


def test_verify_py_imports_only_the_standard_library():
    source = VERIFY_PY.read_text()
    imports = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
    }
    allowed = {
        "argparse",
        "base64",
        "calendar",
        "hashlib",
        "json",
        "math",
        "os",
        "pathlib",
        "re",
        "shutil",
        "subprocess",
        "sys",
        "zipfile",
        "datetime",
        "tempfile",
        "time",
        "__future__",
        "typing",
        "dataclasses",
    }
    assert imports <= allowed, imports - allowed


async def test_proof_as_a_reply_posts_the_pack_in_the_chat(chat):
    s = await scene(chat)
    await chat.say("/proof", reply_to=s["note"])
    (file,) = chat.connector.files_sent
    assert file.filename == f"proof-E{s['record_seq']}.zip"
    assert (
        file.caption.startswith(f"Evidence pack for E{s['record_seq']}")
        and "verify.py" in file.caption
    )
    assert zipfile.ZipFile(io.BytesIO(file.data)).testzip() is None
    assert file.reply_to.message_id is not None


async def test_proof_with_numbers_builds_one_pack_for_several_records(chat):
    s = await scene(chat)
    await chat.say(f"/proof E{s['record_seq']} {s['doc_seq']}")
    (file,) = chat.connector.files_sent
    assert file.filename == f"proof-E{s['record_seq']}-E{s['doc_seq']}.zip"


async def test_proof_usage_and_unknown_numbers(chat):
    await chat.say("/proof")
    assert chat.replies[-1].startswith("Usage: /proof")
    await chat.say("/proof E999")
    assert chat.replies[-1] == "No entry E999."


async def test_a_pack_over_the_sending_limit_points_to_the_api(chat, monkeypatch):
    from app.commands import proof

    s = await scene(chat)
    monkeypatch.setattr(proof, "SEND_LIMIT_BYTES", 1000)
    await chat.say(f"/proof E{s['record_seq']}")
    assert chat.connector.files_sent == []
    reply = chat.replies[-1]
    assert "50 MB" in reply or "too large" in reply.lower()
    assert f"/api/v1/records/{s['record_seq']}/proof" in reply


@pytest.mark.skipif(
    not os.environ.get("FACTUAL_PACK_OUT"), reason="set FACTUAL_PACK_OUT to write a sample pack"
)
async def test_write_a_sample_pack_for_the_outsider_test(chat):
    """Not a check: writes a real pack to FACTUAL_PACK_OUT for someone outside the project."""
    s = await scene(chat)
    pack = await build(chat, [s["record_seq"], s["doc_seq"]])
    out = Path(os.environ["FACTUAL_PACK_OUT"])
    out.write_bytes(pack.data)
    assert out.stat().st_size > 1000


async def test_verify_py_detects_a_wrong_key_fingerprint(chat, tmp_path):
    s = await scene(chat)
    pack = await build(chat, [s["record_seq"]])
    where = unzip(pack, tmp_path)
    manifest = json.loads((where / "manifest.json").read_text())
    manifest["key_fingerprint"] = "sha256:" + "ab" * 32
    (where / "manifest.json").write_text(json.dumps(manifest))
    result = subprocess.run(
        [SYSTEM_PYTHON, "-I", "-S", "verify.py"], cwd=where, capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "FAIL  public key fingerprint" in result.stdout


async def test_verify_py_reports_incomplete_not_failed_when_only_libressl_is_available(
    chat, tmp_path
):
    """Stock macOS has LibreSSL, which cannot check timestamps; a genuine pack must not FAIL."""
    s = await scene(chat)
    pack = await build(chat, [s["record_seq"]])
    where = unzip(pack, tmp_path)
    fake = tmp_path / "fake-openssl"
    fake.write_text(
        '#!/bin/sh\nif [ "$1" = "version" ]; then echo "LibreSSL 3.3.6"; exit 0; fi\n'
        'echo "unsupported" >&2; exit 1\n'
    )
    fake.chmod(0o755)
    result = subprocess.run(
        [SYSTEM_PYTHON, "-I", "-S", "verify.py"],
        cwd=where,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "FACTUAL_OPENSSL": str(fake)},
    )
    assert result.returncode == 2, result.stdout
    out = result.stdout
    assert "INCOMPLETE" in out and "SKIP" in out and "FAIL" not in out
    assert "LibreSSL" in result.stdout and "brew install openssl" in result.stdout
    assert "OK    entry hash" in result.stdout and "OK    seal signature 1" in result.stdout


async def test_verify_py_names_each_token_and_warns_about_a_test_authority(chat, tmp_path):
    s = await scene(chat)
    pack = await build(chat, [s["record_seq"]])
    where = unzip(pack, tmp_path)
    out = subprocess.run(
        [SYSTEM_PYTHON, "-I", "-S", "verify.py"], cwd=where, capture_output=True, text=True
    ).stdout
    assert (
        "OK    timestamp seals/1-1.tsr on seal 1" in out
        and "timestamp seals/1-2.tsr on seal 1" in out
    )
    assert "Factual Test TSA" in out and "WARNING" in out and "not independent" in out
    assert "recorded Thu 29 Oct 2026" in out and "according to Telegram and Factual" in out
    assert "existed with exactly this content no later than" in out
    assert "unprotected labels" in out


async def test_the_readme_explains_times_authorities_entries_and_gives_concrete_commands(
    chat, tmp_path
):
    s = await scene(chat)
    pack = await build(chat, [s["record_seq"], s["doc_seq"]])
    where = unzip(pack, tmp_path)
    readme = (where / "README.txt").read_text()
    rec, doc = s["record_seq"], s["doc_seq"]
    edit_seq = chat.rows("SELECT seq FROM ledger_entry WHERE type = 'record.edited'")[0][0]
    filing_seq = chat.rows("SELECT seq FROM ledger_entry WHERE type = 'record.filed'")[0][0]
    stored_seq = chat.rows("SELECT seq FROM ledger_entry WHERE type = 'attachment.stored'")[0][0]
    assert "TWO KINDS OF TIME" in readme and "reported by Telegram and Factual" in readme
    assert f"Edited: E{edit_seq}" in readme and "both versions are in this pack" in readme
    assert f"Filed: E{filing_seq} puts it under client-acme" in readme
    assert (
        "File: contract-v2.pdf (application/pdf)" in readme
        and f"stored copy E{stored_seq}" in readme
    )
    assert f"E{edit_seq:<6} record.edited            a later version (edit) of E{rec}" in readme
    assert f"the stored copy of a file of E{doc}" in readme
    assert "TIME-STAMPING AUTHORITIES" in readme and "Factual Test TSA" in readme
    assert "WARNING: TEST AUTHORITY" in readme
    assert (
        "openssl ts -verify -in seals/1-1.tsr -data seals/1.stamped -CAfile tsa/chain-1.pem"
        in readme
    )
    assert (
        "openssl ts -verify -in seals/1-2.tsr -data seals/1.stamped -CAfile tsa/chain-2.pem"
        in readme
    )
    assert (
        "openssl pkey -pubin -in keys/instance-ed25519.pub -outform DER | shasum -a 256" in readme
    )
    assert "LibreSSL" in readme and "INCOMPLETE" in readme
    assert 'SHA-256("ledger-entry-v1"' in readme and "RFC 8785" in readme
    assert "not signed" in readme and "A later" in readme
    assert "Proven to exist by" in readme
