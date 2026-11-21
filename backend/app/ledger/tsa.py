"""RFC 3161 trusted timestamps: the request, the response check and the openssl check.

A token proves that its message imprint existed no later than the TSA's time.
Factual stamps SHA-256(seal ‖ signature) of each seal at two authorities. Tokens
are parsed with rfc3161-client; when an authority encodes its response in a way
the strict parser refuses, `openssl ts` reads it instead. `openssl ts -verify`
is the check that an outsider runs on an evidence pack, so it is also the check
used here.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from rfc3161_client import HashAlgorithm, TimestampRequestBuilder, decode_timestamp_response

GRANTED = {0, 1}  # granted, granted with modifications


def public_url(url: str) -> str:
    """The address without credentials: what is stored, logged and shown."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class TsaError(RuntimeError):
    pass


@dataclass(frozen=True)
class TsaToken:
    issuer: str
    response_bytes: bytes
    message_imprint: bytes
    gen_time: datetime | None
    policy: str | None
    chain_pem: bytes


def _pem(der_certificates) -> bytes:
    pems = []
    for der in der_certificates:
        cert = x509.load_der_x509_certificate(bytes(der))
        pems.append(cert.public_bytes(serialization.Encoding.PEM))
    return b"".join(pems)


def _parse_strict(
    response_bytes: bytes,
) -> tuple[int, bytes, datetime | None, str | None, bytes, int | None]:
    response = decode_timestamp_response(response_bytes)
    status = int(response.status)
    if status not in GRANTED:
        return status, b"", None, None, b"", None
    info = response.tst_info
    policy = info.policy.dotted_string if info.policy is not None else None
    gen_time = info.gen_time
    if gen_time is not None and gen_time.tzinfo is None:
        gen_time = gen_time.replace(tzinfo=UTC)
    chain = _pem(response.signed_data.certificates)
    nonce = int(info.nonce) if info.nonce is not None else None
    return status, bytes(info.message_imprint.message), gen_time, policy, chain, nonce


def _run(args: list[str], cwd: Path, data: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, input=data, capture_output=True, check=False)  # noqa: S603


def _hex_dump_after(text: str, heading: str) -> bytes:
    """The bytes of an openssl hex dump: lines like `0000 - ee bb 25-9c c3 ...   ..%.`."""
    lines = text.split(heading, 1)[1].splitlines()[1:] if heading in text else []
    collected: list[str] = []
    for line in lines:
        match = re.match(r"\s*[0-9a-f]{4} - ((?:[0-9a-f]{2}[ -]?)+)", line)
        if not match:
            break
        collected.append(match.group(1).replace("-", "").replace(" ", ""))
    try:
        return bytes.fromhex("".join(collected))
    except ValueError:
        return b""


def _parse_with_openssl(
    response_bytes: bytes,
) -> tuple[int, bytes, datetime | None, str | None, bytes, int | None]:
    """The lenient path: `openssl ts -reply -text` and `openssl pkcs7 -print_certs`."""
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        (work / "r.tsr").write_bytes(response_bytes)
        text = _run(["openssl", "ts", "-reply", "-in", "r.tsr", "-text"], work)
        out = text.stdout.decode(errors="replace")
        if text.returncode != 0 or "Status: Granted" not in out:
            return 2, b"", None, None, b"", None
        imprint = _hex_dump_after(out, "Message data:")
        gen_time = None
        time_match = re.search(r"Time stamp:\s*(.+)", out)
        if time_match:
            try:
                gen_time = datetime.strptime(
                    time_match.group(1).strip(), "%b %d %H:%M:%S.%f %Y %Z"
                ).replace(tzinfo=UTC)
            except ValueError:
                try:
                    gen_time = datetime.strptime(
                        time_match.group(1).strip(), "%b %d %H:%M:%S %Y %Z"
                    ).replace(tzinfo=UTC)
                except ValueError:
                    gen_time = None
        policy_match = re.search(r"Policy OID:\s*(\S+)", out)
        policy = policy_match.group(1) if policy_match else None
        nonce_match = re.search(r"Nonce:\s*(?:0x)?([0-9A-Fa-f]+)", out)
        nonce = (
            int(nonce_match.group(1), 16)
            if nonce_match and nonce_match.group(1) != "unspecified"
            else None
        )
        _run(["openssl", "ts", "-reply", "-in", "r.tsr", "-token_out", "-out", "t.der"], work)
        certs = _run(["openssl", "pkcs7", "-inform", "DER", "-in", "t.der", "-print_certs"], work)
        chain = (
            b"\n".join(
                line
                for line in certs.stdout.splitlines()
                if not line.startswith((b"subject=", b"issuer="))
            ).strip()
            + b"\n"
        )
        return 0, imprint, gen_time, policy, chain if b"BEGIN CERTIFICATE" in chain else b"", nonce


class TimestampClient:
    def __init__(self, http: httpx.AsyncClient, *, timeout: float = 30.0) -> None:
        self.http = http
        self.timeout = timeout

    @staticmethod
    def build_request(data: bytes):
        return (
            TimestampRequestBuilder()
            .data(data)
            .hash_algorithm(HashAlgorithm.SHA256)
            .nonce(nonce=True)
            .cert_request(cert_request=True)
            .build()
        )

    def check(
        self, response_bytes: bytes, data: bytes, *, nonce: int | None, issuer: str = ""
    ) -> TsaToken:
        """Granted, imprint equal to SHA-256(data), nonce echoed: else TsaError."""
        try:
            status, imprint, gen_time, policy, chain, got_nonce = _parse_strict(response_bytes)
        except ValueError:
            status, imprint, gen_time, policy, chain, got_nonce = _parse_with_openssl(
                response_bytes
            )
        if status not in GRANTED:
            raise TsaError(f"{issuer or 'the TSA'} did not grant the timestamp (status {status})")
        expected = hashlib.sha256(data).digest()
        if imprint != expected:
            raise TsaError(
                f"{issuer or 'the TSA'} returned a token whose message imprint does not match"
            )
        if nonce is not None and got_nonce is not None and got_nonce != nonce:
            raise TsaError(f"{issuer or 'the TSA'} returned a token with a different nonce")
        return TsaToken(issuer, response_bytes, imprint, gen_time, policy, chain)

    async def request(self, url: str, data: bytes) -> TsaToken:
        request = self.build_request(data)
        shown = public_url(url)  # credentials, if any, never reach a message or a row
        try:
            response = await self.http.post(
                url,
                content=request.as_bytes(),
                headers={
                    "Content-Type": "application/timestamp-query",
                    "Accept": "application/timestamp-reply",
                },
                timeout=self.timeout,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise TsaError(f"{shown}: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise TsaError(f"{shown} answered HTTP {response.status_code}")
        return self.check(response.content, data, nonce=request.nonce, issuer=shown)


def verify_token_with_openssl(
    response_bytes: bytes, data: bytes, chain_pem: bytes, *, at: datetime | None = None
) -> bool:
    """`openssl ts -verify -in token -data stamped -CAfile chain`: the outsider's check.

    `at` checks the certificates at that time instead of now: a token whose authority's
    certificate has expired is still good at the time a renewal proved it existed.
    """
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        (work / "token.tsr").write_bytes(response_bytes)
        (work / "stamped.bin").write_bytes(data)
        (work / "chain.pem").write_bytes(chain_pem)
        args = [
            "openssl",
            "ts",
            "-verify",
            "-in",
            "token.tsr",
            "-data",
            "stamped.bin",
            "-CAfile",
            "chain.pem",
        ]
        if at is not None:
            args += ["-attime", str(int(at.timestamp()))]
        result = _run(args, work)
    return result.returncode == 0 and b"Verification: OK" in result.stdout + result.stderr
