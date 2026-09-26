# ABOUTME: Tests the end-to-end suite's Service harness with a stand-in docker first on
# ABOUTME: PATH: cleanup survives a failed `compose logs`; the config stays readable.
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.e2e import test_home_assistant
from tests.e2e.test_home_assistant import SERVICE, Service

# Stands in for docker: appends its arguments to $FAKE_DOCKER_CALLS, and fails
# `compose logs` as it does when it cannot reach the engine.
FAKE_DOCKER = """#!/bin/sh
echo "$*" >> "$FAKE_DOCKER_CALLS"
case " $* " in
  *" logs "*) echo "Cannot connect to the Docker daemon" >&2; exit 1 ;;
esac
"""


@pytest.fixture
def docker_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put the stand-in docker first on PATH; returns the file it logs calls to."""
    docker = tmp_path / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    calls = tmp_path / "calls"
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_CALLS", str(calls))
    return calls


def test_removing_the_service_survives_a_failed_logs_call(docker_calls: Path) -> None:
    # The failure still surfaces, after the removal.
    with pytest.raises(pytest.fail.Exception, match="compose logs"):
        Service().remove()

    last_call = docker_calls.read_text().splitlines()[-1]
    assert last_call.endswith(f"rm --stop --force {SERVICE}"), docker_calls.read_text()


def test_the_mounted_config_stays_readable_by_every_user_under_a_strict_umask(
    docker_calls: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On a Linux Docker host the container's user, UID 10001, cannot read a file
    # private to its owner. Colima lets it, so the end-to-end tests there pass
    # either way.
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(test_home_assistant, "RUN_DIR", run_dir)
    previous_umask = os.umask(0o077)
    try:
        Service().start({"sensors": {}}, "fake-key")
    finally:
        os.umask(previous_umask)

    mode = (run_dir / "config.yaml").stat().st_mode & 0o777
    assert mode == 0o644, oct(mode)
