"""A local RFC 3161 time-stamping authority for tests, built on `openssl ts`.

One HTTP server answers two paths (/tsa1 and /tsa2) as two "authorities" with the
same test CA; /broken answers 500 until `broken` is set to False.
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

TSA_CNF = """
[ req ]
distinguished_name = dn
prompt = no
[ dn ]
CN = Factual Test TSA
[ tsa_ext ]
keyUsage = critical, digitalSignature, nonRepudiation
extendedKeyUsage = critical, timeStamping
[ ca_ext ]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
[ tsa ]
default_tsa = tsa_config1
[ tsa_config1 ]
dir = {dir}
serial = $dir/tsaserial
crypto_device = builtin
signer_cert = $dir/tsa.crt
certs = $dir/ca.crt
signer_key = $dir/tsa.key
signer_digest = sha256
default_policy = 1.3.6.1.4.1.99999.1.1
other_policies = 1.3.6.1.4.1.99999.1.2
digests = sha256, sha384, sha512
accuracy = secs:1, millisecs:0, microsecs:0
ordering = no
tsa_name = yes
ess_cert_id_chain = no
ess_cert_id_alg = sha256
"""


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


class LocalTsa:
    def __init__(self, days: int = 3650) -> None:
        """`days`: validity of the TSA certificate (30 makes an authority that expires soon)."""
        self.dir = Path(tempfile.mkdtemp(prefix="factual-tsa-"))
        self.days = days
        self.broken = False
        self.requests: list[str] = []
        self._make_certs()
        self._server = HTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _make_certs(self) -> None:
        d = self.dir
        (d / "tsa.cnf").write_text(TSA_CNF.format(dir=d))
        _run(
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-keyout",
            "ca.key",
            "-out",
            "ca.crt",
            "-days",
            "3650",
            "-subj",
            "/CN=Factual Test CA",
            "-config",
            "tsa.cnf",
            "-extensions",
            "ca_ext",
            cwd=d,
        )
        _run(
            "openssl",
            "req",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-keyout",
            "tsa.key",
            "-out",
            "tsa.csr",
            "-subj",
            "/CN=Factual Test TSA",
            "-config",
            "tsa.cnf",
            cwd=d,
        )
        _run(
            "openssl",
            "x509",
            "-req",
            "-in",
            "tsa.csr",
            "-CA",
            "ca.crt",
            "-CAkey",
            "ca.key",
            "-CAcreateserial",
            "-out",
            "tsa.crt",
            "-days",
            str(self.days),
            "-extfile",
            "tsa.cnf",
            "-extensions",
            "tsa_ext",
            cwd=d,
        )
        (d / "tsaserial").write_text("01\n")

    @property
    def ca_pem(self) -> bytes:
        return (self.dir / "ca.crt").read_bytes()

    def url(self, name: str = "tsa1") -> str:
        return f"http://127.0.0.1:{self.port}/{name}"

    def reply(self, query: bytes) -> bytes:
        result = subprocess.run(
            ["openssl", "ts", "-reply", "-queryfile", "/dev/stdin", "-config", "tsa.cnf"],
            cwd=self.dir,
            input=query,
            capture_output=True,
            check=True,
        )
        return result.stdout

    def _handler(self):
        tsa = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — http.server API
                length = int(self.headers.get("Content-Length", "0"))
                query = self.rfile.read(length)
                tsa.requests.append(self.path)
                if self.path == "/broken" and tsa.broken:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"tsa down")
                    return
                try:
                    body = tsa.reply(query)
                except subprocess.CalledProcessError:
                    self.send_response(400)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/timestamp-reply")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:  # silence
                return None

        return Handler

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
