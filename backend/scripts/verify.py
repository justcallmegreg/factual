#!/usr/bin/env python3
"""Verify a Factual evidence pack offline.

Needs only Python 3.9 or newer and the `openssl` command. No network, no access
to Factual. Run it inside the unpacked folder, or give it the folder or the zip:

    python3 verify.py
    python3 verify.py proof-E41233.zip

It checks, for every disclosed entry: the entry's hash, the payload commitment,
the chain proof up to the seal, the seal's Ed25519 signature, the time-stamping
authorities' tokens (with `openssl ts -verify`) and the hash of every attachment.
It ends with VERIFIED or FAILED and explains what the pack proves.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ENTRY_TAG = b"ledger-entry-v1"
CHAIN_TAG = b"ledger-chain-v1"
SEAL_TAG = b"ledger-seal-v1"
MAX_SAFE_INTEGER = 2**53 - 1

# --------------------------------------------------------------------------------------
# Canonical JSON (RFC 8785), the same rules the ledger uses
# --------------------------------------------------------------------------------------

_SHORT_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def canonicalize(value):
    parts = []
    _write(value, parts)
    return "".join(parts).encode("utf-8")


def _write(value, out):
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise ValueError("integer outside the safe range")
        out.append(str(value))
    elif isinstance(value, float):
        out.append(format_number(value))
    elif isinstance(value, str):
        out.append(quote(value))
    elif isinstance(value, dict):
        out.append("{")
        first = True
        for key in sorted(value, key=lambda k: k.encode("utf-16-be", "surrogatepass")):
            if not first:
                out.append(",")
            first = False
            out.append(quote(key))
            out.append(":")
            _write(value[key], out)
        out.append("}")
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _write(item, out)
        out.append("]")
    else:
        raise TypeError("not a JSON type: %r" % type(value))


def quote(text):
    out = ['"']
    for char in text:
        escaped = _SHORT_ESCAPES.get(char)
        if escaped is not None:
            out.append(escaped)
        elif ord(char) < 0x20:
            out.append("\\u%04x" % ord(char))
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def format_number(number):
    if math.isnan(number) or math.isinf(number):
        raise ValueError("NaN and Infinity cannot be represented in JSON")
    if number == 0:
        return "0"
    sign = "-" if number < 0 else ""
    text = repr(abs(number))
    mantissa, _, exponent_text = text.partition("e")
    exponent = int(exponent_text) if exponent_text else 0
    int_part, _, frac_part = mantissa.partition(".")
    all_digits = int_part + frac_part
    stripped = all_digits.lstrip("0")
    leading_zeros = len(all_digits) - len(stripped)
    digits = stripped.rstrip("0")
    n = len(int_part) + exponent - leading_zeros
    k = len(digits)
    if k == 0:
        return "0"
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        exp = ("+" if e >= 0 else "-") + str(abs(e))
        body = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + exp
    return sign + body


# --------------------------------------------------------------------------------------
# Ed25519 signature check, RFC 8032 section 6, in plain Python
# --------------------------------------------------------------------------------------

_P = 2**255 - 19
_Q = 2**252 + 27742317777372353535851937790883648493


def _inv(x):
    return pow(x, _P - 2, _P)


_D = -121665 * _inv(121666) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _point_add(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    aa = (y1 - x1) * (y2 - x2) % _P
    bb = (y1 + x1) * (y2 + x2) % _P
    cc = 2 * t1 * t2 * _D % _P
    dd = 2 * z1 * z2 % _P
    e, f, g, h = bb - aa, dd - cc, dd + cc, bb + aa
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _point_mul(s, point):
    result = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            result = _point_add(result, point)
        point = _point_add(point, point)
        s >>= 1
    return result


def _point_equal(a, b):
    if (a[0] * b[2] - b[0] * a[2]) % _P != 0:
        return False
    return (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _recover_x(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * _inv(5) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _decompress(data):
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def ed25519_verify(public_key, message, signature):
    """True when `signature` is a valid Ed25519 signature of `message` by `public_key`."""
    if len(public_key) != 32 or len(signature) != 64:
        return False
    a = _decompress(public_key)
    r = _decompress(signature[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _Q:
        return False
    h = (
        int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little")
        % _Q
    )
    return _point_equal(_point_mul(s, _G), _point_add(r, _point_mul(h, a)))


ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


def public_key_from_pem(pem):
    """The raw 32-byte key from a `BEGIN PUBLIC KEY` PEM file (SubjectPublicKeyInfo)."""
    body = "".join(line for line in pem.decode().splitlines() if not line.startswith("-----"))
    der = base64.b64decode(body)
    if not der.startswith(ED25519_SPKI_PREFIX) or len(der) != len(ED25519_SPKI_PREFIX) + 32:
        raise ValueError("not an Ed25519 public key")
    return der[len(ED25519_SPKI_PREFIX) :]


# --------------------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------------------


def sha256(data):
    return hashlib.sha256(data).digest()


def hex_of(prefixed):
    """'sha256:ab12…' → bytes."""
    if not isinstance(prefixed, str) or not prefixed.startswith("sha256:"):
        raise ValueError("expected a sha256:… value")
    return bytes.fromhex(prefixed[len("sha256:") :])


class Report:
    def __init__(self):
        self.lines = []
        self.failures = 0
        self.skipped = 0

    def ok(self, label, detail=""):
        self.lines.append("OK    %s%s" % (label, (" — " + detail) if detail else ""))

    def fail(self, label, detail=""):
        self.failures += 1
        self.lines.append("FAIL  %s%s" % (label, (" — " + detail) if detail else ""))

    def skip(self, label, detail=""):
        self.skipped += 1
        self.lines.append("SKIP  %s%s" % (label, (" — " + detail) if detail else ""))

    def note(self, text):
        self.lines.append("      " + text)


OPENSSL_CANDIDATES = [
    "openssl",
    "/opt/homebrew/opt/openssl/bin/openssl",
    "/opt/homebrew/opt/openssl@3/bin/openssl",
    "/usr/local/opt/openssl/bin/openssl",
    "/usr/local/opt/openssl@3/bin/openssl",
    "/usr/bin/openssl",
]
OPENSSL_HINT = (
    "timestamps need OpenSSL 1.1.1 or newer (LibreSSL, which macOS ships, cannot check them): "
    "on macOS run `brew install openssl`, on Debian/Ubuntu `apt install openssl`, then run again"
)
_OPENSSL = {"path": None, "version": None}


def find_openssl():
    """The first usable OpenSSL on this machine, or None. LibreSSL is not usable.

    FACTUAL_OPENSSL names one binary to use instead of searching.
    """
    if _OPENSSL["path"] is not None or _OPENSSL["version"] == "none":
        return _OPENSSL["path"]
    forced = os.environ.get("FACTUAL_OPENSSL")
    candidates = [forced] if forced else OPENSSL_CANDIDATES
    for candidate in candidates:
        try:
            out = subprocess.run(
                [candidate, "version"], capture_output=True, text=True, timeout=10
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        match = re.match(r"OpenSSL (\d+)\.(\d+)\.(\d+)", out)
        if match and tuple(int(g) for g in match.groups()) >= (1, 1, 1):
            _OPENSSL["path"] = candidate
            _OPENSSL["version"] = out.strip()
            return candidate
    _OPENSSL["version"] = "none"
    return None


def run_openssl(args, cwd):
    binary = find_openssl()
    if binary is None:
        return None, OPENSSL_HINT
    result = subprocess.run([binary] + args, cwd=str(cwd), capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


def certificate_names(chain_path, cwd):
    """(subject, issuer) of the first certificate in a PEM file, through openssl x509."""
    code, out = run_openssl(["x509", "-in", str(chain_path), "-noout", "-subject", "-issuer"], cwd)
    if code != 0:
        return None, None
    subject = re.search(r"subject=\s*(.+)", out)
    issuer = re.search(r"issuer=\s*(.+)", out)
    return (
        subject.group(1).strip() if subject else None,
        issuer.group(1).strip() if issuer else None,
    )


def key_fingerprint(public_key):
    """sha256: over the DER SubjectPublicKeyInfo, the same value `openssl pkey -outform DER | shasum -a 256` gives."""
    return "sha256:" + hashlib.sha256(ED25519_SPKI_PREFIX + public_key).hexdigest()


def tsa_time(token_path, cwd):
    code, out = run_openssl(["ts", "-reply", "-in", str(token_path), "-text"], cwd)
    if code != 0:
        return None
    match = re.search(r"Time stamp:\s*(.+)", out)
    return match.group(1).strip() if match else None


def verify_pack(root, report):
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    public_key = public_key_from_pem((root / manifest["key"]).read_bytes())
    fingerprint = key_fingerprint(public_key)
    if manifest.get("key_fingerprint") and manifest["key_fingerprint"] != fingerprint:
        report.fail(
            "public key fingerprint",
            "the key file hashes to %s but the pack says %s"
            % (fingerprint, manifest["key_fingerprint"]),
        )
    else:
        report.ok("public key fingerprint", "%s = %s" % (manifest["key"], fingerprint))
    binary = find_openssl()
    if binary is None:
        report.skip("openssl", OPENSSL_HINT)
    else:
        report.ok("openssl", "%s (%s)" % (_OPENSSL["version"], binary))

    entries = {}
    for item in manifest["entries"]:
        seq = item["seq"]
        path = root / item["file"]
        data = path.read_bytes()
        try:
            entry = json.loads(data)
        except ValueError:
            report.fail("entry E%d is JSON" % seq)
            continue
        if canonicalize(entry) != data or entry.get("seq") != seq:
            report.fail("entry E%d is canonical and carries its number" % seq)
            continue
        entry_hash = sha256(ENTRY_TAG + data)
        entries[seq] = (entry, entry_hash)
        report.ok("entry hash E%d" % seq, "sha256:%s…" % entry_hash.hex()[:16])
        if item.get("payload"):
            try:
                disclosed = json.loads((root / item["payload"]).read_text(encoding="utf-8"))
                salt = bytes.fromhex(disclosed["salt"])
                commitment = sha256(salt + canonicalize(disclosed["payload"]))
            except (ValueError, KeyError, TypeError) as exc:
                report.fail("payload commitment E%d" % seq, "payload file unreadable: %s" % exc)
                continue
            if "sha256:" + commitment.hex() == entry.get("payload_commitment"):
                report.ok(
                    "payload commitment E%d" % seq, "the disclosed payload is the recorded one"
                )
            else:
                report.fail("payload commitment E%d" % seq, "the payload does not match the entry")

    seals = {}
    for item in manifest["seals"]:
        no = item["no"]
        body = (root / item["body"]).read_bytes()
        signature = (root / item["signature"]).read_bytes()
        stamped = (root / item["stamped"]).read_bytes()
        try:
            fields = json.loads(body)
        except ValueError:
            report.fail("seal %d is JSON" % no)
            continue
        if canonicalize(fields) != body or fields.get("no") != no:
            report.fail("seal %d is canonical" % no)
            continue
        seals[no] = fields
        if not ed25519_verify(public_key, body, signature):
            report.fail(
                "seal signature %d" % no, "the signature does not verify with the public key"
            )
        elif fields.get("key_id") not in (None, fingerprint):
            report.fail(
                "seal signature %d" % no,
                "signed by key %s, not the pack's key %s" % (fields.get("key_id"), fingerprint),
            )
        else:
            report.ok("seal signature %d" % no, "Ed25519 signature by key %s" % fingerprint)
        if stamped != body + signature:
            report.fail("seal %d stamped bytes" % no, "stamped file is not seal + signature")
        if not item.get("anchors"):
            report.skip(
                "timestamps on seal %d" % no, "the pack holds no trusted timestamp for this seal"
            )
        for anchor in item.get("anchors", []):
            label = "timestamp %s on seal %d" % (anchor["token"], no)
            code, out = run_openssl(
                [
                    "ts",
                    "-verify",
                    "-in",
                    anchor["token"],
                    "-data",
                    item["stamped"],
                    "-CAfile",
                    anchor["chain"],
                ],
                root,
            )
            if code is None:
                report.skip(label, "not checked: " + out)
            elif code == 0 and "Verification: OK" in out:
                when = tsa_time(anchor["token"], root) or anchor.get("gen_time", "?")
                subject, issuer = certificate_names(anchor["chain"], root)
                who = "%s (%s)" % (
                    anchor.get("url") or anchor.get("issuer", "?"),
                    subject or "certificate unreadable",
                )
                detail = "openssl ts -verify: OK; time %s; authority %s" % (when, who)
                if is_test_authority(anchor, subject, issuer):
                    detail += "; WARNING: a test or self-run authority, not independent"
                report.ok(label, detail)
            else:
                report.fail(label, "openssl ts -verify refused the token (%s)" % anchor["chain"])

    for item in manifest["proofs"]:
        seq = item["seq"]
        proof = json.loads((root / item["file"]).read_text(encoding="utf-8"))
        if seq not in entries:
            report.fail("chain proof E%d" % seq, "entry missing")
            continue
        try:
            head = sha256(CHAIN_TAG + hex_of(proof["chain_before"]) + entries[seq][1])
            for later in proof["later_entry_hashes"]:
                head = sha256(CHAIN_TAG + head + hex_of(later))
            expected = hex_of(proof["chain_head"])
        except (ValueError, KeyError) as exc:
            report.fail("chain proof E%d" % seq, "unreadable: %s" % exc)
            continue
        if head != expected:
            report.fail("chain proof E%d" % seq, "the folded chain does not reach the stated head")
            continue
        seal_no = item.get("seal_no")
        if seal_no is None:
            report.ok("chain proof E%d" % seq, "reaches the chain head, not yet sealed")
            continue
        fields = seals.get(seal_no)
        if fields is None:
            report.fail("chain proof E%d" % seq, "seal %s is not in the pack" % seal_no)
        elif (
            fields.get("chain_head") != proof["chain_head"]
            or fields.get("up_to_seq") != proof["up_to_seq"]
        ):
            report.fail("chain proof E%d" % seq, "seal %d does not commit to this head" % seal_no)
        else:
            report.ok("chain proof E%d" % seq, "folds to the head that seal %d signs" % seal_no)

    for item in manifest.get("attachments", []):
        path = root / item["file"]
        digest = sha256(path.read_bytes()).hex()
        entry = entries.get(item["entry_seq"])
        recorded = item.get("sha256_plain")
        if entry is not None and (root / ("payloads/%d.json" % item["entry_seq"])).exists():
            payload = json.loads(
                (root / ("payloads/%d.json" % item["entry_seq"])).read_text(encoding="utf-8")
            )
            recorded = payload["payload"].get("sha256_plain", recorded)
        if digest == recorded:
            report.ok(
                "attachment %s" % item["file"], "SHA-256 matches entry E%d" % item["entry_seq"]
            )
        else:
            report.fail("attachment %s" % item["file"], "the file was changed")
    return manifest, entries, seals


def is_test_authority(anchor, subject, issuer):
    """A timestamp from the pack maker's own machine or a self-signed test certificate proves nothing."""
    url = (anchor.get("url") or anchor.get("issuer") or "").lower()
    if "127.0.0.1" in url or "localhost" in url:
        return True
    if subject and issuer and subject == issuer:
        return True
    return bool(subject and "test" in subject.lower())


def conclusion(manifest, seals, root):
    lines = []
    summaries = {int(r["seq"]): r for r in manifest.get("record_summaries", [])}
    for seq in manifest.get("records", []):
        summary = summaries.get(seq, {})
        if summary.get("recorded_at"):
            lines.append(
                "E%d: recorded %s according to Telegram and Factual (their own clocks, not proven)."
                % (seq, summary["recorded_at"])
            )
        proof = next((p for p in manifest["proofs"] if p["seq"] == seq), None)
        if proof is None or proof.get("seal_no") is None:
            lines.append(
                "E%d is recorded and chained, but not yet sealed: "
                "no third-party time covers it yet." % seq
            )
            continue
        seal_no = proof["seal_no"]
        item = next(s for s in manifest["seals"] if s["no"] == seal_no)
        proven = []
        warnings = []
        for anchor in item.get("anchors", []):
            when = tsa_time(anchor["token"], root) or anchor.get("gen_time")
            subject, issuer = certificate_names(anchor["chain"], root)
            who = anchor.get("url") or anchor.get("issuer", "?")
            if when:
                proven.append("%s by %s%s" % (when, who, " [%s]" % subject if subject else ""))
            if is_test_authority(anchor, subject, issuer):
                warnings.append(who)
        if proven:
            lines.append(
                "E%d existed with exactly this content no later than: %s (seal %d)."
                % (seq, "; ".join(proven), seal_no)
            )
        else:
            lines.append(
                "E%d is sealed in seal %d; the seal has no trusted timestamp in this pack."
                % (seq, seal_no)
            )
        if warnings:
            lines.append(
                "  WARNING: %s is a test or self-run authority; its time is not independent evidence."
                % " and ".join(sorted(set(warnings)))
            )
    return lines


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    target = Path(argv[0]) if argv else Path.cwd()
    tmp = None
    if target.is_file() and target.suffix == ".zip":
        tmp = tempfile.TemporaryDirectory()
        with zipfile.ZipFile(target) as zf:
            zf.extractall(tmp.name)
        root = Path(tmp.name)
    else:
        root = target
    if not (root / "manifest.json").exists():
        print("No manifest.json here. Run this inside the unpacked evidence pack, or pass the zip.")
        return 2
    report = Report()
    try:
        manifest, entries, seals = verify_pack(root, report)
    except Exception as exc:  # noqa: BLE001 — any crash is a failed verification
        report.fail("pack structure", "%s: %s" % (type(exc).__name__, exc))
        manifest, seals = None, {}
    print("\n".join(report.lines))
    print()
    if report.failures:
        print("FAILED: %d check(s) failed. Do not trust this pack." % report.failures)
        return 1
    if report.skipped:
        print(
            "INCOMPLETE: %d check(s) could not be run (SKIP lines above). The pack is not refuted, "
            "but the timestamps are not confirmed. %s" % (report.skipped, OPENSSL_HINT)
        )
        for line in conclusion(manifest, seals, root):
            print("  " + line)
        if tmp is not None:
            tmp.cleanup()
        return 2
    print("VERIFIED: every check passed.")
    for line in conclusion(manifest, seals, root):
        print("  " + line)
    print(
        "  Note: README.txt and manifest.json are unprotected labels; what is proven is the content "
        "of entries/, payloads/ and blobs/ as checked above."
    )
    if tmp is not None:
        tmp.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
