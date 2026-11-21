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

### Connectors

Telegram sits behind the connector interface in `app/connectors/base.py`; the core never imports a platform SDK. Thoth (`app/connectors/telegram/`) polls `getUpdates` with long polling (no inbound port), turns messages, edits, slash commands and button taps into platform-neutral events, and moves its offset past an update only after the ledger has committed it. A crash between the commit and the next poll makes Telegram deliver the update again; the ledger drops the duplicate by its event id. Messages from bots (Thoth's own included), service messages and commands addressed to another bot are not recorded.

Every connector passes the contract tests in `tests/test_connector_contract.py`, which replay recorded updates: a new message, an edit, an attachment, a duplicate delivery, a button tap, and the rule that the acknowledgement follows the commit. The Telegram fixtures in `tests/fixtures/telegram/` were built from the Bot API reference while no bot token was available; replace them with recordings from the real capture chat when you have one.

### Acknowledgement

Thoth marks every message it records with the reaction from `ACK_REACTION` (default ✍), set only after the ledger transaction has committed: a message without the mark was not recorded. When Telegram answers a reaction with a rate limit (HTTP 429, `retry_after`), the mark is queued and set after the requested delay; any other failure is retried with a growing delay, and after six attempts the mark is given up with an `ack.failed` error line, so the missing mark stays visible as the signal. After downtime, the first poll returns the backlog Telegram kept (up to 24 hours): Thoth records every message in order, marks each one late, and logs a `backlog` line with the count.

The real-group test from the specification (does every mark notify the members, and does a busy chat hit Telegram's rate limits?) has **not been run yet**: no bot token was available while this was built. Run it in the capture chat and write the result here:

| Question | Result |
| --- | --- |
| Do members get a notification for each ✍ mark? | not yet observed |
| Does a busy chat (20 messages in a minute) hit rate limits, and does the queue recover? | not yet observed |

### Search

`/ask <question>` searches record text, captions and (later) derived text with PostgreSQL full-text search: the `search_chunk` projection holds one row per text or caption with a generated `english` tsvector and a GIN index. The question's words are stemmed and combined with OR, hits are ranked with `ts_rank_cd`, one hit per record; when the question names a project, that project's records come first. The renderer prints only stored text with the entry number, the project, the origin, the time and the integrity status. With no match, Thoth lists the words it searched for (stop words left out).

Measured on 21 November 2026 with 50,000 seeded records (PostgreSQL 17 in Docker on an Apple silicon laptop, questions matching most records): median 247 ms, worst 359 ms per search. The test is `FACTUAL_PERF=1 pytest tests/test_search.py -k fifty_thousand`.

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
