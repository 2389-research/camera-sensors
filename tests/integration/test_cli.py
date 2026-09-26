# ABOUTME: Integration tests for python -m djev_sensors: config errors, startup order,
# ABOUTME: and exit status 0 after SIGINT or SIGTERM but 1 for a stop no signal caused.
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = 30.0  # seconds to wait for the service to start or to exit
SERVICE_ENV = {**os.environ, "LUNAROUTE_API_KEY": "test-key"}

# Runs the command line with a stand-in for Application whose run() sets
# stop_event, as the camera hub does on a callback bug, then returns normally,
# as the real run() does when the hub's re-raise misses the shutdown bound.
# With SIGTERM_AFTER_STOP set, a SIGTERM arrives after that stop.
UNSIGNALLED_STOP = """
import asyncio
import os
import signal
import sys

import djev_sensors.__main__ as cli


class HubStopsTheService:
    def __init__(self, config, camera_hub, detector, scheduler, publisher, model):
        self._publisher = publisher
        self._model = model

    async def run(self, stop_event):
        stop_event.set()
        if os.environ.get("SIGTERM_AFTER_STOP"):
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(0.2)  # the loop runs the signal handler meanwhile
        self._publisher.stop()
        await self._model.aclose()


cli.Application = HubStopsTheService
sys.exit(cli.main(sys.argv[1:]))
"""


def other_stderr(text: str) -> list[str]:
    """Stderr lines other than macOS's notices about duplicate Objective-C classes.

    PyAV and OpenCV each bundle FFmpeg's libavdevice, so importing both makes
    the Objective-C runtime print "objc[<pid>]: Class ... is implemented in
    both ..." on macOS.
    """
    return [line for line in text.splitlines() if not line.startswith("objc[")]


def closed_port() -> int:
    """A local TCP port with nothing listening on it."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def wait_until(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + TIMEOUT
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {TIMEOUT:.0f} s waiting for {what}")
        time.sleep(0.05)


def write_config(tmp_path: Path, mqtt: dict[str, object], rtsp_port: int) -> Path:
    """One camera at 127.0.0.1:`rtsp_port` and one sensor on it."""
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "system": {
                    "mqtt": mqtt,
                    "model": {
                        "provider": "lunaroute",
                        "model": "djev",
                        "api_key_env": "LUNAROUTE_API_KEY",
                    },
                },
                "cameras": {"garage": {"rtsp": f"rtsp://127.0.0.1:{rtsp_port}/stream"}},
                "sensors": {
                    "car_in_garage": {
                        "name": "Car in Garage",
                        "camera": "garage",
                        "prompt": "Is a car parked in the garage?",
                    }
                },
            }
        )
    )
    return config


@dataclass
class ServiceRun:
    returncode: int
    events: list[dict[str, Any]]
    stderr: list[str]  # without macOS's Objective-C notices


def run_until(
    config: Path, tmp_path: Path, event: str, signum: signal.Signals
) -> ServiceRun:
    """Run the service until it logs `event`, then send `signum` and await its exit."""
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        service = subprocess.Popen(
            [sys.executable, "-m", "djev_sensors", "--config", str(config)],
            cwd=REPO_ROOT,
            env=SERVICE_ENV,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
        )
    try:
        wait_until(
            lambda: (
                service.poll() is not None or f'"{event}"' in stdout_path.read_text()
            ),
            f"{event}, or the service's exit",
        )
        assert service.poll() is None, stderr_path.read_text()
        service.send_signal(signum)
        returncode = service.wait(timeout=TIMEOUT)
    finally:
        if service.poll() is None:
            service.kill()
        service.wait(timeout=TIMEOUT)
    # Every stdout line is one JSON event.
    events = [json.loads(line) for line in stdout_path.read_text().splitlines()]
    return ServiceRun(returncode, events, other_stderr(stderr_path.read_text()))


def test_an_invalid_config_exits_2_with_the_error_on_stderr_and_no_traceback(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("cameras: {}\nsensors: {}\n")

    result = subprocess.run(
        [sys.executable, "-m", "djev_sensors", "--config", str(config)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )

    assert result.returncode == 2
    # The message and nothing else: no traceback.
    assert other_stderr(result.stderr) == [
        "python -m djev_sensors: error: system: Field required; "
        "sensors: Value error, at least one sensor is required"
    ]
    assert result.stdout == ""


@pytest.mark.parametrize(
    "signum", [signal.SIGINT, signal.SIGTERM], ids=lambda s: s.name
)
def test_a_stop_signal_shuts_the_service_down_and_exits_0(
    tmp_path: Path, signum: signal.Signals
) -> None:
    # Nothing listens for MQTT, so the service is still waiting for its first
    # connect when the signal arrives, and no frame can reach the model.
    port = closed_port()
    config = write_config(tmp_path, {"host": "127.0.0.1", "port": port}, port)

    run = run_until(config, tmp_path, "service.started", signum)

    assert run.returncode == 0, run.stderr
    assert {
        "event": "service.started",
        "cameras": ["garage"],
        "sensors": ["car_in_garage"],
    } in run.events
    assert run.events[-1] == {"event": "service.stopped"}
    assert run.stderr == []


def test_the_cameras_start_only_after_mqtt_connects_and_publishes_discovery(
    mqtt_address: tuple[str, int], tmp_path: Path
) -> None:
    host, port = mqtt_address
    run_id = uuid.uuid4().hex[:12]  # the session's broker keeps retained topics
    mqtt = {
        "host": host,
        "port": port,
        "client_id": f"djev-cli-{run_id}",
        "discovery_prefix": f"ha-cli-{run_id}",
        "topic_prefix": f"djev-cli-{run_id}",
    }
    # The camera never answers, so no frame can reach the model.
    config = write_config(tmp_path, mqtt, closed_port())

    run = run_until(config, tmp_path, "camera.reconnecting", signal.SIGTERM)

    assert run.returncode == 0, run.stderr
    names = [event["event"] for event in run.events]
    assert "service.started" in names
    assert names.index("mqtt.discovery_published") < names.index("camera.reconnecting")
    assert names[-1] == "service.stopped"
    assert run.stderr == []


@pytest.mark.parametrize(
    "sigterm_after_stop", [False, True], ids=["no signal", "SIGTERM after the stop"]
)
def test_a_stop_that_no_signal_caused_exits_1(
    tmp_path: Path, sigterm_after_stop: bool
) -> None:
    port = closed_port()
    config = write_config(tmp_path, {"host": "127.0.0.1", "port": port}, port)
    env = dict(SERVICE_ENV)
    if sigterm_after_stop:
        env["SIGTERM_AFTER_STOP"] = "1"

    result = subprocess.run(
        [sys.executable, "-c", UNSIGNALLED_STOP, "--config", str(config)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )

    assert result.returncode == 1
    assert other_stderr(result.stderr) == [
        "python -m djev_sensors: error: the service stopped without SIGINT or SIGTERM"
    ]
