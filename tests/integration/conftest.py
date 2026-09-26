# ABOUTME: Integration fixtures: run the Docker Compose test stack (MediaMTX RTSP on
# ABOUTME: 127.0.0.1:18554, Mosquitto MQTT on 127.0.0.1:18883) for the test session.
from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.local_ports import require_free, wait_until_serving

COMPOSE_FILE = Path(__file__).with_name("compose.yaml")
COMPOSE_PROJECT = "djev-sensors-test"
RTSP_PORT = 18554
MQTT_PORT = 18883


def _compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            COMPOSE_PROJECT,
            "--file",
            str(COMPOSE_FILE),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="session")
def compose_stack() -> Iterator[None]:
    """Run the test stack for the session and remove it, volumes included, after."""
    _compose("down", "--volumes", "--remove-orphans")  # left by an interrupted run
    for port in (RTSP_PORT, MQTT_PORT):
        require_free(port)
    try:
        up = _compose("up", "--detach")
        if up.returncode != 0:
            pytest.fail(f"docker compose up failed:\n{up.stderr}")
        for port in (RTSP_PORT, MQTT_PORT):
            wait_until_serving(port)
        yield
    finally:
        down = _compose("down", "--volumes", "--remove-orphans")
        if down.returncode != 0:
            pytest.fail(f"docker compose down failed:\n{down.stderr}")


@pytest.fixture(scope="session")
def rtsp_base_url(compose_stack: None) -> str:
    """Base URL of the stack's MediaMTX server; append a path such as /test."""
    return f"rtsp://127.0.0.1:{RTSP_PORT}"


@pytest.fixture(scope="session")
def mqtt_address(compose_stack: None) -> tuple[str, int]:
    """Host and port of the stack's Mosquitto broker, which allows anonymous clients."""
    return ("127.0.0.1", MQTT_PORT)
