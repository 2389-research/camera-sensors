# ABOUTME: Integration tests for python -m djev_sensors: an invalid config exits 2 with
# ABOUTME: no traceback, and SIGINT or SIGTERM stops the service with exit status 0.
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = 30.0  # seconds to wait for the service to start or to exit


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
        "python -m djev_sensors: error: system: Field required"
    ]
    assert result.stdout == ""


@pytest.mark.parametrize(
    "signum", [signal.SIGINT, signal.SIGTERM], ids=lambda s: s.name
)
def test_a_stop_signal_shuts_the_service_down_and_exits_0(
    tmp_path: Path, signum: signal.Signals
) -> None:
    # Nothing listens for MQTT or RTSP, so no frame arrives and no model
    # request is made.
    port = closed_port()
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "system": {
                    "mqtt": {"host": "127.0.0.1", "port": port},
                    "model": {
                        "provider": "lunaroute",
                        "model": "djev",
                        "api_key_env": "LUNAROUTE_API_KEY",
                    },
                },
                "cameras": {"garage": {"rtsp": f"rtsp://127.0.0.1:{port}/stream"}},
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
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        service = subprocess.Popen(
            [sys.executable, "-m", "djev_sensors", "--config", str(config)],
            cwd=REPO_ROOT,
            env={**os.environ, "LUNAROUTE_API_KEY": "test-key"},
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
        )
    try:
        wait_until(
            lambda: (
                service.poll() is not None
                or '"service.started"' in stdout_path.read_text()
            ),
            "service.started, or the service's exit",
        )
        assert service.poll() is None, stderr_path.read_text()
        service.send_signal(signum)
        returncode = service.wait(timeout=TIMEOUT)
    finally:
        if service.poll() is None:
            service.kill()
        service.wait(timeout=TIMEOUT)

    assert returncode == 0, stderr_path.read_text()
    # Every stdout line is one JSON event.
    events = [json.loads(line) for line in stdout_path.read_text().splitlines()]
    assert {
        "event": "service.started",
        "cameras": ["garage"],
        "sensors": ["car_in_garage"],
    } in events
    assert events[-1] == {"event": "service.stopped"}
    assert other_stderr(stderr_path.read_text()) == []
