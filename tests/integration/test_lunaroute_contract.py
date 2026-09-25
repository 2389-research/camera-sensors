# ABOUTME: Wire-boundary tests for LunaRouteDjevClient against a real local HTTP
# ABOUTME: server: request shape, headers, byte budget, and both response outcomes.
from __future__ import annotations

import asyncio
import base64
import json
import socket
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


def test_malformed_2xx_raises_invalid_model_response(
    contract_server: _ContractServer,
) -> None:
    contract_server.set_response(200, b'{"answers": {"result": {"type": "choice"}}}')

    with pytest.raises(InvalidModelResponse):
        _evaluate(contract_server.url, INSTRUCTIONS)


def test_non_2xx_status_raises_model_error(contract_server: _ContractServer) -> None:
    contract_server.set_response(503, b"service unavailable", content_type="text/plain")

    with pytest.raises(ModelError):
        _evaluate(contract_server.url, INSTRUCTIONS)


def test_closed_port_raises_model_error() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    with pytest.raises(ModelError):
        _evaluate(f"http://127.0.0.1:{port}/v1/systemone", INSTRUCTIONS)


def test_no_fit_frame_raises_model_error_with_no_request_received(
    contract_server: _ContractServer,
) -> None:
    huge_instructions = "x" * 100_000

    with pytest.raises(ModelError):
        _evaluate(contract_server.url, huge_instructions)

    assert contract_server.requests == []
