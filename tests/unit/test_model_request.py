# ABOUTME: Unit tests for djev_sensors.models.lunaroute_djev's pure request-fitting
# ABOUTME: function: byte budget, dimension/quality search, and the wire JSON shape.
from __future__ import annotations

import base64
import json

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray

from djev_sensors.models.base import ModelError
from djev_sensors.models.lunaroute_djev import BODY_BYTE_BUDGET, _fit_request_body

INSTRUCTIONS = (
    "Evaluate only the visible contents of the supplied camera frame.\n\n"
    "Is the garage door open?"
)


def _compressible_frame() -> NDArray[np.uint8]:
    """A 640x480 BGR frame of four flat blocks: highly JPEG-compressible."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    frame[:240, :320] = (40, 40, 40)
    frame[:240, 320:] = (200, 200, 200)
    frame[240:, :320] = (40, 200, 40)
    frame[240:, 320:] = (200, 40, 40)
    return frame


def _noisy_frame() -> NDArray[np.uint8]:
    """A 640x480 BGR frame of uniform random noise: compresses poorly."""
    rng = np.random.default_rng(seed=1234)
    return rng.integers(0, 256, size=(480, 640, 3), dtype=np.uint8).astype(np.uint8)


def _decode_image_field(payload: dict[str, object]) -> NDArray[np.uint8]:
    """Base64-decode the question's image data URL and return its pixels."""
    questions = payload["questions"]
    result = questions["result"]  # type: ignore[index]
    instructions = result["instructions"]  # type: ignore[index]
    image_field = instructions["image"]  # type: ignore[index]
    prefix = "data:image/jpeg;base64,"
    assert image_field.startswith(prefix)
    raw = base64.b64decode(image_field[len(prefix) :])
    assert raw[:2] == b"\xff\xd8"  # JPEG magic bytes
    decoded = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    return np.asarray(decoded, dtype=np.uint8)


def test_request_shape_has_exactly_one_noul_question_and_no_top_level_images() -> None:
    body, width, height = _fit_request_body(_compressible_frame(), INSTRUCTIONS, "djev")

    payload = json.loads(body)
    assert payload["model"] == "djev"
    assert payload["state"] == ""
    assert list(payload["questions"].keys()) == ["result"]
    assert payload["questions"]["result"]["type"] == "noul"
    assert payload["questions"]["result"]["instructions"]["text"] == INSTRUCTIONS
    assert "images" not in payload
    assert (width, height) == (640, 480)


def test_compressible_frame_keeps_source_resolution() -> None:
    body, width, height = _fit_request_body(_compressible_frame(), INSTRUCTIONS, "djev")

    assert (width, height) == (640, 480)
    decoded = _decode_image_field(json.loads(body))
    assert decoded.shape[:2] == (480, 640)
    assert len(body) <= BODY_BYTE_BUDGET


def test_noisy_frame_shrinks_below_source_resolution() -> None:
    body, width, height = _fit_request_body(_noisy_frame(), INSTRUCTIONS, "djev")

    assert width < 640
    assert height < 480
    decoded = _decode_image_field(json.loads(body))
    assert decoded.shape[:2] == (height, width)
    assert len(body) <= BODY_BYTE_BUDGET


def test_every_fitted_body_is_at_most_the_byte_budget() -> None:
    for frame in (_compressible_frame(), _noisy_frame()):
        body, _width, _height = _fit_request_body(frame, INSTRUCTIONS, "djev")
        assert len(body) <= BODY_BYTE_BUDGET


def test_no_candidate_fits_the_budget_raises_model_error_before_http() -> None:
    with pytest.raises(ModelError):
        _fit_request_body(_compressible_frame(), INSTRUCTIONS, "djev", budget=100)


def test_fitting_never_mutates_the_input_frame() -> None:
    frame = _noisy_frame()
    snapshot = frame.copy()

    _fit_request_body(frame, INSTRUCTIONS, "djev")

    np.testing.assert_array_equal(frame, snapshot)
