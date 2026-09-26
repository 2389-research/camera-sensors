# ABOUTME: Wire-boundary tests for LunaRouteDjevClient against a real local HTTP
# ABOUTME: server: request shape, headers, byte budget, and both response outcomes.
from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import NamedTuple

import numpy as np
import pytest
from numpy.typing import NDArray
from pydantic import SecretStr

from djev_sensors.models.base import BinaryJudgment, InvalidModelResponse, ModelError
from djev_sensors.models.lunaroute_djev import LunaRouteDjevClient
from tests.local_ports import closed_port

FAKE_KEY = "lr_" + "a" * 32
INSTRUCTIONS = "Is the door open?"


class _RecordedRequest(NamedTuple):
    """One request the local server received, with headers keyed lowercase."""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class _RecordingHandler(BaseHTTPRequestHandler):
    """Records each POST and replies with whatever the test configured."""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append(  # type: ignore[attr-defined]
            _RecordedRequest(
                method=self.command,
                path=self.path,
                headers={k.lower(): v for k, v in self.headers.items()},
                body=body,
            )
        )
        response_body = self.server.response_body  # type: ignore[attr-defined]
        self.send_response(self.server.response_status)  # type: ignore[attr-defined]
        self.send_header(
            "Content-Type",
            self.server.response_content_type,  # type: ignore[attr-defined]
        )
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)

    def log_message(self, format: str, *args: object) -> None:
        pass  # keep test output quiet


class _ContractServer:
    """A loopback HTTP server that records requests and returns canned replies."""

    def __init__(self, server: ThreadingHTTPServer, url: str) -> None:
        self._server = server
        self.url = url

    @property
    def requests(self) -> list[_RecordedRequest]:
        return self._server.requests  # type: ignore[attr-defined]

    def set_response(
        self, status: int, body: bytes, content_type: str = "application/json"
    ) -> None:
        self._server.response_status = status  # type: ignore[attr-defined]
        self._server.response_body = body  # type: ignore[attr-defined]
        self._server.response_content_type = content_type  # type: ignore[attr-defined]


@pytest.fixture
def contract_server() -> Iterator[_ContractServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
    server.requests = []  # type: ignore[attr-defined]
    server.response_status = 200  # type: ignore[attr-defined]
    server.response_body = b"{}"  # type: ignore[attr-defined]
    server.response_content_type = "application/json"  # type: ignore[attr-defined]
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _ContractServer(server, f"http://127.0.0.1:{port}/v1/systemone")
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _noul_body(value: str) -> bytes:
    """A response body whose answers.result is a noul answer of `value`, raw JSON."""
    return b'{"answers": {"result": {"type": "noul", "noul": ' + value.encode() + b"}}}"


def _small_frame() -> NDArray[np.uint8]:
    return np.zeros((48, 64, 3), dtype=np.uint8)


def _evaluate(url: str, instructions: str) -> BinaryJudgment:
    """Run one evaluate_binary call to completion, closing the client afterward."""

    async def _call() -> BinaryJudgment:
        client = LunaRouteDjevClient(
            api_key=SecretStr(FAKE_KEY), model="djev", timeout_seconds=5.0, url=url
        )
        try:
            return await client.evaluate_binary(_small_frame(), instructions)
        finally:
            await client.aclose()

    return asyncio.run(_call())


def test_valid_noul_response_returns_binary_judgment(
    contract_server: _ContractServer,
) -> None:
    contract_server.set_response(
        200, b'{"answers": {"result": {"type": "noul", "noul": 0.83}}}'
    )

    judgment = _evaluate(contract_server.url, INSTRUCTIONS)

    assert judgment.true_probability == pytest.approx(0.83)
    assert judgment.sent_width > 0
    assert judgment.sent_height > 0
    assert judgment.latency_ms >= 0

    [request] = contract_server.requests
    assert request.method == "POST"
    assert request.path == "/v1/systemone"
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert request.headers["content-type"] == "application/json"
    content_length = int(request.headers["content-length"])
    assert content_length == len(request.body)
    assert content_length <= 45_000

    payload = json.loads(request.body)
    assert list(payload["questions"].keys()) == ["result"]
    assert "images" not in payload
    image_field = payload["questions"]["result"]["instructions"]["image"]
    prefix = "data:image/jpeg;base64,"
    assert image_field.startswith(prefix)
    assert base64.b64decode(image_field[len(prefix) :])[:2] == b"\xff\xd8"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"<html>Bad gateway</html>", id="not JSON"),
        pytest.param(b"[0.9]", id="not an object"),
        pytest.param(b"{}", id="no answers"),
        pytest.param(b'{"answers": [0.9]}', id="answers not an object"),
        pytest.param(b'{"answers": {"result": 0.9}}', id="result not an object"),
        pytest.param(
            b'{"answers": {"result": {"type": "choice"}}}', id="not a noul answer"
        ),
        pytest.param(b'{"answers": {"result": {"type": "noul"}}}', id="noul missing"),
        pytest.param(_noul_body('"0.9"'), id="noul a string"),
        pytest.param(_noul_body("true"), id="noul a boolean"),
        pytest.param(_noul_body("1.5"), id="noul above 1"),
        pytest.param(_noul_body("-0.1"), id="noul below 0"),
        pytest.param(_noul_body("1" + "0" * 400), id="noul an integer past float"),
        # Python's json module reads these non-standard constants.
        pytest.param(_noul_body("NaN"), id="noul NaN"),
        pytest.param(_noul_body("Infinity"), id="noul Infinity"),
        pytest.param(_noul_body("-Infinity"), id="noul -Infinity"),
    ],
)
def test_a_2xx_without_a_valid_noul_raises_invalid_model_response(
    contract_server: _ContractServer, body: bytes
) -> None:
    # The scheduler turns InvalidModelResponse into OFF (spec section 18); any
    # other exception would make the sensor unavailable instead.
    contract_server.set_response(200, body)

    with pytest.raises(InvalidModelResponse):
        _evaluate(contract_server.url, INSTRUCTIONS)


def test_no_log_record_carries_the_api_key(
    contract_server: _ContractServer, caplog: pytest.LogCaptureFixture
) -> None:
    # This holds only while httpx and httpcore keep header values out of their
    # log lines, so it watches every logger at DEBUG through a whole request.
    caplog.set_level(logging.DEBUG)
    for name in ("httpx", "httpcore"):
        caplog.set_level(logging.DEBUG, logger=name)
    contract_server.set_response(200, _noul_body("0.83"))

    _evaluate(contract_server.url, INSTRUCTIONS)

    transport = [r for r in caplog.records if r.name.startswith(("httpx", "httpcore"))]
    assert transport, "httpx and httpcore logged nothing, so this proved nothing"
    assert FAKE_KEY not in caplog.text


def test_non_2xx_status_raises_model_error(contract_server: _ContractServer) -> None:
    contract_server.set_response(503, b"service unavailable", content_type="text/plain")

    with pytest.raises(ModelError):
        _evaluate(contract_server.url, INSTRUCTIONS)


def test_closed_port_raises_model_error() -> None:
    with pytest.raises(ModelError):
        _evaluate(f"http://127.0.0.1:{closed_port()}/v1/systemone", INSTRUCTIONS)


def test_no_fit_frame_raises_model_error_with_no_request_received(
    contract_server: _ContractServer,
) -> None:
    huge_instructions = "x" * 100_000

    with pytest.raises(ModelError):
        _evaluate(contract_server.url, huge_instructions)

    assert contract_server.requests == []
