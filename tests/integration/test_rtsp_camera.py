# ABOUTME: Integration tests for djev_sensors.camera over real RTSP: MediaMTX fed by an
# ABOUTME: FFmpeg publisher, and a refused connection that must leak no URL or password.
from __future__ import annotations

import errno
import logging
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import av
import pytest

from djev_sensors.camera import CameraStreamHub, FrameSample
from djev_sensors.config import CameraConfig
from tests.local_ports import closed_port
from tests.log_events import events

CAMERA_IDS = ("front_door", "porch")


def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        pytest.fail("ffmpeg is not on PATH; the RTSP integration test needs it")
    return path


class Publisher:
    """Loops a clip into MediaMTX over RTSP/TCP with FFmpeg, as MediaMTX documents."""

    def __init__(self, clip: Path, url: str, log_path: Path) -> None:
        self._command = [
            _ffmpeg(),
            "-nostdin",
            "-loglevel",
            "warning",
            "-re",
            "-stream_loop",
            "-1",
            "-i",
            str(clip),
            "-c",
            "copy",
            "-f",
            "rtsp",
            "-rtsp_transport",
            "tcp",
            url,
        ]
        self._log_path = log_path
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        assert self._process is None, "publisher already running"
        with self._log_path.open("ab") as log:
            self._process = subprocess.Popen(
                self._command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
            )

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        self._process = None


class StreamObserver:
    """Records hub callbacks from worker threads and lets the test wait on them."""

    def __init__(self, hub: CameraStreamHub) -> None:
        self._hub = hub
        self._changed = threading.Condition()
        self.connected: dict[str, bool] = {}
        self.transitions: dict[str, list[bool]] = {}
        # Samples received in each online period, per camera ID.
        self.samples_per_connection: dict[str, list[int]] = {}
        self.image_formats: set[tuple[tuple[int, ...], str]] = set()
        self.open_decoders_seen: list[int] = []

    def on_status(self, camera_id: str, connected: bool) -> None:
        with self._changed:
            self.open_decoders_seen.append(self._hub.open_decoder_count())
            self.connected[camera_id] = connected
            self.transitions.setdefault(camera_id, []).append(connected)
            if connected:
                self.samples_per_connection.setdefault(camera_id, []).append(0)
            self._changed.notify_all()

    def on_sample(self, sample: FrameSample) -> None:
        with self._changed:
            self.open_decoders_seen.append(self._hub.open_decoder_count())
            self.samples_per_connection[sample.camera_id][-1] += 1
            self.image_formats.add((sample.image.shape, sample.image.dtype.str))
            self._changed.notify_all()

    def all_sampled(self, connection: int, count: int) -> bool:
        """Whether every camera ID got `count` samples in online period `connection`."""
        return all(
            len(counts) >= connection and counts[connection - 1] >= count
            for counts in (self.samples_per_connection.get(c, []) for c in CAMERA_IDS)
        )

    def all_offline(self) -> bool:
        return all(self.connected.get(c) is False for c in CAMERA_IDS)

    def wait_until(self, condition: Callable[[], bool], what: str) -> None:
        timeout = 30.0
        with self._changed:
            if not self._changed.wait_for(condition, timeout):
                pytest.fail(
                    f"timed out after {timeout:.0f} s waiting for {what}; "
                    f"transitions={self.transitions}, "
                    f"samples per connection={self.samples_per_connection}"
                )


def camera(url: str, fps: float) -> CameraConfig:
    return CameraConfig.model_validate({"rtsp": url, "fps": fps})


@pytest.fixture
def test_clip(tmp_path: Path) -> Path:
    """A 4 s, 5 FPS H.264 test pattern with a keyframe every second."""
    clip = tmp_path / "djev-rtsp-test.mp4"
    result = subprocess.run(
        [
            _ffmpeg(),
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=5",
            "-t",
            "4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "5",
            str(clip),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return clip


@pytest.fixture
def publisher(
    test_clip: Path, rtsp_base_url: str, tmp_path: Path
) -> Iterator[Publisher]:
    running = Publisher(test_clip, f"{rtsp_base_url}/test", tmp_path / "publisher.log")
    try:
        yield running
    finally:
        running.stop()


def test_hub_samples_a_live_stream_and_recovers_it_on_one_decoder(
    publisher: Publisher, rtsp_base_url: str
) -> None:
    url = f"{rtsp_base_url}/test"
    hub = CameraStreamHub({"front_door": camera(url, 2), "porch": camera(url, 1)})
    observer = StreamObserver(hub)
    stop = threading.Event()
    runner = threading.Thread(
        target=hub.run, args=(observer.on_sample, observer.on_status, stop)
    )

    publisher.start()
    runner.start()
    try:
        observer.wait_until(lambda: observer.all_sampled(1, 2), "live samples")
        publisher.stop()
        observer.wait_until(observer.all_offline, "offline after the publisher stopped")
        publisher.start()
        observer.wait_until(
            lambda: observer.all_sampled(2, 2), "samples after recovery"
        )
        assert hub.open_decoder_count() == 1
    finally:
        stop.set()
        runner.join(timeout=30)

    assert not runner.is_alive()
    assert hub.open_decoder_count() == 0
    assert observer.transitions == {c: [True, False, True] for c in CAMERA_IDS}
    # Two camera IDs on one URL never had a second decoder, before or after recovery.
    assert max(observer.open_decoders_seen) == 1
    assert observer.image_formats == {((240, 320, 3), "|u1")}


@pytest.fixture
def ffmpeg_logs_on_stderr() -> Iterator[None]:
    """Route FFmpeg's own log messages to stderr, then restore PyAV's default."""
    av.logging.restore_default_callback()
    try:
        yield
    finally:
        av.logging.set_level(None)


def test_a_refused_connection_leaks_neither_url_nor_password(
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
    ffmpeg_logs_on_stderr: None,
) -> None:
    caplog.set_level(logging.DEBUG)
    password = "fake-camera-password-5b9e"
    port = closed_port()
    config = CameraConfig.model_validate(
        {"rtsp": f"rtsp://admin:${{CAMERA_PASSWORD}}@127.0.0.1:{port}/stream"},
        context={"env": {"CAMERA_PASSWORD": password}},
    )
    stop = threading.Event()
    attempts: list[int] = []

    def stop_after_first_failure(attempt: int) -> float:
        attempts.append(attempt)
        stop.set()
        return 0.0

    hub = CameraStreamHub({"garage": config}, reconnect_delay=stop_after_first_failure)

    hub.run(lambda sample: None, lambda camera_id, connected: None, stop)

    captured = capfd.readouterr()
    assert attempts == [1]
    assert events(caplog) == [
        {
            "event": "camera.reconnecting",
            "camera": "garage",
            "attempt": 1,
            "delay": 0.0,
            "error": "ConnectionRefusedError",
            "errno": errno.ECONNREFUSED,
        }
    ]
    for output in (caplog.text, captured.err, captured.out):
        assert password not in output
        assert f"127.0.0.1:{port}" not in output
