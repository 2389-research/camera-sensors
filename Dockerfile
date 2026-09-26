# ABOUTME: The service's container image: Python 3.12 with dependencies synced by uv
# ABOUTME: from uv.lock, running python -m djev_sensors on the config mounted in /app.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.25 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
# The service needs no privileges, so it runs as an unprivileged system user.
# uv run needs a writable cache, which it keeps under the user's home directory.
RUN useradd --system --uid 10001 --create-home djev
USER djev
COPY djev_sensors ./djev_sensors
CMD ["uv", "run", "--no-sync", "python", "-m", "djev_sensors", "--config", "/app/config.yaml"]
