# Factual

Factual turns what you send to one Telegram chat into tamper-evident evidence and finds it again when you ask. Its bot, **Thoth**, records every message and attachment in an append-only, hash-chained ledger, seals the ledger every 5 minutes with an Ed25519 signature and RFC 3161 trusted timestamps, and files each record under a project that you pick with one tap.

| What you do | What Factual does |
| --- | --- |
| Send a note, photo, file, voice note or forwarded message to the capture chat | Thoth records it within 2 seconds and marks it with ✍ |
| Tap a project under the record | The record is filed under `family`, `client-acme`, … with one tap |
| `/ask did Greg say the kickoff is on 5 February?` | Thoth shows the matching records as verbatim quotes, then Claude adds a one-line verdict |
| `/proof` as a reply to a record | Thoth posts an evidence pack that anyone can verify offline |
| `/respond <claim>` | Thoth drafts a calm, evidence-only reply for you to edit and send |

`SPECIFICATION.md` is the source of truth for the design. `PROJECT_PLAN.md` lists the user stories that implement it, in order of work.

## Repository structure

```text
factual/
├── VERSION.txt          the version, one line — the only place where the version is written
├── README.md            this file
├── SPECIFICATION.md     the technical specification (source of truth)
├── PROJECT_PLAN.md      the user stories, in order of work
├── backend/             the FastAPI service: Thoth, the ledger, the sealer, the workers and the HTTP API
│   └── README.md        set-up, configuration, Makefile targets, API, logging, tests, backup and restore
└── frontend/            reserved for the frontend; empty for now
    └── README.md
```

Factual ships as one container: a FastAPI process that also runs the Telegram connector, the job workers, the sealer and the scheduler. All state lives in PostgreSQL and S3-compatible object storage, so the container can be replaced at any time.

## Five-minute start

1. Create the bot with @BotFather, name it Thoth and disable privacy mode. Create a private Telegram group, add the people who may record and recall, and add Thoth. `backend/README.md` has the step-by-step set-up.
2. `cd backend && cp .env.example .env`, then fill in the variables.
3. `make up` starts the backend, PostgreSQL and an S3-compatible store with Docker Compose. `make down` stops them. `make help` lists every target.
4. Send a message to the group. Thoth records it, marks it with ✍ and asks for its project.

## Where the version lives

`VERSION.txt` at the repository root holds one line with the version, for example `0.1.0`. It is the only place where the version is written. Backend and frontend share this version: the backend shows it at `GET /version`, in `info.version` of `swagger.json`, in every log line and in the image tag.

The version follows [Semantic Versioning](https://semver.org) (`MAJOR.MINOR.PATCH`):

- **MAJOR** for breaking changes to the API or the ledger format.
- **MINOR** for new features.
- **PATCH** for fixes.

A release is a commit that changes `VERSION.txt`, tagged `v<version>`. The ledger format has its own number, the `"v"` field of each entry, so old entries stay readable after any release.
