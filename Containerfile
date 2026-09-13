# DocsForge MCP server — the hosted process.
#
# One container, one process: `main.py --http` serving the landing page at `/`,
# a health check at `/health`, and the tool surface at `/mcp` behind a bearer
# token. The web chat (docsforge/server/app.py) is deliberately NOT started
# here; it is a local testing surface and nothing routes to it.
#
#   podman build -t docsforge -f Containerfile .
#   podman run -p 8765:8765 -e DOCSFORGE_MCP_TOKEN=... -e DOCSFORGE_DB=... docsforge
#
# Required at runtime:
#   DOCSFORGE_MCP_TOKEN   clients send it as `Authorization: Bearer <token>`;
#                         without it the server refuses to bind 0.0.0.0.
#   DOCSFORGE_DB or DATABASE_URL
#                         the Postgres DSN. The runtime is stateless — a file
#                         store would vanish on every redeploy. Aiven Apps
#                         injects DATABASE_URL when a Postgres service is
#                         connected, and DocsForge reads it as a fallback.
# Optional:
#   PORT                  what the platform routes to (default 8765)
#   DOCSFORGE_MAX_CHARS, DOCSFORGE_HARVEST_DEADLINE, …  as documented in README

FROM python:3.12-slim

# No Playwright/Chromium: `js=true` harvests are not available in this image,
# and would not fit a 1 GB plan anyway.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8765

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY pyproject.toml README.md LICENSE ./
COPY docsforge ./docsforge
COPY main.py ./
RUN pip install ".[postgres]"

# Caches the app writes at runtime (harvest status, resolution cache, logs)
# live under a non-root home and are ephemeral by design.
RUN useradd --create-home --uid 10001 docsforge \
    && mkdir -p /app/logs && chown -R docsforge:docsforge /app
USER docsforge
ENV HOME=/home/docsforge

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8765\")}/health',timeout=4)" || exit 1

CMD ["sh", "-c", "python main.py --http --host 0.0.0.0 --port ${PORT}"]
