# Factual frontend

This folder is **empty for now**. It is reserved for the Factual frontend, which belongs to roadmap phase 3 (Reach) and waits until the backend's read-only HTTP API is in place.

## Planned role

A web interface for the people in the capture chat: browse projects and records, search the ledger, check a record's integrity and download evidence packs. Everything it shows comes from the backend's read-only API (`/api/v1`, documented in `backend/swagger.json`); it never reads the database directly.

## Rules

- It follows `VERSION.txt` at the repository root. Backend and frontend always carry the same version; the frontend never has a version of its own.
- It uses the backend API only, with the bearer token from `API_TOKEN`.
- Until it exists, the Telegram chat is the only interface, as described in `SPECIFICATION.md`.
