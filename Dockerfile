# servicenow-mcp — streamable HTTP mode.
#
#   docker build -t servicenow-mcp .
#   docker run --rm --env-file .env -p 8765:8765 servicenow-mcp
#
# Credentials (SNOW_INSTANCE_URL / SNOW_USERNAME / SNOW_PASSWORD) are supplied at
# runtime via environment variables. .env is excluded by .dockerignore and is never
# copied into the image.

FROM python:3.12-slim

# uv, from the official distroless image (no pip needed).
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (cached unless the lockfile changes). README.md and src/
# are needed because pyproject.toml declares them for the local package build.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# Then the server itself.
COPY server.py ./

# Run as a non-root user.
RUN useradd --create-home --uid 10001 mcp && chown -R mcp:mcp /app
USER mcp

ENV SNOW_MCP_TRANSPORT=http
EXPOSE 8765

CMD ["/app/.venv/bin/python", "server.py"]
