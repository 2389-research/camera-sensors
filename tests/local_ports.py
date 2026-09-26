# ABOUTME: Local TCP port checks for the Docker Compose test stacks: a port must be free
# ABOUTME: before a stack starts, and it serves only once it holds a connection open.
from __future__ import annotations

import socket
import time

import pytest


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
    deadline = time.monotonic() + timeout
    while not is_serving(port):
        if time.monotonic() > deadline:
            pytest.fail(f"nothing served 127.0.0.1:{port} within {timeout:.0f} s")
        time.sleep(0.2)
