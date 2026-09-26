# ABOUTME: LunaRoute adapter for Djev Noul questions (spec sections 14-15): fits one
# ABOUTME: camera frame into the gateway's byte budget, then POSTs and parses the reply.
from __future__ import annotations

import asyncio
import base64
import json
import math
import time
from typing import Final

import cv2
import httpx
import numpy as np
from numpy.typing import NDArray
from pydantic import SecretStr

from djev_sensors.frames import as_uint8
from djev_sensors.models.base import BinaryJudgment, InvalidModelResponse, ModelError

GATEWAY_URL: Final = "https://gw.lunaroute.com/v1/systemone"
BODY_BYTE_BUDGET: Final = 45_000

_DJEV_MAX_SIDE_PIXELS: Final = 2048
_CANDIDATE_MAX_SIDES: Final[tuple[int, ...]] = (
    1600,
    1280,
    1024,
    896,
    768,
    640,
    512,
    448,
    384,
    320,
    256,
    192,
)
_JPEG_QUALITIES: Final[tuple[int, ...]] = (85, 75, 65, 55)
_ERROR_TEXT_LIMIT: Final = 200


class LunaRouteDjevClient:
    """Evaluates Djev Noul questions through LunaRoute (spec section 15).

    Fits the image to the gateway's byte budget on a worker thread (JPEG
    encoding is CPU-bound), then POSTs the exact fitted bytes over one shared
    `httpx.AsyncClient`. No retries: the next qualifying change retries
    naturally. Logs nothing itself; the scheduler logs inference events.
    """

    def __init__(
        self,
        api_key: SecretStr,
        model: str,
        timeout_seconds: float,
        *,
        url: str = GATEWAY_URL,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._url = url
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def evaluate_binary(
        self, image: NDArray[np.uint8], instructions: str
    ) -> BinaryJudgment:
        """Ask Djev a single yes/no question about `image` (spec section 14).

        `image` is never mutated; it may be shared with other readers.
        Raises ModelError for transport, auth, provider, capacity, or
        image-fit failures. Raises InvalidModelResponse for a successful
        response whose Noul content cannot be parsed or validated.
        """
        body, width, height = await asyncio.to_thread(
            _fit_request_body, image, instructions, self._model
        )
        headers = {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        started_at = time.monotonic()
        try:
            response = await self._client.post(self._url, content=body, headers=headers)
        except httpx.RequestError as exc:
            raise ModelError(f"LunaRoute request failed: {type(exc).__name__}") from exc
        latency_ms = round((time.monotonic() - started_at) * 1000)

        if not response.is_success:
            raise ModelError(
                f"LunaRoute returned HTTP {response.status_code}: "
                f"{_error_excerpt(response)}"
            )
        return _parse_noul_response(response, width, height, latency_ms)

    async def aclose(self) -> None:
        """Close the underlying HTTP client. Safe to call once at shutdown."""
        await self._client.aclose()


def _error_excerpt(response: httpx.Response) -> str:
    """At most `_ERROR_TEXT_LIMIT` characters of the gateway's error text."""
    return response.text[:_ERROR_TEXT_LIMIT]


def _parse_noul_response(
    response: httpx.Response, width: int, height: int, latency_ms: int
) -> BinaryJudgment:
    try:
        payload = response.json()
    except ValueError as exc:
        raise InvalidModelResponse(f"response body was not valid JSON: {exc}") from exc

    return BinaryJudgment(
        true_probability=_extract_noul(payload),
        sent_width=width,
        sent_height=height,
        latency_ms=latency_ms,
    )


def _extract_noul(payload: object) -> float:
    """The Noul probability at answers.result.noul, or raise InvalidModelResponse."""
    if not isinstance(payload, dict):
        raise InvalidModelResponse("response body was not a JSON object")
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise InvalidModelResponse("response has no 'answers' object")
    result = answers.get("result")
    if not isinstance(result, dict):
        raise InvalidModelResponse("response has no answers.result object")
    if result.get("type") != "noul":
        raise InvalidModelResponse(
            f"answers.result.type was not 'noul': {result.get('type')!r}"
        )
    value = result.get("noul")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidModelResponse(f"answers.result.noul was not numeric: {value!r}")
    # Only a float can be infinite or NaN. math.isfinite raises OverflowError
    # for an int too big for a float; the range check below compares it exactly.
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidModelResponse("answers.result.noul was not finite")
    if not 0.0 <= value <= 1.0:
        raise InvalidModelResponse(f"answers.result.noul out of [0, 1]: {value!r}")
    return float(value)


def _fit_request_body(
    image: NDArray[np.uint8],
    instructions: str,
    model: str,
    budget: int = BODY_BYTE_BUDGET,
) -> tuple[bytes, int, int]:
    """The largest Djev request body (spec section 14) that fits under `budget` bytes.

    Tries the source dimensions when their longest side is within Djev's
    2048-pixel limit, then each smaller candidate max side, at decreasing
    JPEG quality, returning the first whole serialized body at or under
    `budget` plus its width and height. Never mutates `image`, which may be
    shared with other readers. Runs CPU-bound JPEG encoding (up to 52
    candidates): callers with an event loop should run this in a thread.

    Raises ModelError if no candidate fits, before any HTTP request is made.
    """
    source_height, source_width = image.shape[:2]
    first_side = min(max(source_width, source_height), _DJEV_MAX_SIDE_PIXELS)
    max_sides = [first_side] + [
        side for side in _CANDIDATE_MAX_SIDES if side < first_side
    ]

    for max_side in max_sides:
        width, height = _fit_dimensions(source_width, source_height, max_side)
        resized = (
            image
            if (width, height) == (source_width, source_height)
            else _resize(image, width, height)
        )
        for quality in _JPEG_QUALITIES:
            ok, encoded = cv2.imencode(
                ".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, quality]
            )
            if not ok:
                continue
            body = _build_body(encoded, instructions, model)
            if len(body) <= budget:
                return body, width, height

    raise ModelError(f"no image candidate fit the {budget}-byte request budget")


def _fit_dimensions(
    source_width: int, source_height: int, max_side: int
) -> tuple[int, int]:
    """Aspect-preserving size with longest side at most `max_side`, minimum 1 pixel."""
    source_longest = max(source_width, source_height)
    if source_longest <= max_side:
        return source_width, source_height
    scale = max_side / source_longest
    width = max(1, round(source_width * scale))
    height = max(1, round(source_height * scale))
    return width, height


def _resize(image: NDArray[np.uint8], width: int, height: int) -> NDArray[np.uint8]:
    return as_uint8(cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA))


def _build_body(encoded: NDArray[np.uint8], instructions: str, model: str) -> bytes:
    """Serialize the Djev/LunaRoute request body (spec section 14) as compact UTF-8."""
    image_b64 = base64.b64encode(encoded.tobytes()).decode("ascii")
    payload = {
        "model": model,
        "state": "",
        "questions": {
            "result": {
                "type": "noul",
                "instructions": {
                    "text": instructions,
                    "image": f"data:image/jpeg;base64,{image_b64}",
                },
            }
        },
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
