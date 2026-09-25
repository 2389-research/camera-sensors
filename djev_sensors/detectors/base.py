# ABOUTME: Change-detector interface: the ChangeDetector protocol and its result type.
# ABOUTME: Detectors are stateless; callers supply both frames on every comparison.
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class ChangeResult:
    """The percentage of a prepared frame's pixels that changed enough to count."""

    changed_pct: float


class ChangeDetector(Protocol):
    """Interface for a pluggable camera-level change detector (spec section 9)."""

    def prepare(self, frame: NDArray[np.uint8]) -> NDArray[np.uint8]:
        """Reduce a decoded BGR frame to this detector's comparison representation."""
        ...

    def compare(
        self, previous: NDArray[np.uint8], current: NDArray[np.uint8]
    ) -> ChangeResult:
        """Score how much `current` differs from `previous`.

        Neither input is modified. Implementations hold no baseline of their
        own: the caller supplies both frames on every call. Raises ValueError
        if the two frames have different shapes.
        """
        ...
