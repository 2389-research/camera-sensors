# ABOUTME: Local TCP port helpers for tests: a port nothing listens on, a polling wait,
# ABOUTME: and the Compose stacks' checks that a port is free and that it serves.
from __future__ import annotations

import socket
import time
from collections.abc import Callable

import pytest


def closed_port() -> int:
    """A local TCP port with nothing listening on it."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def wait_until(
    condition: Callable[[], bool], what: str, timeout: float, *, interval: float = 0.05
) -> None:
    """Poll `condition` every `interval` seconds; fail if `timeout` passes first."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {timeout:.0f} s waiting for {what}")
        time.sleep(interval)


def require_free(port: int) -> None:
    """Fail if anything already accepts connections on 127.0.0.1:`port`.

    Probes with a connect, not a bind: right after a previous run's stack is
    gone, macOS can refuse a plain bind for a while though nothing listens.
    """
    with socket.socket() as probe:
        probe.settimeout(1.0)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            pytest.fail(f"127.0.0.1:{port} is in use; the test stack needs it free")


def is_serving(port: int) -> bool:
    """Whether a server accepts on `port` and keeps the connection open.

    Docker's port forwarding accepts connections on a published port before
    the service inside listens, then closes them at once, so a bare connect
    proves nothing.
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


def wait_until_serving(port: int, timeout: float = 60.0) -> None:
    wait_until(
        lambda: is_serving(port), f"127.0.0.1:{port} to serve", timeout, interval=0.2
    )
