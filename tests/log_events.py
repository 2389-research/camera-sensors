# ABOUTME: Reads the service's structured log events back out of pytest's caplog: each
# ABOUTME: JSON object the djev_sensors logger wrote, with or without its log level.
from __future__ import annotations

import json
from typing import Any

import pytest


def logged(caplog: pytest.LogCaptureFixture) -> list[tuple[int, dict[str, Any]]]:
    """Each event logged on the djev_sensors logger as (level, fields), in order."""
    return [
        (record.levelno, json.loads(record.getMessage()))
        for record in caplog.records
        if record.name == "djev_sensors"
    ]


def events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """The fields of each event logged on the djev_sensors logger, in order."""
    return [fields for _, fields in logged(caplog)]


def event_names(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The name of each event logged on the djev_sensors logger, in order."""
    return [fields["event"] for fields in events(caplog)]


def events_named(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    """The fields of each `name` event logged on the djev_sensors logger, in order."""
    return [fields for fields in events(caplog) if fields["event"] == name]
