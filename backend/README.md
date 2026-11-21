# Factual backend

The backend is one FastAPI service that runs Thoth (the Telegram connector), the ledger, the sealer, the job workers, the scheduler and the read-only HTTP API. It ships as one container image and keeps all state in PostgreSQL and S3-compatible object storage.

Contents: [Prerequisites](#prerequisites) · [Set-up](#set-up) · [Environment variables](#environment-variables) · [Makefile targets](#makefile-targets) · [API and swagger.json](#api-and-swaggerjson) · [Logging](#logging) · [Tests](#tests) · [Migrations](#migrations) · [Backup and restore](#backup-and-restore)

## Prerequisites

- Python 3.12 or newer for `make run` and the tests.
- Docker with Compose and Buildx for `make up` and `make build`.
- PostgreSQL 14 or newer and an S3-compatible object store with Object Lock (the Compose stack provides both for development).
- A Telegram bot token from @BotFather, the Claude API key, and the addresses of two RFC 3161 time-stamping authorities.

## Set-up

Thoth serves exactly one private Telegram group, the capture chat.

1. Create the bot with @BotFather, name it Thoth, and disable privacy mode with `/setprivacy` so that it receives every message in the group. A privacy change only applies after the bot is removed from the group and added again.
2. Create a private group, add the people who may record and recall, and add Thoth.
3. `cp .env.example .env`, set `TELEGRAM_BOT_TOKEN` and the other required variables, and run `make keys`: it creates the Ed25519 signing key and the master key in `./secrets` (mode 0600, never overwritten). The chain's genesis value is derived from the public key, so keep the signing key as safe as the database.
4. Start the backend with `TELEGRAM_CAPTURE_CHAT_ID` unset: Thoth records nothing and replies in the group with the group's chat ID.
5. Set `TELEGRAM_CAPTURE_CHAT_ID` to that ID and restart. Thoth now records every message in the group, leaves every other group and answers a private message with one line.

Everyone in the capture chat can use every command and every project, so membership of the group is the only access control. Keep the group private.

## Environment variables

Factual is configured with environment variables only. `.env.example` lists every variable; copy it to `.env`, which is never committed.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LOG_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` |
| `TELEGRAM_BOT_TOKEN` | required | Thoth's token from @BotFather |
| `TELEGRAM_CAPTURE_CHAT_ID` | unset | The one group that Thoth serves. While it is unset, Thoth records nothing and only reports the ID of its group |
| `DATABASE_URL` | required | PostgreSQL connection |
| `S3_ENDPOINT`, `S3_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | required | Object storage for attachments |
| `S3_OBJECT_LOCK_DAYS` | `3650` | Retention for attachments; `0` switches Object Lock off |
| `ANTHROPIC_API_KEY` | required | Key for the Claude API |
| `CLAUDE_MODEL` | `claude-sonnet-5-5` | Model for `/ask` and `/respond` |
| `ASK_DAILY_LIMIT` | `200` | Questions per day |
| `SIGNING_KEY_FILE` | required | Ed25519 key that signs the seals |
| `MASTER_KEY_FILE` | required | Key that wraps the attachment keys |
| `TSA_URLS` | required | Two RFC 3161 TSA addresses, separated by a comma |
| `QUALIFIED_TSA_URL` | unset | Optional qualified TSA, used once a day |
| `SEAL_INTERVAL_SECONDS` | `300` | Time between seals |
| `TIMEZONE` | `Europe/Amsterdam` | Time zone for shown times and for the daily help |
| `DAILY_HELP_TIME` | `06:00` | Time of the daily help message |
| `ACK_REACTION` | `✍` | The mark on recorded messages |
| `API_TOKEN` | unset | Bearer token for `/api/v1`; unset switches these routes off |

The service refuses to start when a required variable is missing and names the variable.

## Makefile targets

`make` without a target prints the help.

| Target | What it does |
| --- | --- |
| `help` | Lists all targets with a one-line description |
| `build` | Builds one image for `linux/amd64` and `linux/arm64`, tagged with the version from `../VERSION.txt` |
| `up` | Starts the backend, PostgreSQL and an S3-compatible store with Docker Compose |
| `down` | Stops the Compose stack |
| `keys` | Creates the signing key and the master key in `./secrets` |
| `run` | Runs the backend on your machine with auto-reload |
| `swagger`, `swagger-check` | Regenerates `swagger.json`, or fails if it is out of date |
| `test`, `lint` | Runs pytest and Ruff |

## Running with Docker Compose

`make up` builds the image and starts three services with `docker-compose.yml`:

| Service | Image | Host port | Notes |
| --- | --- | --- | --- |
| `backend` | `factual-backend:<version>`, built from `Dockerfile` | `8000` | Reads `.env`; the Compose service names replace the localhost addresses |
| `postgres` | `postgres:17` | `5432` | User, password and database `factual`; data in the `pgdata` volume |
| `s3` | `rustfs/rustfs:1.0.6` | `9000` | S3-compatible store with Object Lock; credentials from `S3_ACCESS_KEY_ID` and `S3_SECRET_ACCESS_KEY`; data in the `s3data` volume |

- `.env` keeps localhost addresses so that `make run` works on your machine; the Compose file swaps in the service names for the container. The keys in `./secrets` are mounted at `/run/secrets`.
- When a default host port is taken, set `BACKEND_PORT`, `POSTGRES_PORT` or `S3_PORT` in your shell before `make up`, for example `BACKEND_PORT=18080 make up`.
- `make down` stops the stack and keeps the volumes.
- RustFS replaces MinIO, whose `minio/minio` image is no longer published on Docker Hub. It was checked with the calls Factual makes: a bucket created with Object Lock, a versioned upload with an SHA-256 checksum and a compliance retention date, a rejected upload with a wrong checksum, and a refused delete of a locked version.

## Container image

One image, one process: `python -m app` starts Uvicorn, and the app's lifespan starts the Telegram connector, the job workers, the sealer and the scheduler.

- Built from `python:3.12-slim` with `openssl` for RFC 3161 checks; runs as the non-root user `factual` (uid 10001); exposes port 8000; health check on `GET /healthz`.
- `make build` builds one image for `linux/amd64` and `linux/arm64` with `docker buildx`, tagged with the version from `../VERSION.txt`, which the build passes in as `APP_VERSION`. Docker Desktop and Docker Engine 29 or newer build both platforms without extra set-up; an older engine needs the containerd image store or a `docker-container` builder, which cannot load the result locally: run `make build BUILD_ARGS=--push` to push to a registry.
- Architecture test on 21 November 2026: the `arm64` image ran natively on an Apple silicon Mac, and the `amd64` image ran on the same machine under emulation (`uname -m` reports `x86_64`); both answered `GET /version` as the non-root user. A run on a physical Intel or AMD server is still to be done.

## API and swagger.json

FastAPI generates the OpenAPI description from the code. The running service serves it at `/openapi.json`, Swagger UI at `/docs` and ReDoc at `/redoc`. `make swagger` writes the same description to `swagger.json` without starting a server, and CI fails when the committed file is out of date. `swagger.json` is never edited by hand.

| Endpoint | Purpose | Access |
| --- | --- | --- |
| `GET /healthz` | The process is alive | Open |
| `GET /readyz` | Database, object storage and Telegram polling work | Open |
| `GET /version` | The version from `VERSION.txt` | Open |
| `GET /api/v1/status` | Last seal, last audit, records without a project | Token |
| `GET /api/v1/projects` | Projects with their record counts | Token |
| `GET /api/v1/records` and `/api/v1/records/{seq}` | Records, filtered by project, date and text | Token |
| `GET /api/v1/search` | Full-text search, the same hits as `/ask` | Token |
| `GET /api/v1/records/{seq}/proof` | The evidence pack as a zip | Token |

The `/api/v1` routes are read-only and need `Authorization: Bearer <API_TOKEN>`. When `API_TOKEN` is not set, they are switched off.

## Logging

Every line on standard output is one JSON object with Elastic Common Schema (ECS) field names: `@timestamp`, `log.level`, `message`, `log.logger`, `service.name`, `service.version` and, for errors, `error.*`. Uvicorn's access and error logs use the same handler. `LOG_LEVEL` sets the level; the default is `INFO`.

Logs carry entry numbers, never record text, file contents, tokens or keys.

## Tests

```sh
make test     # pytest
make lint     # ruff check .
```

The tests that touch the database need a PostgreSQL server; `make up` provides one. Tests marked `docker` build and run the container image; they are skipped when no Docker daemon answers, and `pytest -m "not docker"` leaves them out on purpose.

## Continuous integration

GitHub Actions runs `.github/workflows/ci.yml`. Every push runs `make lint`, `make test` (with a PostgreSQL 17 service container) and `make swagger-check`; a push to `main` also runs `make build` for both architectures. Dependencies are cached between runs. To make a failed step block a merge, require the `check` job as a status check in the branch protection rules of `main`.

## Migrations

The schema is under Alembic control in `migrations/`, written by hand (never autogenerated) because the migrations also create roles, grants and triggers. The backend applies `alembic upgrade head` at start, so a new version migrates the database on its first start; running the migrations twice is a no-op.

Two database roles:

- **Owner.** The login role in `DATABASE_URL` owns the schema and runs the migrations. On the first run it needs `CREATEROLE` to create the runtime role (or create the role `factual_runtime` beforehand with `CREATE ROLE factual_runtime NOLOGIN` and grant it to the owner).
- **Runtime.** Every application connection switches to `factual_runtime` when it opens (`options=-c role=factual_runtime`). This role can read and insert everywhere and maintain the projections, but it has no `UPDATE` or `DELETE` privilege on evidence tables.

Evidence tables (`ledger_entry`, `evidence_payload`, later `seal`, `seal_anchor` and `blob`) are insert-only: besides the missing privileges, a statement-level trigger rejects `UPDATE`, `DELETE` and `TRUNCATE` for every role, the owner included. This is defence in depth; the sealed and timestamped chain heads are the real protection.

The database tests use `TEST_DATABASE_URL` (default `postgresql://factual:factual@localhost:5432/factual`, the Compose PostgreSQL) as an admin connection: they migrate one template database per test session and clone it for each test that needs a clean database.

## Backup and restore

Written with the backup and restore story (FAC-26): the database backup, the restore steps, and the audit that runs against the stored seals before Thoth starts again.
