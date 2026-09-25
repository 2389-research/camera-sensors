# ABOUTME: Structured log events (spec section 33): each event is one JSON object logged
# ABOUTME: on the djev_sensors logger. Callers pass IDs and numbers, never URLs.
from __future__ import annotations

import json
import logging

_logger = logging.getLogger("djev_sensors")


def log_event(event: str, *, level: int = logging.INFO, **fields: object) -> None:
    """Log `event` and its fields as one JSON object string, event name first.

    Fields must be JSON-serializable; anything else raises TypeError rather than
    being stringified, so an exception or URL object never slips into a log line.
    """
    _logger.log(level, json.dumps({"event": event, **fields}))
