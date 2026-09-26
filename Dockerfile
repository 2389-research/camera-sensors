# ABOUTME: The service's container image: Python 3.12 with dependencies synced by uv
# ABOUTME: from uv.lock, running python -m djev_sensors on the config mounted in /app.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.25 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
# A cache mount keeps uv's downloads between builds. It is a separate filesystem,
# so uv copies packages from it rather than hardlinking them.
ENV UV_LINK_MODE=copy
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev
# The service needs no privileges, so it runs as an unprivileged system user.
RUN useradd --system --uid 10001 djev
COPY djev_sensors ./djev_sensors
# COPY keeps the build context's file modes, and a checkout made under a strict
# umask leaves them readable by their owner alone, so open them to the service user.
RUN chmod -R a+rX pyproject.toml uv.lock djev_sensors
USER djev
# The virtualenv's python runs the service; uv is needed only to build the image.
ENV PATH="/app/.venv/bin:$PATH"
CMD ["python", "-m", "djev_sensors", "--config", "/app/config.yaml"]
