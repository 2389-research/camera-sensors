# ABOUTME: The service's container image: Python 3.12 with dependencies synced by uv
# ABOUTME: from uv.lock, running python -m djev_sensors on the config mounted in /app.

# The build stage syncs the virtualenv with uv. The runtime stage below copies
# only the virtualenv and the sources, so uv never reaches the final image.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.25 /uv /bin/
# The virtualenv's python links to this image's interpreter, which the runtime
# stage has at the same path, so uv must never download an interpreter of its own.
ENV UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock ./
# A cache mount keeps uv's downloads between builds. It is a separate filesystem,
# so uv copies packages from it rather than hardlinking them.
ENV UV_LINK_MODE=copy
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev

# The same base image as the build stage, so the virtualenv's interpreter link
# resolves here too.
FROM python:3.12-slim
WORKDIR /app
# The service needs no privileges, so it runs as an unprivileged system user.
RUN useradd --system --uid 10001 djev
COPY --from=build /app/.venv /app/.venv
COPY djev_sensors ./djev_sensors
# COPY keeps the build context's file modes, and a checkout made under a strict
# umask leaves them readable by their owner alone, so open them to the service user.
RUN chmod -R a+rX djev_sensors
USER djev
# The virtualenv's python runs the service.
ENV PATH="/app/.venv/bin:$PATH"
CMD ["python", "-m", "djev_sensors", "--config", "/app/config.yaml"]
