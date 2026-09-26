# ABOUTME: Tests the end-to-end suite's container cleanup with a stand-in docker first
# ABOUTME: on PATH: a failed `docker compose logs` must not skip removing the container.
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.e2e.test_home_assistant import SERVICE, Service

# Stands in for docker: appends its arguments to $FAKE_DOCKER_CALLS, and fails
# `compose logs` as it does when it cannot reach the engine.
FAKE_DOCKER = """#!/bin/sh
echo "$*" >> "$FAKE_DOCKER_CALLS"
case " $* " in
  *" logs "*) echo "Cannot connect to the Docker daemon" >&2; exit 1 ;;
esac
"""


def test_removing_the_service_survives_a_failed_logs_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = tmp_path / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    calls = tmp_path / "calls"
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_CALLS", str(calls))

    # The failure still surfaces, after the removal.
    with pytest.raises(pytest.fail.Exception, match="compose logs"):
        Service().remove()

    last_call = calls.read_text().splitlines()[-1]
    assert last_call.endswith(f"rm --stop --force {SERVICE}"), calls.read_text()
