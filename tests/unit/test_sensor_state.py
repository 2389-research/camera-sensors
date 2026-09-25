# ABOUTME: Unit tests for djev_sensors.state.resolve_state, the uncertainty band that
# ABOUTME: turns one Djev probability into ON, OFF, or the sensor's prior state.
from __future__ import annotations

import pytest

from djev_sensors.state import resolve_state


@pytest.mark.parametrize("previous", [None, False, True])
def test_a_confident_yes_turns_the_sensor_on(previous: bool | None) -> None:
    assert resolve_state(previous, 0.91, 0.8) is True


@pytest.mark.parametrize("previous", [None, False, True])
def test_a_confident_no_turns_the_sensor_off(previous: bool | None) -> None:
    assert resolve_state(previous, 0.09, 0.8) is False


@pytest.mark.parametrize(
    ("previous", "probability"),
    [(False, 0.55), (True, 0.55), (False, 0.79), (True, 0.21)],
)
def test_a_probability_inside_the_band_keeps_the_prior_state(
    previous: bool, probability: float
) -> None:
    assert resolve_state(previous, probability, 0.8) is previous


def test_an_uncertain_first_inference_leaves_the_state_unknown() -> None:
    assert resolve_state(None, 0.55, 0.8) is None


@pytest.mark.parametrize(
    ("previous", "probability", "expected"),
    [(False, 0.8, True), (True, 0.2, False)],
    ids=["equal-to-threshold", "equal-to-one-minus-threshold"],
)
def test_equality_at_either_band_edge_transitions(
    previous: bool, probability: float, expected: bool
) -> None:
    assert resolve_state(previous, probability, 0.8) is expected


# In binary floating point 1.0 - 0.66 is just under 0.34, and 1.0 - 0.34 is just
# under 0.66, so these pairs catch a lower edge written as either subtraction.
@pytest.mark.parametrize(("threshold", "probability"), [(0.66, 0.34), (0.93, 0.07)])
def test_exactly_one_minus_the_threshold_turns_the_sensor_off(
    threshold: float, probability: float
) -> None:
    assert resolve_state(True, probability, threshold) is False
