"""FAC-16: hash, encrypt and store a file; the store refuses changes to a locked object."""

from __future__ import annotations

import base64
import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.storage.blobs import (
    InMemoryObjectStore,
    S3ObjectStore,
    decrypt,
    encrypt_stream,
    sniff_mime,
    unwrap_key,
    wrap_key,
)

TEST_S3_ENDPOINT = os.environ.get("TEST_S3_ENDPOINT", "http://localhost:9000")
TEST_S3_KEY = os.environ.get("TEST_S3_ACCESS_KEY_ID", "factual")
TEST_S3_SECRET = os.environ.get("TEST_S3_SECRET_ACCESS_KEY", "factual-dev-secret")


async def chunks(data: bytes, size: int = 7):
    for i in range(0, len(data), size):
        yield data[i : i + size]


async def test_the_stream_is_hashed_encrypted_and_hashed_again_in_one_pass():
    plain = os.urandom(1000) + b"tail"
    file_key = os.urandom(32)
    result = await encrypt_stream(chunks(plain), file_key, aad=b"blobs/x")
    assert result.sha256_plain == hashlib.sha256(plain).digest()
    assert result.size_bytes == len(plain)
    stored = result.stored_bytes()
    assert result.sha256_stored == hashlib.sha256(stored).digest()
    assert len(stored) == 12 + len(plain) + 16  # nonce, ciphertext, tag
    assert plain not in stored
    assert decrypt(stored, file_key, aad=b"blobs/x") == plain
    with pytest.raises(ValueError):
        decrypt(stored, os.urandom(32), aad=b"blobs/x")
    with pytest.raises(ValueError):
        decrypt(stored, file_key, aad=b"blobs/other")


def test_the_file_key_is_wrapped_by_the_master_key():
    master, file_key = os.urandom(32), os.urandom(32)
    wrapped = wrap_key(master, file_key, aad=b"blobs/x")
    assert wrapped != file_key and len(wrapped) == 12 + 32 + 16
    assert unwrap_key(master, wrapped, aad=b"blobs/x") == file_key
    with pytest.raises(ValueError):
        unwrap_key(os.urandom(32), wrapped, aad=b"blobs/x")


@pytest.mark.parametrize(
    ("head", "declared", "name", "expected"),
    [
        (b"\xff\xd8\xff\xe0" + b"\0" * 20, None, "photo.jpg", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n" + b"\0" * 20, "image/jpeg", "x.png", "image/png"),
        (b"%PDF-1.7\n" + b"\0" * 20, None, "contract-v2.pdf", "application/pdf"),
        (b"OggS" + b"\0" * 20, "audio/ogg", None, "audio/ogg"),
        (b"\0" * 4 + b"ftypisom" + b"\0" * 20, None, "clip.mp4", "video/mp4"),
        (b"plain text file" + b"\0" * 20, "text/plain", "notes.txt", "text/plain"),
        (b"\x01\x02\x03" + b"\0" * 20, None, None, "application/octet-stream"),
    ],
)
def test_the_mime_type_is_detected_from_the_bytes_first(head, declared, name, expected):
    assert sniff_mime(head, declared, name) == expected


async def test_the_in_memory_store_behaves_like_a_locked_versioned_bucket():
    store = InMemoryObjectStore()
    await store.ensure_bucket()
    data = b"ciphertext"
    digest = base64.b64encode(hashlib.sha256(data).digest()).decode()
    until = datetime.now(UTC) + timedelta(days=1)
    put = await store.put("blobs/a", data, digest, until)
    assert put.version_id and put.checksum_sha256 == digest
    head = await store.head("blobs/a", put.version_id)
    assert head.size == len(data) and head.checksum_sha256 == digest
    assert await store.get("blobs/a", put.version_id) == data
    with pytest.raises(PermissionError):
        await store.delete("blobs/a", put.version_id)
    with pytest.raises(ValueError):
        await store.put(
            "blobs/b", data, base64.b64encode(hashlib.sha256(b"x").digest()).decode(), until
        )
    again = await store.put(
        "blobs/a",
        b"tampered",
        base64.b64encode(hashlib.sha256(b"tampered").digest()).decode(),
        until,
    )
    assert again.version_id != put.version_id
    assert await store.get("blobs/a", put.version_id) == data


def s3_available() -> bool:
    try:
        return httpx.get(f"{TEST_S3_ENDPOINT}/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


needs_s3 = pytest.mark.skipif(
    not s3_available(), reason="no S3-compatible store at TEST_S3_ENDPOINT"
)


@pytest.fixture
def s3_store() -> S3ObjectStore:
    return S3ObjectStore(
        endpoint=TEST_S3_ENDPOINT,
        bucket=f"factual-test-{uuid.uuid4().hex[:10]}",
        access_key_id=TEST_S3_KEY,
        secret_access_key=TEST_S3_SECRET,
        lock_days=1,
    )


@needs_s3
async def test_the_bucket_is_created_with_object_lock_and_uploads_carry_a_checksum(s3_store):
    await s3_store.ensure_bucket()
    assert await s3_store.object_lock_enabled() is True
    data = os.urandom(5000)
    digest = base64.b64encode(hashlib.sha256(data).digest()).decode()
    until = datetime.now(UTC) + timedelta(days=1)
    put = await s3_store.put("blobs/one", data, digest, until)
    assert put.version_id and put.checksum_sha256 == digest
    head = await s3_store.head("blobs/one", put.version_id)
    assert head.size == len(data) and head.checksum_sha256 == digest
    assert head.retain_until is not None and head.retain_until.date() == until.date()
    assert await s3_store.get("blobs/one", put.version_id) == data


@needs_s3
async def test_the_store_rejects_a_wrong_checksum(s3_store):
    await s3_store.ensure_bucket()
    wrong = base64.b64encode(hashlib.sha256(b"something else").digest()).decode()
    with pytest.raises(ValueError, match="checksum"):
        await s3_store.put("blobs/bad", b"real data", wrong, datetime.now(UTC) + timedelta(days=1))


@needs_s3
async def test_a_changed_object_is_a_new_version_and_the_locked_one_stays(s3_store):
    await s3_store.ensure_bucket()
    data = b"original ciphertext"
    digest = base64.b64encode(hashlib.sha256(data).digest()).decode()
    until = datetime.now(UTC) + timedelta(days=1)
    put = await s3_store.put("blobs/two", data, digest, until)
    tampered = b"tampered ciphertext"
    await s3_store.put(
        "blobs/two", tampered, base64.b64encode(hashlib.sha256(tampered).digest()).decode(), until
    )
    assert await s3_store.get("blobs/two", put.version_id) == data, "the recorded version is intact"
    assert await s3_store.get("blobs/two", None) == tampered, "the current version differs"
    head = await s3_store.head("blobs/two", put.version_id)
    assert head.checksum_sha256 == digest
    with pytest.raises(PermissionError):
        await s3_store.delete("blobs/two", put.version_id)
