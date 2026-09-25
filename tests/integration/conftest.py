# ABOUTME: Integration fixtures: run the Docker Compose test stack (MediaMTX RTSP on
# ABOUTME: 127.0.0.1:18554, Mosquitto MQTT on 127.0.0.1:18883) for the test session.
from __future__ import annotations

import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

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


def _require_free(port: int) -> None:
    """Fail if anything already accepts connections on 127.0.0.1:`port`.

    Probes with a connect, not a bind: right after a previous run's stack is
    gone, macOS can refuse a plain bind for a while though nothing listens.
    """
    with socket.socket() as probe:
        probe.settimeout(1.0)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            pytest.fail(f"127.0.0.1:{port} is in use; the test stack needs it free")


def _is_serving(port: int) -> bool:
    """Whether a server accepts on `port` and keeps the connection open.

    Docker Desktop accepts connections on a published port before the service
    inside listens, then closes them at once, so a bare connect proves nothing.
    """
    try:
        conn = socket.create_connection(("127.0.0.1", port), timeout=1.0)
    except OSError:
        return False
    with conn:
        conn.settimeout(0.5)
        try:
            return conn.recv(1) != b""  # empty: closed at once, nothing listens
        except TimeoutError:
            return True  # held open: the server waits for the client to speak
        except OSError:
            return False


def _wait_until_serving(port: int, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while not _is_serving(port):
        if time.monotonic() > deadline:
            pytest.fail(f"nothing served 127.0.0.1:{port} within {timeout:.0f} s")
        time.sleep(0.2)


@pytest.fixture(scope="session")
def compose_stack() -> Iterator[None]:
    """Run the test stack for the session and remove it, volumes included, after."""
    _compose("down", "--volumes", "--remove-orphans")  # left by an interrupted run
    for port in (RTSP_PORT, MQTT_PORT):
        _require_free(port)
    try:
        up = _compose("up", "--detach")
        if up.returncode != 0:
            pytest.fail(f"docker compose up failed:\n{up.stderr}")
        for port in (RTSP_PORT, MQTT_PORT):
            _wait_until_serving(port)
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
