# syntax=docker/dockerfile:1
# One image, one process: Uvicorn serves the FastAPI app, whose lifespan starts the
# Telegram connector, the job workers, the sealer and the scheduler.
# Build: docker buildx build --platform linux/amd64,linux/arm64 --build-arg VERSION=$(cat ../VERSION.txt) -t factual-backend:$(cat ../VERSION.txt) .
FROM python:3.12-slim

# The version comes from ../VERSION.txt through the build argument; the file itself
# lives outside the build context. It is the image tag and every log line's service.version.
ARG VERSION
RUN test -n "${VERSION}" || { echo "the VERSION build argument is required" >&2; exit 1; }
ENV APP_VERSION=${VERSION} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# openssl checks RFC 3161 tokens, the same way an outsider checks an evidence pack
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 factual \
 && useradd --uid 10001 --gid factual --home-dir /app --no-create-home --shell /usr/sbin/nologin factual

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

USER factual
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]
CMD ["python", "-m", "app"]
