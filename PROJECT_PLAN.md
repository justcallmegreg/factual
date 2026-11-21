# Factual — Project Plan

21 November 2026 · Greg Nagy

27 user stories in three phases build Factual v1 in the order of the roadmap, and each phase ends with a gate. The stories implement `SPECIFICATION.md`.

## How to use this plan with Claude Code

- Read `SPECIFICATION.md` first. It is the source of truth; if a story and the specification disagree, stop and ask.
- Work on one story at a time, in the order of the overview. Start a story only when the stories under "Needs" are done.
- A story is done when every "Done when" item is true and its tests pass. Tick each task box when it is finished.
- Stop at each gate and report the result before the next phase starts.
- Do not add features that are not in the specification.

## How to read this plan

- **Order:** the stories are in the order of work. "Needs" names the stories that must be done first.
- **Story:** each one has a user story, a "Done when" list and tasks to tick off.
- **Size:** a rough estimate for one developer. S is up to 1 day, M is 2 to 3 days and L is about a week.
- **Roles:** a member is anyone in the capture chat, the operator runs the container, a developer works on the code, and an outsider checks an evidence pack.
- **Gates:** a phase is finished when its gate passes, not when its stories are closed.

## Overview

Phases 0 to 2 are v1. As a rough estimate for one developer, phase 0 takes about 5 weeks, phase 1 about 5 weeks and phase 2 about 3 weeks.

| Story | Title | Phase | Needs | Size |
| --- | --- | --- | --- | --- |
| FAC-01 | Repository skeleton and versioning | 0 | none | S |
| FAC-02 | FastAPI service skeleton | 0 | 01 | S |
| FAC-03 | Structured logging | 0 | 02 | S |
| FAC-04 | Container and Makefile | 0 | 02 | M |
| FAC-05 | swagger.json generation | 0 | 04 | S |
| FAC-06 | CI pipeline | 0 | 05 | S |
| FAC-07 | Database and migrations | 0 | 04 | M |
| FAC-08 | Ledger entries and hash chain | 0 | 07 | M |
| FAC-09 | Connector interface and Thoth | 0 | 07 | M |
| FAC-10 | Record messages | 0 | 08, 09 | M |
| FAC-11 | Acknowledgement mark | 0 | 10 | S |
| FAC-12 | Projects | 0 | 10 | S |
| FAC-13 | Filing with the project picker | 0 | 12 | M |
| FAC-14 | Search hits with `/ask` | 0 | 10 | M |
| FAC-15 | `/verify`, `/status` and `/help` | 0 | 08, 10 | S |
| FAC-16 | Attachments | 1 | 10 | L |
| FAC-17 | Seals | 1 | 08 | M |
| FAC-18 | Trusted timestamps | 1 | 17 | M |
| FAC-19 | Evidence packs and offline verifier | 1 | 16, 18 | L |
| FAC-20 | Claude verdict for `/ask` | 1 | 14 | L |
| FAC-21 | Daily help message | 1 | 15 | S |
| FAC-22 | Read-only HTTP API | 1 | 14, 19 | M |
| FAC-23 | Text from images and PDFs | 2 | 16, 20 | M |
| FAC-24 | Nightly deep audit | 2 | 18 | M |
| FAC-25 | Claim response skill | 2 | 20 | L |
| FAC-26 | Backup and restore | 2 | 24 | M |
| FAC-27 | Optional anchors | 2 | 18 | S |

| Gate | After | Passes when |
| --- | --- | --- |
| 1 · Core loop works | FAC-15 | You send a note, see ✍, file it with one tap and find it with `/ask`; the chain verifies after a crash |
| 2 · Outsider check | FAC-22 | Someone who does not know the system verifies an evidence pack offline |
| 3 · Stable operation | FAC-27 | 30 nightly audits in a row pass, and a restore drill succeeds |

## Phase 0 · Spike

Fifteen stories build the core loop: record a message, mark it, file it and find it again. Stories 01 to 06 set up the repository and the developer workflow from the technical stack specification.

### FAC-01 · Repository skeleton and versioning

As a developer, I want one repository with a clear layout and a single version file, so that backend and frontend always carry the same version.

**Done when**

- The root holds `VERSION.txt` with `0.1.0`, `README.md`, `backend/` and `frontend/`.
- The root README describes Factual, the repository structure and where the version lives.
- `backend/README.md` and `frontend/README.md` exist; the frontend one says that the folder is empty for now.

**Tasks**

- [x] Create the repository with `.gitignore` and `.editorconfig`.
- [x] Add `VERSION.txt` and write the Semantic Versioning rules into the root README.
- [x] Write the three READMEs from the outline in the specification.

### FAC-02 · FastAPI service skeleton

As a developer, I want a running FastAPI service with typed settings, so that every later feature has a place to live.

**Done when**

- `GET /healthz` returns 200, and `GET /version` returns the content of `VERSION.txt`.
- Settings come from environment variables, and the service refuses to start when a required one is missing.
- `.env.example` lists every variable from the specification.

**Tasks**

- [x] Create `backend/app` with the app factory and the lifespan hook.
- [x] Add the settings class and the version reader (`APP_VERSION`, else `../VERSION.txt`).
- [x] Add the health and version routes with tests.
- [x] Pin the dependencies in `requirements.txt` and `requirements-dev.txt`.

### FAC-03 · Structured logging

As an operator, I want JSON logs with a level that I can set, so that I can follow and filter what the service does.

**Done when**

- `LOG_LEVEL` accepts `DEBUG`, `INFO`, `WARNING`, `ERROR` and `CRITICAL`; the default is `INFO`.
- Every line is one JSON object with ECS field names, including `service.name` and `service.version`.
- Uvicorn's access and error logs use the same format.
- A test proves that record text, tokens and keys do not appear in logs.

**Tasks**

- [x] Configure the standard `logging` module with the ECS formatter on standard output.
- [x] Route the Uvicorn loggers through the same handler.
- [x] Add a helper for context fields such as the entry number and the action.
- [x] Write tests for the levels, the format and the content rule.

### FAC-04 · Container and Makefile

As a developer, I want one command for each daily task, so that building and running Factual is the same on every machine.

**Done when**

- `make help` lists all targets, and `make` alone prints the same list.
- `make build` produces one image for `linux/amd64` and `linux/arm64`, tagged with the version.
- `make up` starts the backend, PostgreSQL and an S3-compatible store; `make down` stops them.
- `make run` starts the backend on the developer's machine with auto-reload.
- The image runs as a non-root user and has a health check on `/healthz`.

**Tasks**

- [x] Write the `Dockerfile` with the `VERSION` build argument.
- [x] Write `docker-compose.yml` for the backend, PostgreSQL and the S3-compatible store.
- [x] Write the `Makefile` with `help`, `build`, `up`, `down`, `run`, `test` and `lint`.
- [x] Run the image on an Intel or AMD machine and on an ARM machine.

### FAC-05 · swagger.json generation

As a developer, I want `swagger.json` generated from the code, so that the API document is always up to date.

**Done when**

- `make swagger` writes `backend/swagger.json` from the FastAPI app without starting a server.
- `make swagger-check` fails when the committed file differs from the generated one.
- Swagger UI at `/docs` shows the same API, with the version from `VERSION.txt`.

**Tasks**

- [x] Write `scripts/export_openapi.py` with a stable key order and indentation.
- [x] Add the two Makefile targets.
- [x] Set the API title, description and version in the app factory.

### FAC-06 · CI pipeline

As a developer, I want every change checked automatically, so that broken code or a stale `swagger.json` does not reach the main branch.

**Done when**

- Each push runs `make lint`, `make test` and `make swagger-check`.
- Each merge to the main branch runs `make build` for both architectures.
- A failed step blocks the merge.

**Tasks**

- [x] Choose the CI service that belongs to the code host.
- [x] Add the pipeline with the four steps and a dependency cache.
- [x] Add the status badge to the root README.

### FAC-07 · Database and migrations

As a developer, I want the PostgreSQL schema under migration control, so that every environment has the same tables.

**Done when**

- `make up` brings up PostgreSQL, and the backend applies the migrations at start.
- The first evidence tables exist and are insert-only: the runtime role cannot update or delete, and a trigger rejects both.
- `GET /readyz` reports the state of the database.

**Tasks**

- [x] Add SQLAlchemy and Alembic with the first migration: `ledger_entry`, `evidence_payload` and `project`.
- [x] Create the runtime role, the owner role and the insert-only triggers.
- [x] Add the readiness route.
- [x] Test that an update or a delete on an evidence table fails.

### FAC-08 · Ledger entries and hash chain

As a member, I want every record chained to the one before it, so that nobody can change or remove a record without detection.

**Done when**

- An append writes the entry, the payload and the chain value in one transaction, with a number that has no gaps.
- Entry bytes are canonical JSON (RFC 8785), and the payload commitment uses a 32-byte random salt.
- A check of the whole chain passes, and it fails after any manual change to a row.
- The chain still verifies after the process is killed during appends and started again.

**Tasks**

- [x] Implement the canonical encoding, `entry_hash`, `payload_commitment` and `chain_hash`.
- [x] Implement `append()` with the advisory lock and the duplicate guard on `source_event`.
- [x] Implement `verify_chain()` and the chain proof for one entry.
- [x] Write property tests and the crash-and-replay test.

### FAC-09 · Connector interface and Thoth

As a developer, I want Telegram behind a connector interface, so that another source can be added later without a change to the core.

**Done when**

- The `Connector` protocol and `SourceEvent` exist as specified, with contract tests that run on recorded fixtures.
- Thoth polls with `getUpdates` and advances its offset only after the ledger commit.
- Thoth serves only the chat in `TELEGRAM_CAPTURE_CHAT_ID`, leaves other groups and answers a private message with one fixed line.
- While the chat ID is unset, Thoth records nothing and replies with the ID of the group it is in.

**Tasks**

- [x] Define the protocol, the event types and the capability flags.
- [x] Implement the Telegram connector with aiogram and long polling.
- [x] Record fixtures for a message, an edit, a duplicate delivery and a button tap.
- [x] Write the set-up part of `backend/README.md`: BotFather, privacy mode, the group and the chat ID.

### FAC-10 · Record messages

As a member, I want everything I send to the capture chat recorded at once, so that I do not lose a note.

**Done when**

- Each message that is not a command becomes a `record.created` entry within 2 seconds, with the raw Telegram update in its payload.
- `/log <text>` records the text after the command.
- An edit in Telegram appends `record.edited` when the text really changed, and the first version stays.
- A reply to a record is stored with a reference to it, and a forwarded message keeps its original sender and send time.
- A second delivery of the same update creates no second entry.

**Tasks**

- [x] Map `message` and `edited_message` updates to source events and entries.
- [x] Build the `record` and `record_version` projections.
- [x] Parse the stated event time (`@09:30`) and hashtags into the normalised view.
- [x] Test with the fixtures: new message, edit, reply, forward and duplicate.

### FAC-11 · Acknowledgement mark

As a member, I want a ✍ on every message that was recorded, so that a missing mark tells me that something went wrong.

**Done when**

- Thoth sets the reaction from `ACK_REACTION` only after the ledger commit.
- After downtime, Thoth records the backlog and marks those messages late.
- A rate-limit error delays the mark but does not lose it.
- The result of the real-group test is written down: do members get a notification for each mark, and does a busy chat hit rate limits?

**Tasks**

- [x] Implement `acknowledge()` in the connector with a retry queue.
- [x] Test the commit-then-mark order and the backlog case.
- [ ] Run the notification and rate-limit test in a real group and record the result in the README.

### FAC-12 · Projects

As a member, I want to create and list projects with short names, so that I can group records.

**Done when**

- `/project` lists the projects with their record counts.
- `/project new <name>` accepts 1 to 32 characters from `a–z`, `0–9`, `_` and `-`, stores capitals as lowercase, and rejects anything else with a clear message.
- `/project archive <name>` hides a project from the picker, and its records stay.
- Each change appends `project.created` or `project.archived`.

**Tasks**

- [x] Implement the three commands and the name check.
- [x] Build the `project` projection.
- [x] Test valid names, invalid names, duplicates and archive.

### FAC-13 · Filing with the project picker

As a member, I want to pick the project with one tap, so that filing a record needs no typing.

**Done when**

- After a record, Thoth replies with project buttons: most recently used first, six buttons, then More.
- A tap appends `record.filed` and turns the reply into a one-line confirmation with a Change project button.
- Records that one person sends within 10 seconds share one picker.
- `/log` without text asks for the project first, and everything that person sends is filed under it until Done or 10 minutes of silence.
- A reply to a record takes the project of that record.
- `/log pending` lists the records without a project, each with its buttons.

**Tasks**

- [x] Implement the picker with inline buttons and the `callback_query` handler.
- [x] Implement the `/log` session for each person.
- [x] Implement the shared picker, the reply rule and `/log pending`.
- [x] Test wrong taps, changed projects and expired sessions.

### FAC-14 · Search hits with /ask

As a member, I want `/ask` to show the matching records within 2 seconds, so that I find evidence even without AI.

**Done when**

- `/ask <question>` returns the best matches as verbatim quotes with entry number, project, time and integrity status.
- Search covers record text and captions with the `english` full-text configuration.
- If the question names a project, records from that project come first.
- With no match, Thoth says so and lists the words it searched for.

**Tasks**

- [x] Build the `search_chunk` projection with a `tsvector` column and a GIN index.
- [x] Implement the query builder and the ranking.
- [x] Implement the renderer that prints only stored text.
- [x] Measure the response time with 50,000 seeded records.

### FAC-15 · /verify, /status and /help

As a member, I want to check a record and the health of the system from the chat, so that I can trust what I see.

**Done when**

- `/verify` as a reply to a record, or with its number, shows its hash check and its filing history.
- `/status` shows the state of polling and the number of records without a project.
- `/help` lists all commands, and the same list is published with `setMyCommands`.

**Tasks**

- [x] Implement the fast verification for one entry.
- [x] Implement the three commands.
- [x] Publish the command menu at start-up.

**Gate 1 · Core loop works.** You send a note, see ✍, file it with one tap and find it with `/ask`; the chain verifies after a crash.

## Phase 1 · MVP

Seven stories make the record convincing to others: attachments, seals with trusted timestamps, evidence packs, Claude's verdict, the daily help and the HTTP API.

### FAC-16 · Attachments

As a member, I want photos, files and voice notes stored with the record, so that the evidence is complete.

**Done when**

- A file up to 20 MB is downloaded at once, hashed with SHA-256, encrypted with its own key and uploaded with a checksum.
- The bucket uses Object Lock with the retention from `S3_OBJECT_LOCK_DAYS`.
- An `attachment.stored` entry holds both hashes, the size, the type, the object key and the version.
- A larger file gets `attachment.unavailable`, and Thoth tells the sender at once.
- Photos sent as one album form one record.

**Tasks**

- [x] Implement the job queue on PostgreSQL with retries.
- [x] Implement download, hashing, encryption and upload as one stream.
- [x] Add the `blob` table and the attachment projection.
- [x] Handle albums and the 20 MB limit.
- [x] Test against an S3-compatible store, including a changed object.

### FAC-17 · Seals

As a member, I want the ledger sealed every 5 minutes, so that its state at that time is fixed.

**Done when**

- The sealer writes a seal when new entries exist: its number, the last entry, the chain head and the hash of the previous seal.
- Each seal is signed with the Ed25519 key from `SIGNING_KEY_FILE`.
- `/verify` shows the seal that covers a record, and `/status` shows the last seal and the key fingerprint.

**Tasks**

- [x] Implement the sealer loop and the `seal` table.
- [x] Implement key loading, signing and the key fingerprint.
- [x] Add a command that creates a new signing key.
- [x] Test the seal chain and the signature check.

### FAC-18 · Trusted timestamps

As an outsider, I want each seal stamped by independent time-stamping authorities, so that I do not have to trust the operator's clock.

**Done when**

- Each seal gets RFC 3161 tokens from the two TSAs in `TSA_URLS`, stored with their certificate chains.
- A TSA failure is retried, and Thoth reports it in the capture chat after 3 failed attempts.
- `openssl ts -verify` accepts a stored token.

**Tasks**

- [x] Implement the RFC 3161 request and the response check.
- [x] Store the tokens in `seal_anchor`.
- [x] Add retries and the alert, and test against two public TSAs.

### FAC-19 · Evidence packs and offline verifier

As an outsider, I want to verify a record without access to Factual, so that I can trust evidence that someone shows me.

**Done when**

- `/proof` as a reply, or with one or more entry numbers, posts a zip with entries, payloads, attachments, chain proofs, seals, tokens and keys.
- `verify.py` in the pack checks everything offline with Python's standard library and `openssl`.
- The pack discloses only the requested records; other entries appear as hashes.
- A person outside the project verifies a pack with its README alone.

**Tasks**

- [ ] Implement the pack builder and the export of chain proofs.
- [ ] Write `verify.py` and the plain-language README.
- [ ] Decrypt attachments into the pack and handle the 50 MB sending limit.
- [ ] Run the outsider test and fix what confused the tester.

### FAC-20 · Claude verdict for /ask

As a member, I want Claude to answer my question in one line with cited records, so that I see the conclusion and its evidence together.

**Done when**

- After the hits, Thoth edits its message into the final answer: Claude's verdict line, then the cited records printed from the ledger.
- Claude receives only retrieved snippets, uses read-only tools and returns a verdict that matches the schema.
- A verdict that cites an unknown entry, or no entry, becomes "No evidence found".
- If Claude is slow or unavailable, the hits stay as the answer.
- Each run appends `qa.answered`, and `ASK_DAILY_LIMIT` is enforced.

**Tasks**

- [ ] Add the Claude client with `ANTHROPIC_API_KEY` and `CLAUDE_MODEL`.
- [ ] Implement the tools `search_records`, `get_context`, `get_versions` and `get_attachment_text`.
- [ ] Implement the verdict schema, the citation check and the renderer.
- [ ] Write prompt-injection tests with hostile record text.
- [ ] Add the daily limit and the fallback.

### FAC-21 · Daily help message

As a member, I want the command list every morning, so that I remember what Thoth can do and see that it runs.

**Done when**

- At 06:00 in `Europe/Amsterdam`, Thoth posts the commands and a status line in the capture chat.
- Thoth deletes the message of the previous day.
- A restart does not send the message twice, and after an outage the message is still sent until 09:00.

**Tasks**

- [ ] Implement the scheduler with the `schedule_run` table.
- [ ] Implement the message with its status line.
- [ ] Test the daylight-saving changes and restarts.

### FAC-22 · Read-only HTTP API

As a developer of the future frontend, I want a documented read-only API, so that I can build on Factual without access to the database.

**Done when**

- The `/api/v1` routes for status, projects, records, search and proof work as specified.
- All `/api/v1` routes need the bearer token from `API_TOKEN` and are switched off when it is unset.
- `swagger.json` describes every route, and `make swagger-check` passes.

**Tasks**

- [ ] Implement the routes on the projections and the pack builder.
- [ ] Add the token check and tests for the 401 and 404 cases.
- [ ] Regenerate `swagger.json` and update `backend/README.md`.

**Gate 2 · Outsider check.** Someone who does not know the system verifies an evidence pack offline.

## Phase 2 · Depth

Five stories add depth: text from files, the nightly audit, the claim response skill, a tested restore and the optional anchors.

### FAC-23 · Text from images and PDFs

As a member, I want the text in photos and PDFs to be searchable, so that `/ask` also finds scanned letters and screenshots.

**Done when**

- For each stored image or PDF, Claude extracts the text into `derived_text`, with the model name and the time.
- Search includes derived text, and answers label it as derived from the named file.
- The file and its hash stay the evidence.

**Tasks**

- [ ] Add the extraction job after `attachment.stored`.
- [ ] Send the file to Claude and store the result.
- [ ] Add derived text to `search_chunk` and the label to the renderer.
- [ ] Set a size limit and a page limit for each file.

### FAC-24 · Nightly deep audit

As an operator, I want a full check every night, so that silent damage is found within a day.

**Done when**

- The audit checks the whole chain against every seal, checks every signature and token, and re-hashes attachments at a limited rate.
- Each run appends `audit.completed`, and a failure is posted in the capture chat with the exact entry, seal or object.
- `/status` shows the last result.

**Tasks**

- [ ] Implement the audit job and its schedule.
- [ ] Implement the throttled re-hash from object storage.
- [ ] Test with a changed row, a changed object and a bad token.

### FAC-25 · Claim response skill

As a member, I want a calm, evidence-only draft reply to a claim, so that I can answer a dispute with facts.

**Done when**

- `/respond` as a reply to a record, or with pasted text, posts a brief and a draft in the capture chat.
- Every factual sentence cites an entry, quotes are inserted by the system, and notes are labelled as notes.
- The checker rejects a draft with a missing citation, a sentence over 20 words or a banned phrase.
- The brief lists the records that support the claim and tells the member to check the source chat for gaps.

**Tasks**

- [ ] Create the skill folder: `SKILL.md`, the reference files and `check_draft.py`.
- [ ] Implement the tools `find_later_changes` and `verify_entry`.
- [ ] Implement the quote insertion and the checker gate.
- [ ] Build fixtures for the four cases of the change table and test them.

### FAC-26 · Backup and restore

As an operator, I want a tested restore, so that a lost server does not mean lost evidence.

**Done when**

- `backend/README.md` describes the database backup and the restore steps.
- After a restore, the deep audit passes against the stored seals before Thoth starts again.
- A restore drill on a fresh machine succeeds.

**Tasks**

- [ ] Write the backup and restore runbook.
- [ ] Add a start-up check that runs the audit after a restore.
- [ ] Run the drill and note how long it took.

### FAC-27 · Optional anchors

As a member who may need stronger proof, I want optional extra timestamps, so that I can switch them on when a dispute becomes serious.

**Done when**

- With `QUALIFIED_TSA_URL` set, one seal a day also gets a qualified timestamp.
- The deep audit re-timestamps old seals before a TSA certificate expires.
- The OpenTimestamps anchor is built behind a setting, or the decision not to build it is recorded.

**Tasks**

- [ ] Implement the daily qualified stamp.
- [ ] Implement re-timestamping.
- [ ] Try OpenTimestamps on one seal and decide.

**Gate 3 · Stable operation.** 30 nightly audits in a row pass, and a restore drill succeeds.

## Phase 3 · Reach

These items are not yet broken into stories; each one waits for a reason.

| Item | Why it waits |
| --- | --- |
| Frontend | The `frontend` folder is reserved. It will use the read-only API from FAC-22 |
| Slack connector | The connector interface from FAC-09 is ready for it; it needs its own fixtures |
| Files over 20 MB | Needs the local Bot API server next to the backend |
| Chat export import | Brings in older messages, labelled Imported |
| Voice transcription | Needs a speech-to-text engine; until then the audio file is the record |
| Vector search | Needs an embeddings service besides Claude; only worth it if full-text search proves too weak |
