# ABOUTME: Per-sensor runtime state and the symmetric uncertainty band (spec sections 17
# ABOUTME: and 28) that turns one Djev probability into ON, OFF, or the prior state.
from __future__ import annotations

from dataclasses import dataclass

from djev_sensors.config import SensorConfig


def resolve_state(
    previous: bool | None, probability: float, threshold: float
) -> bool | None:
    """Map Djev's yes-probability to a binary state (spec section 17).

    At or above `threshold` is ON, at or below `1 - threshold` is OFF, and
    anything between keeps `previous`, so an uncertain first inference leaves
    the state unknown (None).
    """
    if probability >= threshold:
        return True
    # Written as a sum because 1.0 - 0.8 is 0.19999999999999996 in floating point,
    # which would leave a probability of exactly 0.2 inside the band. When the two
    # decimals add up to one, their floating-point sum never rounds above 1.0.
    if probability + threshold <= 1.0:
        return False
    return previous


@dataclass
class SensorRuntime:
    """In-memory state for one sensor (spec section 28), owned by the scheduler."""

    sensor_id: str
    config: SensorConfig
    state: bool | None = None
    camera_available: bool = False
    model_available: bool = True
    inference_in_flight: bool = False
    cooldown_started_at: float | None = None  # monotonic seconds

    @property
    def available(self) -> bool:
        """What Home Assistant sees (spec section 21): both conditions must hold."""
        return self.camera_available and self.model_available

    def cooldown_expired(self, now: float) -> bool:
        """True once `cooldown_seconds` have passed since the last request began."""
        if self.cooldown_started_at is None:
            return True
        return now - self.cooldown_started_at >= self.config.cooldown_seconds
