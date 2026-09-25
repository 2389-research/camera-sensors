# ABOUTME: Unit tests for djev_sensors.events: each structured event is one JSON object
# ABOUTME: logged on the djev_sensors logger, with the event name as its first key.
from __future__ import annotations

import json
import logging

import pytest

from djev_sensors.events import log_event


def test_log_event_writes_one_json_object_with_the_event_name_first(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="djev_sensors")

    log_event("camera.reconnecting", camera="garage", attempt=2, delay=2.0)

    [record] = caplog.records
    assert record.name == "djev_sensors"
    assert record.levelno == logging.INFO
    assert record.getMessage().startswith('{"event": "camera.reconnecting"')
    assert json.loads(record.getMessage()) == {
        "event": "camera.reconnecting",
        "camera": "garage",
        "attempt": 2,
        "delay": 2.0,
    }


def test_log_event_logs_at_the_requested_level(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="djev_sensors")

    log_event("camera.disconnected", level=logging.WARNING, camera="garage")

    [record] = caplog.records
    assert record.levelno == logging.WARNING
