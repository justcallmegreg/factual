"""`python -m app`: configure logging, then serve the app with Uvicorn.

Uvicorn's own logging configuration is switched off (`log_config=None`), so its
access and error logs, including the start-up lines, go through the same ECS
handler as everything else.

`python -m app keys [--dir DIR]` creates the signing key and the master key.
`python -m app audit` runs the deep audit once and exits with 1 when it fails
(for example after a restore, before Thoth is started again).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from app.core.logging import configure_logging
from app.core.settings import ConfigurationError, Settings
from app.core.version import read_version
from app.ledger.keys import generate_master_key, generate_signing_key


def keys(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app keys", description="Create the signing key and the master key."
    )
    parser.add_argument("--dir", default="secrets", help="directory for the key files")
    args = parser.parse_args(argv)
    directory = Path(args.dir)
    for name, create in (
        ("signing-key.pem", generate_signing_key),
        ("master.key", generate_master_key),
    ):
        path = directory / name
        try:
            create(path)
            print(f"created {path}")
        except FileExistsError:
            print(f"kept {path} (exists)")
    return 0


def audit(argv: Sequence[str]) -> int:
    """Run the deep audit once against the configured database and store."""
    import asyncio

    from app.ledger.audit import DeepAudit
    from app.ledger.keys import load_signing_key, public_key_bytes
    from app.ledger.ledger import Ledger
    from app.storage.blobs import S3ObjectStore
    from app.storage.db import Database, run_migrations

    parser = argparse.ArgumentParser(prog="python -m app audit", description="Run the deep audit.")
    parser.add_argument("--reason", default="manual")
    args = parser.parse_args(argv)
    version = read_version()
    configure_logging("INFO", version)
    try:
        settings = Settings.load()
    except ConfigurationError as exc:
        logging.getLogger("factual").critical(str(exc))
        return 2
    configure_logging(settings.log_level, version, secrets=settings.secret_values())

    async def run() -> int:
        run_migrations(settings.database_url)
        db = Database(settings.database_url)
        try:
            key = load_signing_key(settings.signing_key_file)
            ledger = await Ledger.open(db, public_key=public_key_bytes(key))
            store = S3ObjectStore(
                endpoint=settings.s3_endpoint,
                bucket=settings.s3_bucket,
                access_key_id=settings.s3_access_key_id,
                secret_access_key=settings.s3_secret_access_key,
                lock_days=settings.s3_object_lock_days,
            )
            master_key = settings.master_key_file.read_bytes()
            result = await DeepAudit(
                ledger, db, store, master_key, None, None, public_key=ledger.public_key
            ).run(reason=args.reason)
        finally:
            await db.dispose()
        for failure in result.failures:
            print(f"FAIL  {failure.line()}")
        for warning in result.warnings:
            print(f"WARN  {warning}")
        print(
            f"{'PASSED' if result.ok else 'FAILED'}: {result.entries} entries, "
            f"{result.seals} seals, "
            f"{result.tokens} tokens, {result.blobs} files (audit entry E{result.entry_seq})"
        )
        return 0 if result.ok else 1

    return asyncio.run(run())


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "keys":
        return keys(argv[1:])
    if argv and argv[0] == "audit":
        return audit(argv[1:])
    parser = argparse.ArgumentParser(prog="python -m app", description="Run the Factual backend.")
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 — the container's own port
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="restart on code changes")
    args = parser.parse_args(argv)

    version = read_version()
    configure_logging("INFO", version)
    try:
        settings = Settings.load()
    except ConfigurationError as exc:
        logging.getLogger("factual").critical(str(exc))
        return 2
    configure_logging(settings.log_level, version, secrets=settings.secret_values())

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
