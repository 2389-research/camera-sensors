# ABOUTME: End-to-end check that the service image works when built from a checkout
# ABOUTME: made under a strict umask, where every source file is private to its owner.
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE = "djev-sensors:strict-umask-test"
BUILD_CONTEXT = (
    "Dockerfile",
    ".dockerignore",
    "pyproject.toml",
    "uv.lock",
    "djev_sensors",
)
BUILD_TIMEOUT = 900.0  # the private modes defeat the layer cache, so uv sync reruns
RUN_TIMEOUT = 120.0
# Imports the whole service as the image's own user, and fails if that is root.
IMPORT_AS_SERVICE_USER = "import os, djev_sensors.app; assert os.getuid() != 0"


def _copy_with_private_modes(destination: Path) -> None:
    """Copy the build context the way a checkout under umask 077 leaves it."""
    for name in BUILD_CONTEXT:
        source = REPO_ROOT / name
        target = destination / name
        if source.is_dir():
            shutil.copytree(
                source, target, ignore=shutil.ignore_patterns("__pycache__")
            )
        else:
            shutil.copy2(source, target)
    for path in [destination, *destination.rglob("*")]:
        path.chmod(0o700 if path.is_dir() else 0o600)


def test_the_image_runs_from_a_strict_umask_checkout(tmp_path: Path) -> None:
    _copy_with_private_modes(tmp_path)
    try:
        subprocess.run(
            ["docker", "build", "--quiet", "--tag", IMAGE, str(tmp_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT,
        )
        result = subprocess.run(
            ["docker", "run", "--rm", IMAGE, "python", "-c", IMPORT_AS_SERVICE_USER],
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT,
        )
    finally:
        subprocess.run(
            ["docker", "image", "rm", "--force", IMAGE],
            capture_output=True,
            check=False,
        )
    assert result.returncode == 0, result.stderr
