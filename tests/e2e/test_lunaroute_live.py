# ABOUTME: Live contract probe for LunaRouteDjevClient against the real gateway. Runs
# ABOUTME: only under `scripts/check --live`; fails outright when no key is configured.
from __future__ import annotations

import asyncio
import os

import numpy as np
import pytest
from numpy.typing import NDArray
from pydantic import SecretStr

from djev_sensors.models.lunaroute_djev import LunaRouteDjevClient


def _white_frame() -> NDArray[np.uint8]:
    """A small, synthetic, nonprivate frame: safe to send to a live model."""
    return np.full((64, 64, 3), 255, dtype=np.uint8)


def test_live_gateway_returns_a_numeric_probability() -> None:
    api_key = os.environ.get("LUNAROUTE_API_KEY")
    if not api_key:
        pytest.fail("LUNAROUTE_API_KEY is not set; the live probe needs a rotated key")

    async def _call() -> None:
        client = LunaRouteDjevClient(
            api_key=SecretStr(api_key), model="djev", timeout_seconds=15.0
        )
        try:
            judgment = await client.evaluate_binary(
                _white_frame(), "Is this image mostly one solid light color?"
            )
        finally:
            await client.aclose()
        assert 0.0 <= judgment.true_probability <= 1.0
        assert judgment.sent_width > 0
        assert judgment.sent_height > 0

    asyncio.run(_call())
