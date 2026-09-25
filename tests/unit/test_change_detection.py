# ABOUTME: Unit tests for djev_sensors.detectors.frame_difference: the frame-difference
# ABOUTME: detector's prepare/compare pipeline and pixel-threshold change scoring.
from __future__ import annotations

import numpy as np
import pytest

from djev_sensors.detectors.base import ChangeResult
from djev_sensors.detectors.frame_difference import FrameDifferenceDetector


def _detector(
    *, width: int = 320, pixel_delta_threshold: int = 20, blur: int = 0
) -> FrameDifferenceDetector:
    return FrameDifferenceDetector(
        width=width, pixel_delta_threshold=pixel_delta_threshold, blur=blur
    )


def test_identical_frames_score_zero() -> None:
    detector = _detector()
    previous = np.full((10, 10), 100, dtype=np.uint8)
    current = previous.copy()

    result = detector.compare(previous, current)

    assert isinstance(result, ChangeResult)
    assert result.changed_pct == 0.0


def test_pixels_at_threshold_count_as_changed() -> None:
    detector = _detector(pixel_delta_threshold=20)
    previous = np.full((10, 10), 100, dtype=np.uint8)
    current = previous.copy()
    current[0, 0:4] = 120  # delta of exactly 20 (the threshold), four pixels

    result = detector.compare(previous, current)

    assert result.changed_pct == pytest.approx(4.0)


def test_pixels_one_below_threshold_do_not_count() -> None:
    detector = _detector(pixel_delta_threshold=20)
    previous = np.full((10, 10), 100, dtype=np.uint8)
    current = previous.copy()
    current[0, 0:4] = 119  # delta of 19, one below the threshold

    result = detector.compare(previous, current)

    assert result.changed_pct == 0.0


def test_compare_scores_only_the_two_frames_it_is_given() -> None:
    """The detector holds no baseline: an earlier call must not affect the next one."""
    detector = _detector(pixel_delta_threshold=20)
    frame_1 = np.zeros((10, 10), dtype=np.uint8)
    frame_2 = np.full((10, 10), 100, dtype=np.uint8)
    frame_3 = frame_2.copy()
    frame_3[0, 0:4] = 130  # delta of 30 against frame_2 only, four pixels
    snapshots = [frame.copy() for frame in (frame_1, frame_2, frame_3)]

    detector.compare(frame_1, frame_2)  # an unrelated earlier call
    result = detector.compare(frame_2, frame_3)

    assert result.changed_pct == pytest.approx(4.0)
    for frame, snapshot in zip((frame_1, frame_2, frame_3), snapshots, strict=True):
        np.testing.assert_array_equal(frame, snapshot)


def test_compare_rejects_mismatched_shapes() -> None:
    detector = _detector()
    previous = np.zeros((10, 10), dtype=np.uint8)
    current = np.zeros((5, 5), dtype=np.uint8)

    with pytest.raises(ValueError, match="shape"):
        detector.compare(previous, current)


def test_prepare_resizes_to_configured_width_preserving_aspect_ratio() -> None:
    detector = _detector(width=320)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)

    prepared = detector.prepare(frame)

    assert prepared.shape[:2] == (240, 320)


def test_prepare_clamps_height_to_at_least_one() -> None:
    detector = _detector(width=320)
    frame = np.zeros((1, 1000, 3), dtype=np.uint8)

    prepared = detector.prepare(frame)

    assert prepared.shape[:2] == (1, 320)


def test_prepare_does_not_blur_when_blur_is_zero() -> None:
    detector = _detector(width=10, blur=0)
    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    frame[5, 5] = (255, 255, 255)

    prepared = detector.prepare(frame)

    assert np.count_nonzero(prepared) == 1


def test_prepare_blurs_when_blur_is_positive() -> None:
    detector = _detector(width=10, blur=3)
    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    frame[5, 5] = (255, 255, 255)

    prepared = detector.prepare(frame)

    assert np.count_nonzero(prepared) > 1
