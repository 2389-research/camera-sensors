# ABOUTME: The service's container image: Python 3.12 with dependencies synced by uv
# ABOUTME: from uv.lock, running python -m djev_sensors on the config mounted in /app.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.25 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY djev_sensors ./djev_sensors
CMD ["uv", "run", "--no-sync", "python", "-m", "djev_sensors", "--config", "/app/config.yaml"]
