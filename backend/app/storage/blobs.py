"""Attachment bytes: hashed, encrypted with their own key, stored with a checksum and a lock.

The stream is hashed (SHA-256 of the plaintext), encrypted with its own
AES-256-GCM key and hashed again as ciphertext, in one pass. The file key is
wrapped by the master key. The object store verifies the ciphertext checksum
on receipt and keeps the object under Object Lock.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES = 12
TAG_BYTES = 16
HEAD_BYTES = 64


@dataclass
class EncryptResult:
    sha256_plain: bytes
    sha256_stored: bytes
    size_bytes: int
    head: bytes  # the first plaintext bytes, for type detection
    _nonce: bytes
    _ciphertext: bytearray
    _tag: bytes

    def stored_bytes(self) -> bytes:
        return self._nonce + bytes(self._ciphertext) + self._tag


async def encrypt_stream(
    chunks: AsyncIterator[bytes], file_key: bytes, aad: bytes
) -> EncryptResult:
    """Hash, encrypt and hash again while the bytes stream in."""
    nonce = os.urandom(NONCE_BYTES)
    encryptor = Cipher(algorithms.AES(file_key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(aad)
    plain_hash = hashlib.sha256()
    stored_hash = hashlib.sha256(nonce)
    ciphertext = bytearray()
    head = b""
    size = 0
    async for chunk in chunks:
        if not chunk:
            continue
        size += len(chunk)
        plain_hash.update(chunk)
        if len(head) < HEAD_BYTES:
            head += chunk[: HEAD_BYTES - len(head)]
        encrypted = encryptor.update(chunk)
        stored_hash.update(encrypted)
        ciphertext.extend(encrypted)
    final = encryptor.finalize()
    stored_hash.update(final)
    ciphertext.extend(final)
    tag = encryptor.tag
    stored_hash.update(tag)
    return EncryptResult(
        sha256_plain=plain_hash.digest(),
        sha256_stored=stored_hash.digest(),
        size_bytes=size,
        head=head,
        _nonce=nonce,
        _ciphertext=ciphertext,
        _tag=tag,
    )


def decrypt(stored: bytes, file_key: bytes, aad: bytes) -> bytes:
    if len(stored) < NONCE_BYTES + TAG_BYTES:
        raise ValueError("stored object is too short")
    nonce, body, tag = stored[:NONCE_BYTES], stored[NONCE_BYTES:-TAG_BYTES], stored[-TAG_BYTES:]
    decryptor = Cipher(algorithms.AES(file_key), modes.GCM(nonce, tag)).decryptor()
    decryptor.authenticate_additional_data(aad)
    try:
        return decryptor.update(body) + decryptor.finalize()
    except InvalidTag as exc:
        raise ValueError(
            "decryption failed: wrong key, wrong associated data or changed bytes"
        ) from exc


def wrap_key(master_key: bytes, file_key: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(master_key).encrypt(nonce, file_key, aad)


def unwrap_key(master_key: bytes, wrapped: bytes, aad: bytes) -> bytes:
    try:
        return AESGCM(master_key).decrypt(wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:], aad)
    except InvalidTag as exc:
        raise ValueError("key unwrap failed: wrong master key or associated data") from exc


_MAGIC: list[tuple[int, bytes, str]] = [
    (0, b"\xff\xd8\xff", "image/jpeg"),
    (0, b"\x89PNG\r\n\x1a\n", "image/png"),
    (0, b"GIF8", "image/gif"),
    (0, b"%PDF", "application/pdf"),
    (0, b"OggS", "audio/ogg"),
    (0, b"ID3", "audio/mpeg"),
    (4, b"ftyp", "video/mp4"),
]


def sniff_mime(head: bytes, declared: str | None, file_name: str | None) -> str:
    """The type from the bytes when they are unmistakable, else the declared one, else a guess."""
    for offset, magic, mime in _MAGIC:
        if head[offset : offset + len(magic)] == magic:
            if mime == "video/mp4" and declared and declared.startswith("audio/"):
                return declared
            return mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if declared:
        return declared
    if file_name:
        guessed, _ = mimetypes.guess_type(file_name)
        if guessed:
            return guessed
    return "application/octet-stream"


@dataclass(frozen=True)
class PutResult:
    version_id: str | None
    checksum_sha256: str


@dataclass(frozen=True)
class HeadResult:
    size: int
    checksum_sha256: str | None
    retain_until: datetime | None


class ObjectStore(Protocol):
    async def ensure_bucket(self) -> None: ...

    async def object_lock_enabled(self) -> bool: ...

    async def put(
        self, key: str, data: bytes, checksum_sha256: str, retain_until: datetime | None
    ) -> PutResult: ...

    async def head(self, key: str, version_id: str | None) -> HeadResult: ...

    async def get(self, key: str, version_id: str | None) -> bytes: ...

    async def delete(self, key: str, version_id: str | None) -> None: ...


def b64_sha256(data: bytes) -> str:
    import base64

    return base64.b64encode(hashlib.sha256(data).digest()).decode()


@dataclass
class _Version:
    version_id: str
    data: bytes
    checksum: str
    retain_until: datetime | None


@dataclass
class InMemoryObjectStore:
    """A versioned, locked bucket in memory, for tests."""

    objects: dict[str, list[_Version]] = field(default_factory=dict)
    lock_days: int = 3650

    async def ensure_bucket(self) -> None:
        return None

    async def object_lock_enabled(self) -> bool:
        return self.lock_days > 0

    async def put(self, key, data, checksum_sha256, retain_until) -> PutResult:
        if b64_sha256(data) != checksum_sha256:
            raise ValueError("checksum does not match the data")
        version = _Version(uuid.uuid4().hex, bytes(data), checksum_sha256, retain_until)
        self.objects.setdefault(key, []).append(version)
        return PutResult(version.version_id, checksum_sha256)

    def _find(self, key, version_id) -> _Version:
        versions = self.objects.get(key)
        if not versions:
            raise KeyError(key)
        if version_id is None:
            return versions[-1]
        for version in versions:
            if version.version_id == version_id:
                return version
        raise KeyError(f"{key}@{version_id}")

    async def head(self, key, version_id) -> HeadResult:
        version = self._find(key, version_id)
        return HeadResult(len(version.data), version.checksum, version.retain_until)

    async def get(self, key, version_id) -> bytes:
        return self._find(key, version_id).data

    async def delete(self, key, version_id) -> None:
        version = self._find(key, version_id)
        if version.retain_until and version.retain_until > datetime.now(UTC):
            raise PermissionError("object is locked")
        self.objects[key].remove(version)


class S3ObjectStore:
    """Any S3-compatible store, through boto3 in a worker thread."""

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        lock_days: int,
        region: str = "us-east-1",
    ) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.lock_days = lock_days
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
            config=Config(
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    async def ensure_bucket(self) -> None:
        from botocore.exceptions import ClientError

        def _ensure() -> None:
            try:
                self.client.head_bucket(Bucket=self.bucket)
                return
            except ClientError as exc:
                if exc.response["Error"]["Code"] not in ("404", "NoSuchBucket", "NotFound"):
                    raise
            if self.lock_days > 0:
                self.client.create_bucket(Bucket=self.bucket, ObjectLockEnabledForBucket=True)
            else:
                self.client.create_bucket(Bucket=self.bucket)

        await asyncio.to_thread(_ensure)

    async def object_lock_enabled(self) -> bool:
        from botocore.exceptions import ClientError

        def _check() -> bool:
            try:
                config = self.client.get_object_lock_configuration(Bucket=self.bucket)
            except ClientError:
                return False
            return config.get("ObjectLockConfiguration", {}).get("ObjectLockEnabled") == "Enabled"

        return await asyncio.to_thread(_check)

    async def put(self, key, data, checksum_sha256, retain_until) -> PutResult:
        from botocore.exceptions import ClientError

        def _put() -> PutResult:
            params = {
                "Bucket": self.bucket,
                "Key": key,
                "Body": bytes(data),
                "ChecksumSHA256": checksum_sha256,
            }
            if retain_until is not None and self.lock_days > 0:
                params["ObjectLockMode"] = "COMPLIANCE"
                params["ObjectLockRetainUntilDate"] = retain_until
            try:
                response = self.client.put_object(**params)
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code in ("BadDigest", "InvalidRequest", "XAmzContentSHA256Mismatch"):
                    raise ValueError(f"the store rejected the checksum: {code}") from exc
                raise
            return PutResult(
                response.get("VersionId"), response.get("ChecksumSHA256", checksum_sha256)
            )

        return await asyncio.to_thread(_put)

    async def head(self, key, version_id) -> HeadResult:
        def _head() -> HeadResult:
            params = {"Bucket": self.bucket, "Key": key, "ChecksumMode": "ENABLED"}
            if version_id:
                params["VersionId"] = version_id
            response = self.client.head_object(**params)
            return HeadResult(
                int(response["ContentLength"]),
                response.get("ChecksumSHA256"),
                response.get("ObjectLockRetainUntilDate"),
            )

        return await asyncio.to_thread(_head)

    async def get(self, key, version_id) -> bytes:
        def _get() -> bytes:
            params = {"Bucket": self.bucket, "Key": key}
            if version_id:
                params["VersionId"] = version_id
            return self.client.get_object(**params)["Body"].read()

        return await asyncio.to_thread(_get)

    async def delete(self, key, version_id) -> None:
        from botocore.exceptions import ClientError

        def _delete() -> None:
            params = {"Bucket": self.bucket, "Key": key}
            if version_id:
                params["VersionId"] = version_id
            try:
                self.client.delete_object(**params)
            except ClientError as exc:
                if exc.response["Error"]["Code"] in ("AccessDenied", "InvalidRequest"):
                    raise PermissionError("object is locked") from exc
                raise

        await asyncio.to_thread(_delete)
