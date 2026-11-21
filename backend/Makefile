# Daily commands for the Factual backend. `make` alone prints the help.
# Recipe lines start with a tab character.
VERSION   := $(shell cat ../VERSION.txt)
IMAGE     ?= factual-backend
PLATFORMS := linux/amd64,linux/arm64
PYTHON    := $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
BUILD_ARGS ?=
export VERSION

.DEFAULT_GOAL := help
.PHONY: help build up down run swagger swagger-check test lint

help: ## Show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-14s %s\n", $$1, $$2}'

build: ## Build the image for linux/amd64 and linux/arm64, tagged with the version
	docker buildx build --platform $(PLATFORMS) --build-arg VERSION=$(VERSION) -t $(IMAGE):$(VERSION) $(BUILD_ARGS) .

up: ## Start backend, PostgreSQL and S3 storage with Docker Compose
	docker compose up -d --build

down: ## Stop the Compose stack
	docker compose down

run: ## Run the backend on this machine with auto-reload
	APP_VERSION=$(VERSION) $(PYTHON) -m app --reload --port 8000

swagger: ## Regenerate swagger.json from the FastAPI app
	$(PYTHON) scripts/export_openapi.py > swagger.json

swagger-check: ## Fail if swagger.json is out of date
	$(PYTHON) scripts/export_openapi.py | diff - swagger.json

test: ## Run the tests with pytest
	$(PYTHON) -m pytest

lint: ## Check the code style with Ruff
	$(PYTHON) -m ruff check . && $(PYTHON) -m ruff format --check .
