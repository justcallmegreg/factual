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

### Commands

`/help` (and `/start`) print the command list; the same list is published as Telegram's command menu with `setMyCommands` at start-up, from `app/commands/menu.py`. `/verify`, as a reply or with an entry number, recomputes the entry hash, the payload commitment and the chain link of one entry and shows its filing history and versions. `/status` shows the polling state, the ledger head, the records without a project and, as later stories add them, the last seal, the key fingerprint and the last audit. Without `ANTHROPIC_API_KEY`, `/ask` stops at the verbatim hits and `/respond` answers that Claude is not configured.

### Search

`/ask <question>` searches record text, captions and (later) derived text with PostgreSQL full-text search: the `search_chunk` projection holds one row per text or caption with a generated `english` tsvector and a GIN index. The question's words are stemmed and combined with OR, hits are ranked with `ts_rank_cd`, one hit per record; when the question names a project, that project's records come first. The renderer prints only stored text with the entry number, the project, the origin, the time and the integrity status. With no match, Thoth lists the words it searched for (stop words left out).

Measured on 21 November 2026 with 50,000 seeded records (PostgreSQL 17 in Docker on an Apple silicon laptop, questions matching most records): median 247 ms, worst 359 ms per search. The test is `FACTUAL_PERF=1 pytest tests/test_search.py -k fifty_thousand`.

### Attachments

A photo, document, voice note or video is listed in the `record.created` entry and fetched at once by a job on the PostgreSQL queue (`SELECT … FOR UPDATE SKIP LOCKED`, retries after 30 s, 60 s, 2 min, … up to five attempts). The worker streams the file from Telegram and, in one pass, hashes the plaintext (SHA-256), encrypts it with its own AES-256-GCM key and hashes the ciphertext; the file key is wrapped by the master key from `MASTER_KEY_FILE`. The ciphertext is uploaded under `blobs/<sha256 of the plaintext>` with an `x-amz-checksum-sha256` the store verifies, into a bucket created with Object Lock (compliance mode, retention `S3_OBJECT_LOCK_DAYS`; `0` switches the lock off). The `attachment.stored` entry holds both hashes, the size, the detected type, the object key, the version id and the retention date; the wrapped key lives only in the `blob` table.

A file over 20 MB (the cloud Bot API limit) gets an `attachment.unavailable` entry at ingestion and Thoth tells the sender at once. A download that keeps failing ends the same way after the last attempt, with a notice in the chat. Photos sent as one album form one record: every item is its own chained entry, but the later items reference the first one and their files join its record, which carries the one ✍ mark and the one picker.

### Seals

Every `SEAL_INTERVAL_SECONDS` (default 300) the sealer looks for entries beyond the last seal and, when there are any, writes a seal: `{v, no, up_to_seq, chain_head, prev_seal, created_at, key_id}` as canonical JSON, where `chain_head` is the chain value at the last entry and `prev_seal` is `SHA-256("ledger-seal-v1" ‖ previous seal)` (for the first seal, the hash of the chain's genesis value). The seal is signed with Ed25519 by the key in `SIGNING_KEY_FILE`; `key_id` is the key's fingerprint, `sha256:` over its DER public key, which `/status` shows. `/verify` and every `/ask` hit show the first seal that covers the entry, after checking the chain proof from the entry to that seal's head and the seal's signature. The `seal` table is insert-only like the other evidence tables.

### Trusted timestamps

After every seal, one job per address in `TSA_URLS` requests an RFC 3161 token over `SHA-256(seal ‖ signature)` with a fresh nonce and the signer certificates included. The response is checked (granted, imprint equal to the digest, nonce echoed) and stored in `seal_anchor` with the TSA's certificate chain, its time and its policy; `openssl ts -verify -in <token> -data <seal ‖ signature> -CAfile <chain>` accepts every stored token, which is exactly the check an evidence pack's reader runs. A failed request is retried with a growing delay (30 s, 1 min, 2 min, …, ten attempts); after three failed attempts Thoth reports it in the capture chat, and after the last one it says that the seal has no timestamp from that authority.

Pick authorities whose tokens include their full certificate chain, so that the token verifies offline with its own chain: `https://freetsa.org/tsr` and `http://zeitstempel.dfn.de` (the defaults) do, as does `http://tsa.belgium.be/connect`. DigiCert's and Sectigo's tokens do not include their root certificate and cannot be verified from the pack alone, so they are not recommended. Responses that the strict parser refuses are read with `openssl ts -reply -text` instead.

The tests run a local authority built on `openssl ts` (`tests/tsa_server.py`); one test also stamps against the two public defaults when the network allows.

### Optional anchors

Two stronger anchors are off by default.

**Qualified timestamp.** With `QUALIFIED_TSA_URL` set, the first seal of each local day also gets a token from that authority (kind `qualified_tsa`). Under eIDAS Article 41(2) a qualified electronic time stamp is presumed accurate in date and time and in the integrity of the bound data; because the chain links every entry, one stamp a day covers everything before it. Qualified authorities sell per stamp and usually need credentials: put them in the address (`https://account:secret@tsa.example/tsr`). They are sent as HTTP basic authentication and never stored: the ledger, the jobs and the logs hold the address without them. The choice of the day's seal is recorded in `schedule_run` (job `qualified_tsa`), so a restart does not stamp twice. Packs carry the qualified token like any other, and `verify.py` names it "qualified timestamp".

**Renewal before a certificate expires.** The deep audit reads the certificate chain stored with every token. When an authority's chain expires within 90 days (or has expired), the audit warns and requests, for every token of that authority that has no renewal yet, a new token over the old token from each other configured authority (the Evidence Record Syntax approach, RFC 4998). A renewal is stored with `renews` pointing at the token it covers. Once a token's own certificate has expired, the audit checks it at the time of its verified renewal (`openssl ts -verify -attime`), so the old time stays provable; `verify.py` in the pack does the same.

**OpenTimestamps (Bitcoin anchor): tried, not built.** On 21 November 2026 a test digest was submitted to three public calendars (alice and bob at opentimestamps.org, finney at eternitywall.com); each answered within about a second with a pending attestation of 170 to 191 bytes. Completing the proof needs a second request hours later, after the calendar's Bitcoin transaction has confirmed, and checking a completed proof needs Bitcoin block headers (a node or a trusted block explorer), which the pack's offline `verify.py` cannot do with the standard library and OpenSSL. The two independent TSAs and the optional qualified stamp already give offline-checkable third-party time, so the Bitcoin anchor is not built in this version. The schema keeps the kinds `ots_pending` and `ots_complete` for a later story: a daily submission job, an upgrade step in the nightly audit, and the `.ots` file in the pack for the `ots` client.

### Evidence packs

`/proof`, as a reply to a record or with entry numbers (`/proof E41233 E41502`), posts a zip that anyone can verify offline, without access to Factual. It holds the requested entries as canonical JSON, their payloads with the salt, the entries that belong to them (edits, filings, attachments, album items), a chain proof from each entry to the first seal that covers it, those seals with their signatures and the stamped bytes, the RFC 3161 tokens with the authorities' certificate chains, the instance's public key, the decrypted attachments, a `manifest.json`, a plain-language `README.txt` and `verify.py`. Every other entry of the ledger appears only as a hash inside the chain proofs. A record that is not yet sealed gets a proof to the current chain head and the README says so.

`verify.py` needs Python 3.9 or newer and the `openssl` command, nothing else: it recomputes the entry hashes and payload commitments with its own RFC 8785 canonicalizer, folds the chain proofs, checks the Ed25519 signatures with a plain-Python implementation of RFC 8032, runs `openssl ts -verify` on every token and hashes every attachment, then prints VERIFIED or FAILED and what the pack proves in one sentence per record. The tests run it with `python3 -I -S` so that nothing but the standard library can be imported. A pack over Telegram's 50 MB sending limit is not posted; Thoth points to `GET /api/v1/records/{seq}/proof` instead.

### Claude's verdict

After `/ask` posts the hits, Claude (`CLAUDE_MODEL`, default `claude-sonnet-5-5`, through the Anthropic SDK with `ANTHROPIC_API_KEY`) reads only the retrieved records, inside a `<records>` data block, and may call four read-only tools up to six times: `search_records`, `get_context`, `get_versions` and `get_attachment_text`. It must answer with JSON that matches the verdict schema (structured output: `supported`, `contradicted`, `changed_later`, `not_found` or `unclear`, a one-line summary, cited entry numbers with roles, caveats). Every cited number is checked against the records Claude was shown; a verdict with an unknown entry or none left becomes "No evidence found" followed by the searched words. Thoth then edits the hits message into the verdict line and the cited records printed from the ledger, with the caveats and a `/proof` line. When Claude is slow (15 s), unavailable or declines, the hits stay as the answer with a one-line note. Each run appends `qa.answered` (asker, model, cited entries, verdict, reason) and `ASK_DAILY_LIMIT` counts the runs that reached Claude per local day.

Record text is untrusted: it reaches Claude as data, the tools are read-only (an unknown tool name gets an error result), and the output is schema-checked. The tests drive these guardrails with hostile record text and a scripted Claude; a live test runs when `ANTHROPIC_API_KEY` is set.

### Claim response

`/respond`, as a reply to the record that holds the claim or with the claim pasted after the command, tests the claim against the records and posts two messages: a BRIEF (the claim, each assertion with its verdict, the timeline, the shifts before → after with who changed what and whether a record shows that the change was communicated, the gaps with the search scope, the records that support the claim, the evidence with its integrity status and `/proof` command) and a DRAFT reply that the user edits and sends themselves. The instruction set is the skill folder `app/skills/claim-response/` (`SKILL.md`, `references/phrasebook.md`, `references/ste-rules.md`, `references/output-template.md`, `scripts/check_draft.py`); the same folder works as a skill in Claude Code. Claude gets the skill as its system prompt, the claim inside `<claim>` tags (input, never evidence), the matching records, and the read-only tools of `/ask` plus `find_later_changes` and `verify_entry`. Claude writes `[[quote:N]]` placeholders; the system inserts the stored text (a note is labelled "(note by …)") and refuses records whose integrity check failed, naming them. `scripts/check_draft.py` is the gate: each factual sentence cites an entry, no sentence is longer than 20 words, no banned phrase ("never", "obviously", legal labels, blame), each gap names its search scope. A draft that fails is sent back to Claude once with the errors; if it still fails, the brief is posted and the draft is withheld with the reasons. Each run appends `qa.answered` (kind `respond`) with the cited entries, the skill version and whether a draft was posted.

### Daily help

At `DAILY_HELP_TIME` (default 06:00) in `TIMEZONE`, Thoth posts the command list and a status line (records without a project, the time of the last seal, the result of the last audit) in the capture chat and deletes the previous day's message. Each run is a row in `schedule_run`, keyed by the day's 06:00 instant, so a restart never sends twice; after an outage the message is still sent until 09:00, then the day is skipped. Daylight-saving changes are handled by the time zone (06:00 local in winter and summer). If no message arrives by 09:00, Thoth is down or was down that morning.

### Text from images and PDFs

After `attachment.stored`, a job sends each image (JPEG, PNG, GIF, WebP up to 5 MB) and each PDF (up to 20 MB and 100 pages, counted with pypdf) to Claude with the instruction to transcribe its text verbatim. The result lands in the `derived_text` projection with the model name and the time, and in the search index as a `derived` chunk, so `/ask` finds a photographed letter or a scanned contract; hits and answers show it as `[text read from <file>]`. Derived text is never evidence: the file and its hash in the `attachment.stored` entry stay the record. Files over the limits, unreadable PDFs and other types are recorded as skipped with the reason; a failing extraction is retried like any job and finally marked failed.

### Nightly deep audit

Between 03:00 and 06:00 local time (once per night, recorded in `schedule_run`), and with `python -m app audit` at any time (for example after a restore, before Thoth starts), the deep audit checks the whole chain against every seal, every seal signature and every timestamp token with `openssl ts -verify`, re-hashes every stored attachment from object storage at a limited rate (two objects per second by default, decrypting each to check both hashes), warns when a TSA certificate has expired or expires within 90 days, and asks again for timestamps that a seal still lacks from a configured authority. Each run appends `audit.completed` with the counts and the findings; a failure is posted in the capture chat with the exact entry (`E41233: payload commitment mismatch`), seal (`seal 482: signature invalid`) or object (`blobs/…@version: stored bytes changed`). `/status`, the daily help and `GET /api/v1/status` show the last result.

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
| `CLAUDE_EFFORT` | `low` | Effort for `/ask` verdicts and text extraction: `low`, `medium` or `high`; `/respond` uses at least `medium` |
| `ASK_DAILY_LIMIT` | `200` | Questions per day |
| `SIGNING_KEY_FILE` | required | Ed25519 key that signs the seals |
| `MASTER_KEY_FILE` | required | Key that wraps the attachment keys |
| `TSA_URLS` | required | Two RFC 3161 TSA addresses, separated by a comma |
| `QUALIFIED_TSA_URL` | unset | Optional qualified TSA, used for the first seal of each day; credentials go in the address and are never stored |
| `SEAL_INTERVAL_SECONDS` | `300` | Time between seals |
| `TIMEZONE` | `Europe/Amsterdam` | Time zone for shown times and for the daily help |
| `DAILY_HELP_TIME` | `06:00` | Time of the daily help message |
| `ACK_REACTION` | `✍` | The mark on recorded messages |
| `API_TOKEN` | unset | Bearer token for `/api/v1`; unset switches these routes off |
| `AUDIT_ON_START` | `restore` | Deep audit at start-up: `restore` when the database was restored into another cluster, `always` at every start, `never` switches the check off |

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

The `/api/v1` routes are read-only and need `Authorization: Bearer <API_TOKEN>` (401 otherwise). When `API_TOKEN` is not set they answer 404 with "The API is switched off" and are, for all purposes, off. `GET /api/v1/records` takes `project`, `from`, `to` (ISO 8601 instants, on the recorded time), `text` (a substring), `limit` and `offset`, newest first; `GET /api/v1/search?q=` returns the same hits as `/ask`; `GET /api/v1/records/{seq}/proof` streams the evidence pack zip, which is also the way to fetch a pack over Telegram's 50 MB limit. Every record carries its integrity status (`Verified` with the seal number, `Pending seal` or `Failed`).

Example:

```sh
curl -H "Authorization: Bearer $API_TOKEN" http://localhost:8000/api/v1/search?q=kickoff
curl -H "Authorization: Bearer $API_TOKEN" -o proof.zip http://localhost:8000/api/v1/records/41233/proof
```

The signing key is bound to the instance: the service refuses to start when `SIGNING_KEY_FILE` is not the key the instance was created with, because seals signed with another key would not verify.

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

A lost server must not mean lost evidence. Three things hold it, and all three are backed up:

| What | Holds | Backup |
| --- | --- | --- |
| PostgreSQL database | The ledger, the seals and their timestamp tokens, the records, the attachment rows with both hashes | `pg_dump` after every nightly audit, kept off the server; once, the roles |
| Object storage bucket | The encrypted attachments | Replication or a nightly sync to a second bucket with Object Lock, in another account or region |
| Key files | `signing-key.pem` signs the seals; `master.key` wraps every attachment key | Copied once to two offline places. Without `master.key` no attachment can be decrypted; without the signing key no new seal can be made, but every old seal still verifies with the public key in `instance` |

Object Lock protects every stored object against change and early deletion, but not against the loss of the store itself: that is what the second bucket is for. The audit binds the bytes by their hashes, not by the store's version ids, so a bucket synced back from a backup (new version ids) passes the audit: Factual fetches the current version when the stored version id is unknown and checks the hash either way.

### Backup

```sh
# the database, custom format, compressed; run it after the nightly audit (after 06:00 local)
pg_dump --format=custom --no-owner --dbname "$DATABASE_URL" > factual-$(date +%F).dump
# once, and again when roles change: the role factual_runtime is cluster-level, not part of a database dump
pg_dumpall --roles-only --dbname "$DATABASE_URL" > factual-roles.sql
# the bucket, to a second bucket with Object Lock (any S3 sync tool; versions are not needed)
aws s3 sync s3://factual s3://factual-backup --endpoint-url "$S3_ENDPOINT"
```

Keep at least seven daily dumps plus one per month. A dump restores with the `pg_restore` of the same or a newer PostgreSQL major version.

### Restore, on a fresh machine

1. Install PostgreSQL 17 and an S3-compatible store, or point `DATABASE_URL` and `S3_ENDPOINT` at the replacements.
2. Create the empty database and the runtime role, then restore the dump:
   ```sh
   createdb factual
   psql --dbname "$DATABASE_URL" -c "CREATE ROLE factual_runtime NOLOGIN" -c "GRANT factual_runtime TO factual"
   pg_restore --no-owner --exit-on-error --dbname "$DATABASE_URL" factual-2026-11-20.dump
   ```
   The dump holds the schema, the data, the insert-only triggers, the grants and `alembic_version`, so the migrations do not run again.
3. Restore the bucket: `aws s3 sync s3://factual-backup s3://factual`, or set `S3_BUCKET=factual-backup`.
4. Put `signing-key.pem` and `master.key` where `SIGNING_KEY_FILE` and `MASTER_KEY_FILE` point.
5. Run the deep audit before Thoth starts: `python -m app audit --reason restore` (in the container: `docker compose run --rm backend python -m app audit --reason restore`). It checks the whole chain against every seal, every signature and token, re-hashes every attachment and prints `PASSED` or the exact entry, seal or object that failed. Only start Thoth after `PASSED`; a failure means the backup is damaged or incomplete: take an older dump.
6. Start Thoth (`make up`). The start-up check compares the database cluster's identifier with the one recorded at the last start or audit (table `boot`); after the manual audit they match, so Thoth starts at once. Without the manual audit, the start detects the new cluster and runs the deep audit itself before the sealer, the jobs and the connector start; a failing audit stops the start with the finding in the log (`AUDIT_ON_START=never` overrides, `always` audits at every start).

### Restore drill

The test `tests/test_backup_restore.py::test_the_restore_drill_dumps_restores_into_a_fresh_database_and_audits` performs the drill: it fills a database with records, attachments, a seal and two timestamp tokens, dumps it, restores it into a fresh database and runs the deep audit against the stored seals, tokens and objects. It needs a `pg_dump` of the server's major version (`FACTUAL_PG_DUMP` and `FACTUAL_PG_RESTORE` may name another command, for example `docker exec -i factual-postgres-1 pg_dump`, with `FACTUAL_PG_TOOLS_HOST=host.docker.internal` for the host name the tools see) and restores into the test cluster, or into another cluster named by `FACTUAL_RESTORE_TARGET`. Run it twice a year against a fresh PostgreSQL container and note the time.

Drill on 21 November 2026, from the test cluster into a fresh `postgres:17` container on the same machine (`FACTUAL_RESTORE_TARGET` pointing at it), with `pg_dump`/`pg_restore` 17 from Homebrew's `libpq`:

| Step | Time |
| --- | --- |
| `pg_dump` (6 entries, 1 seal, 2 tokens, 2 files; 80 KiB) | 0.2 s |
| `pg_restore` into the fresh cluster | 0.5 s |
| Deep audit against the restored database | 0.1 s |
| Whole test including the container start | 5 s |

The times scale with the data: a dump of a year of use (tens of thousands of entries) restores in minutes, and the audit's re-hash of the attachments takes the longest (two objects per second by default).
