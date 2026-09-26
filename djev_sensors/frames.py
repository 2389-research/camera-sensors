# ABOUTME: Frame array helpers shared by the camera hub, the change detector, and the
# ABOUTME: model client: they narrow PyAV and OpenCV output to uint8 arrays.
from __future__ import annotations

import numpy as np
from numpy.typing import ArrayLike, NDArray


def as_uint8(array: ArrayLike) -> NDArray[np.uint8]:
    """Narrow a PyAV or OpenCV result to the uint8 array our pipeline guarantees."""
    return np.asarray(array, dtype=np.uint8)
