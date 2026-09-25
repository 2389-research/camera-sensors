# ABOUTME: Model client contract: the BinaryJudgment result, the ModelClient protocol,
# ABOUTME: and the exceptions the scheduler treats as distinct inference outcomes.
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class BinaryJudgment:
    """One Djev Noul judgment (spec section 13): probability of yes, plus what was sent.

    `sent_width` and `sent_height` are the dimensions of the image actually
    transmitted, which may be smaller than the source frame (spec section 14).
    `latency_ms` is the HTTP round trip only; fitting time is excluded.
    """

    true_probability: float
    sent_width: int
    sent_height: int
    latency_ms: int


class ModelError(Exception):
    """Transport, auth, provider, capacity, or image-fit failure (spec section 19).

    The scheduler treats this as the sensor becoming unavailable, retaining
    its previous binary state internally. Never carries the request body,
    the image, or the Authorization header.
    """


class InvalidModelResponse(Exception):
    """A successful response whose Noul content did not parse (spec section 18).

    The scheduler treats this as the sensor going OFF. Deliberately does not
    subclass ModelError: an `except ModelError` must never swallow this.
    """


class ModelClient(Protocol):
    """Evaluates one binary sensor condition against one camera frame."""

    async def evaluate_binary(
        self, image: NDArray[np.uint8], instructions: str
    ) -> BinaryJudgment:
        """Ask the model a single yes/no question about `image`.

        `image` is BGR at the camera's source resolution and may be shared
        with other readers; implementations must not mutate it. Raises
        ModelError for transport, auth, provider, capacity, or image-fit
        failures. Raises InvalidModelResponse for a successful response
        whose Noul content cannot be parsed or validated.
        """
        ...

    async def aclose(self) -> None:
        """Release held transport resources. The app calls this at shutdown."""
        ...
