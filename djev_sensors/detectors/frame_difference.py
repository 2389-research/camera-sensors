# ABOUTME: Frame-difference change detector: grayscale, resize, optional blur, then
# ABOUTME: absolute pixel difference against a caller-supplied previous frame.
from __future__ import annotations

import cv2
import numpy as np
from cv2.typing import MatLike
from numpy.typing import NDArray

from djev_sensors.detectors.base import ChangeResult


def _as_uint8(mat: MatLike) -> NDArray[np.uint8]:
    """Narrow an OpenCV result to the uint8 array type our pipeline guarantees."""
    return np.asarray(mat, dtype=np.uint8)


class FrameDifferenceDetector:
    """Camera-level change detector (spec section 8): grayscale + resize + diff.

    Holds no baseline of its own. Spec section 8 requires the comparison
    baseline to be the immediately previous *sampled* frame regardless of
    whether it triggered a sensor; the caller (the app's per-camera loop)
    tracks that frame and passes it explicitly on every `compare` call.
    """

    def __init__(self, width: int, pixel_delta_threshold: int, blur: int) -> None:
        self._width = width
        self._pixel_delta_threshold = pixel_delta_threshold
        self._blur = blur

    def prepare(self, frame: NDArray[np.uint8]) -> NDArray[np.uint8]:
        """Reduce a decoded BGR frame to a small grayscale detection frame."""
        gray = _as_uint8(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        source_height, source_width = gray.shape[:2]
        height = max(1, round(source_height * self._width / source_width))
        resized = _as_uint8(
            cv2.resize(gray, (self._width, height), interpolation=cv2.INTER_AREA)
        )
        if self._blur <= 0:
            return resized
        return _as_uint8(cv2.GaussianBlur(resized, (self._blur, self._blur), 0))

    def compare(
        self, previous: NDArray[np.uint8], current: NDArray[np.uint8]
    ) -> ChangeResult:
        """Score `current` against `previous`; neither input is modified.

        Raises ValueError if the two prepared frames have different shapes.
        """
        if previous.shape != current.shape:
            raise ValueError(
                f"prepared frame shape mismatch: {previous.shape} vs {current.shape}"
            )
        delta = cv2.absdiff(previous, current)
        changed_pct = (
            float(np.count_nonzero(delta >= self._pixel_delta_threshold))
            * 100.0
            / delta.size
        )
        return ChangeResult(changed_pct=changed_pct)
