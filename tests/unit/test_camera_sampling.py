# ABOUTME: Unit tests for djev_sensors.camera: per-camera sample clocks on a shared
# ABOUTME: decoder, status transitions, reconnect backoff, and redacted stream logs.
from __future__ import annotations

import errno
import json
import logging
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Final

import numpy as np
import pytest
from numpy.typing import NDArray

from djev_sensors.camera import CameraStreamHub, FrameSample, reconnect_delay
from djev_sensors.config import CameraConfig

URL = "rtsp://camera.test/stream"
OTHER_URL = "rtsp://other-camera.test/stream"

# Script steps besides a frame time (float) or an exception for the stream to raise.
STOP: Final = "stop"  # set the stop event, then end the stream
WAIT_FOR_STOP: Final = "wait_for_stop"  # block until stop is set, then yield a frame
UNCONVERTIBLE: Final = "unconvertible"  # yield a frame whose BGR conversion fails

Step = float | Exception | str
# One connection: its steps, or the exception that opening the stream raises.
Connection = Sequence[Step] | Exception


class StreamError(Exception):
    """A stream failure raised by a scripted connection."""


class ScriptExhausted(BaseException):
    """The hub opened a URL more times than its script allows.

    Derives from BaseException so the hub cannot mistake it for a stream failure.
    """


class ScriptedFrame:
    """A decoded frame that records its time in `conversions` when converted."""

    def __init__(
        self, time: float, image: NDArray[np.uint8], conversions: list[float]
    ) -> None:
        self._time = time
        self._image = image
        self._conversions = conversions

    def to_bgr(self) -> NDArray[np.uint8]:
        self._conversions.append(self._time)
        return self._image


class UnconvertibleFrame:
    """A decoded frame whose conversion to BGR fails."""

    def to_bgr(self) -> NDArray[np.uint8]:
        raise StreamError("frame would not convert")


class ScriptedSource:
    """Frame-source factory that plays scripted connections for each URL.

    Each call opens the URL's next scripted connection. A float step sets the
    fake clock to that time, then yields a new frame; converting that frame
    returns the array recorded in `frames` and logs its time in `conversions`.
    """

    def __init__(
        self,
        scripts: Mapping[str, Sequence[Connection]],
        stop_event: threading.Event,
    ) -> None:
        self.now = 0.0
        self.opened: list[str] = []
        self.closed: list[str] = []
        self.frames: dict[float, NDArray[np.uint8]] = {}
        self.conversions: list[float] = []
        self._connections = {url: iter(script) for url, script in scripts.items()}
        self._stop_event = stop_event

    def clock(self) -> float:
        return self.now

    def __call__(self, url: str) -> Iterator[ScriptedFrame | UnconvertibleFrame]:
        self.opened.append(url)
        connection = next(self._connections[url], None)
        if connection is None:
            raise ScriptExhausted(url)
        if isinstance(connection, Exception):
            raise connection
        return self._play(url, connection)

    def _play(
        self, url: str, steps: Sequence[Step]
    ) -> Iterator[ScriptedFrame | UnconvertibleFrame]:
        try:
            for step in steps:
                if step == STOP:
                    self._stop_event.set()
                    return
                if step == WAIT_FOR_STOP:
                    self._stop_event.wait()
                    blank = np.zeros((2, 2, 3), dtype=np.uint8)
                    yield ScriptedFrame(self.now, blank, self.conversions)
                elif step == UNCONVERTIBLE:
                    yield UnconvertibleFrame()
                elif isinstance(step, Exception):
                    raise step
                else:
                    assert isinstance(step, float)
                    self.now = step
                    image = np.zeros((2, 2, 3), dtype=np.uint8)
                    self.frames[step] = image
                    yield ScriptedFrame(step, image, self.conversions)
        finally:
            self.closed.append(url)


class Recorder:
    """Collects the hub's callbacks in the order they arrive."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, float | bool]] = []
        self.samples: list[FrameSample] = []

    def on_sample(self, sample: FrameSample) -> None:
        self.samples.append(sample)
        self.events.append(("sample", sample.camera_id, sample.captured_at))

    def on_status(self, camera_id: str, connected: bool) -> None:
        self.events.append(("status", camera_id, connected))

    def sample_times(self, camera_id: str) -> list[float]:
        return [s.captured_at for s in self.samples if s.camera_id == camera_id]

    def statuses(self) -> list[tuple[str, float | bool]]:
        return [
            (camera_id, value)
            for kind, camera_id, value in self.events
            if kind == "status"
        ]


def camera(
    url: str = URL, fps: float = 1, env: Mapping[str, str] | None = None
) -> CameraConfig:
    return CameraConfig.model_validate(
        {"rtsp": url, "fps": fps}, context={"env": env or {}}
    )


def no_delay(attempt: int) -> float:
    return 0.0


def ignore_sample(sample: FrameSample) -> None:
    pass


def ignore_status(camera_id: str, connected: bool) -> None:
    pass


def run_hub(
    hub: CameraStreamHub,
    on_sample: Callable[[FrameSample], None],
    on_status: Callable[[str, bool], None],
    stop_event: threading.Event,
) -> None:
    """Run the hub on this thread; fail if the test watchdog had to stop it."""
    expired = threading.Event()

    def expire() -> None:
        expired.set()
        stop_event.set()

    watchdog = threading.Timer(5.0, expire)
    watchdog.start()
    try:
        hub.run(on_sample, on_status, stop_event)
    finally:
        watchdog.cancel()
        watchdog.join()
        # Checked even when run() raised, so a callback failure that never
        # stopped the hub cannot pass for one that did.
        assert not expired.is_set(), "the hub ran until the test watchdog stopped it"


def djev_events(
    caplog: pytest.LogCaptureFixture,
) -> list[tuple[int, dict[str, object]]]:
    return [
        (record.levelno, json.loads(record.getMessage()))
        for record in caplog.records
        if record.name == "djev_sensors"
    ]


def test_cameras_sharing_a_url_sample_at_their_own_rates_from_one_decoder() -> None:
    stop = threading.Event()
    two_seconds_at_10_fps = [index / 10 for index in range(20)]
    source = ScriptedSource({URL: [[*two_seconds_at_10_fps, STOP]]}, stop)
    hub = CameraStreamHub(
        {"slow": camera(fps=1), "fast": camera(fps=2)},
        frame_source=source,
        clock=source.clock,
        reconnect_delay=no_delay,
    )
    recorder = Recorder()
    open_decoders: list[int] = []
    source_positions: list[float] = []

    def on_sample(sample: FrameSample) -> None:
        open_decoders.append(hub.open_decoder_count())
        source_positions.append(source.now)
        recorder.on_sample(sample)

    run_hub(hub, on_sample, recorder.on_status, stop)

    assert recorder.sample_times("slow") == [0.0, 1.0]
    assert recorder.sample_times("fast") == [0.0, 0.5, 1.0, 1.5]
    assert source.opened == [URL]
    assert open_decoders == [1] * 6
    # Each sample carries its decoded frame, and the hub reads no further frame
    # while a sample is being handled.
    assert all(s.image is source.frames[s.captured_at] for s in recorder.samples)
    assert source_positions == [s.captured_at for s in recorder.samples]
    assert recorder.statuses() == [("slow", True), ("fast", True)]


def test_only_frames_a_camera_samples_are_converted_once_each() -> None:
    stop = threading.Event()
    two_seconds_at_10_fps = [index / 10 for index in range(20)]
    source = ScriptedSource({URL: [[*two_seconds_at_10_fps, STOP]]}, stop)
    hub = CameraStreamHub(
        {"slow": camera(fps=1), "fast": camera(fps=2)},
        frame_source=source,
        clock=source.clock,
        reconnect_delay=no_delay,
    )
    recorder = Recorder()

    run_hub(hub, recorder.on_sample, recorder.on_status, stop)

    # Of 20 decoded frames, only the 4 that a camera sampled were converted, each
    # once, even when both cameras sampled it; both then share one array.
    assert source.conversions == [0.0, 0.5, 1.0, 1.5]
    images = {(s.camera_id, s.captured_at): s.image for s in recorder.samples}
    assert images[("slow", 0.0)] is images[("fast", 0.0)]
    assert images[("slow", 1.0)] is images[("fast", 1.0)]


def test_a_frame_that_fails_to_convert_is_a_stream_failure() -> None:
    stop = threading.Event()
    source = ScriptedSource({URL: [[UNCONVERTIBLE], [0.0, STOP]]}, stop)
    attempts: list[int] = []

    def record_attempt(attempt: int) -> float:
        attempts.append(attempt)
        return 0.0

    hub = CameraStreamHub(
        {"garage": camera()},
        frame_source=source,
        clock=source.clock,
        reconnect_delay=record_attempt,
    )
    recorder = Recorder()

    run_hub(hub, recorder.on_sample, recorder.on_status, stop)

    # The bad first frame never brings the camera online; the hub reconnects.
    assert attempts == [1]
    assert source.opened == [URL, URL]
    assert recorder.events == [("status", "garage", True), ("sample", "garage", 0.0)]


def test_first_frame_after_a_reconnect_is_sampled_at_once_as_a_new_baseline() -> None:
    stop = threading.Event()
    source = ScriptedSource(
        {
            URL: [
                [0.0, 0.1, 0.2, StreamError()],
                # Reconnected 0.3 s after the last sample, inside the 1 s interval.
                [0.3, STOP],
            ]
        },
        stop,
    )
    hub = CameraStreamHub(
        {"garage": camera(fps=1)},
        frame_source=source,
        clock=source.clock,
        reconnect_delay=no_delay,
    )
    recorder = Recorder()

    run_hub(hub, recorder.on_sample, recorder.on_status, stop)

    assert recorder.events == [
        ("status", "garage", True),
        ("sample", "garage", 0.0),
        ("status", "garage", False),
        ("status", "garage", True),
        ("sample", "garage", 0.3),
    ]


def test_reconnect_delay_doubles_from_one_second_and_caps_at_thirty() -> None:
    delays = [reconnect_delay(attempt) for attempt in range(1, 8)]

    assert delays == [1, 2, 4, 8, 16, 30, 30]


def test_reconnect_delay_stays_capped_for_a_camera_that_is_down_for_hours() -> None:
    assert reconnect_delay(10_000) == 30


def test_retry_attempts_grow_while_offline_and_reset_after_a_decoded_frame() -> None:
    stop = threading.Event()
    source = ScriptedSource(
        {
            URL: [
                StreamError(),  # opening fails while already offline
                StreamError(),
                [0.0, StreamError()],  # one decoded frame, then a decode failure
                StreamError(),
                [STOP],
            ]
        },
        stop,
    )
    attempts: list[int] = []

    def record_attempt(attempt: int) -> float:
        attempts.append(attempt)
        return 0.0

    hub = CameraStreamHub(
        {"garage": camera()},
        frame_source=source,
        clock=source.clock,
        reconnect_delay=record_attempt,
    )
    recorder = Recorder()

    run_hub(hub, recorder.on_sample, recorder.on_status, stop)

    assert attempts == [1, 2, 1, 2]
    assert recorder.statuses() == [("garage", True), ("garage", False)]


def test_a_stream_that_ends_counts_as_a_disconnect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="djev_sensors")
    stop = threading.Event()
    source = ScriptedSource({URL: [[0.0], [STOP]]}, stop)
    hub = CameraStreamHub(
        {"garage": camera()},
        frame_source=source,
        clock=source.clock,
        reconnect_delay=no_delay,
    )
    recorder = Recorder()

    run_hub(hub, recorder.on_sample, recorder.on_status, stop)

    assert recorder.statuses() == [("garage", True), ("garage", False)]
    assert source.opened == [URL, URL]
    events = [event for _, event in djev_events(caplog)]
    disconnected = [e for e in events if e["event"] == "camera.disconnected"]
    assert disconnected == [
        {"event": "camera.disconnected", "camera": "garage", "error": "end_of_stream"}
    ]


def test_each_distinct_url_gets_its_own_decoder() -> None:
    stop = threading.Event()
    source = ScriptedSource(
        {URL: [[0.0, WAIT_FOR_STOP]], OTHER_URL: [[0.0, WAIT_FOR_STOP]]}, stop
    )
    sampled: set[str] = set()

    def on_sample(sample: FrameSample) -> None:
        sampled.add(sample.camera_id)
        if sampled == {"garage", "porch"}:
            stop.set()

    hub = CameraStreamHub(
        {"garage": camera(URL), "porch": camera(OTHER_URL)},
        frame_source=source,
        reconnect_delay=no_delay,
    )

    run_hub(hub, on_sample, ignore_status, stop)

    assert sorted(source.opened) == sorted([URL, OTHER_URL])
    assert sorted(source.closed) == sorted([URL, OTHER_URL])


def test_an_exception_from_on_sample_stops_every_worker_and_is_reraised() -> None:
    stop = threading.Event()
    source = ScriptedSource(
        {URL: [[0.0, WAIT_FOR_STOP]], OTHER_URL: [[0.0, WAIT_FOR_STOP]]}, stop
    )
    porch_sampled = threading.Event()

    def on_sample(sample: FrameSample) -> None:
        if sample.camera_id == "porch":
            porch_sampled.set()
            return
        # Fail only once the other URL's worker is running, so the failure must stop it.
        porch_sampled.wait(timeout=5)
        raise RuntimeError("detector bug")

    hub = CameraStreamHub(
        {"garage": camera(URL), "porch": camera(OTHER_URL)},
        frame_source=source,
        reconnect_delay=no_delay,
    )

    with pytest.raises(RuntimeError, match="detector bug"):
        run_hub(hub, on_sample, ignore_status, stop)

    assert sorted(source.closed) == sorted([URL, OTHER_URL])


def test_an_exception_from_on_status_while_going_offline_is_reraised() -> None:
    stop = threading.Event()
    source = ScriptedSource({URL: [[0.0, StreamError()]]}, stop)

    def on_status(camera_id: str, connected: bool) -> None:
        if not connected:
            raise RuntimeError("publisher bug")

    hub = CameraStreamHub(
        {"garage": camera()},
        frame_source=source,
        clock=source.clock,
        reconnect_delay=no_delay,
    )

    with pytest.raises(RuntimeError, match="publisher bug"):
        run_hub(hub, ignore_sample, on_status, stop)

    assert source.opened == [URL]  # the bug is not retried like a stream failure


def test_stop_interrupts_the_backoff_sleep() -> None:
    stop = threading.Event()
    source = ScriptedSource({URL: [StreamError()]}, stop)
    backing_off = threading.Event()

    def long_delay(attempt: int) -> float:
        backing_off.set()
        return 30.0

    hub = CameraStreamHub(
        {"garage": camera()}, frame_source=source, reconnect_delay=long_delay
    )
    runner = threading.Thread(target=hub.run, args=(ignore_sample, ignore_status, stop))

    runner.start()
    try:
        assert backing_off.wait(timeout=5)
    finally:
        stop.set()
        runner.join(timeout=5)

    assert not runner.is_alive()


def test_a_stalled_stream_resumes_its_cadence_without_a_burst_of_samples() -> None:
    stop = threading.Event()
    frame_times = [0.0, 0.1, 3.05, 3.1, 3.2, 3.3, 3.4, 3.5]
    source = ScriptedSource({URL: [[*frame_times, STOP]]}, stop)
    hub = CameraStreamHub(
        {"garage": camera(fps=2)},
        frame_source=source,
        clock=source.clock,
        reconnect_delay=no_delay,
    )
    recorder = Recorder()

    run_hub(hub, recorder.on_sample, recorder.on_status, stop)

    assert recorder.sample_times("garage") == [0.0, 3.05, 3.5]


def test_status_changes_are_logged_by_camera_id_without_the_url_or_error_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="djev_sensors")
    config = camera(
        "rtsp://admin:${CAMERA_PASSWORD}@camera.test/stream",
        env={"CAMERA_PASSWORD": "hunter2"},
    )
    secret_url = config.rtsp.get_secret_value()
    reset = ConnectionResetError(errno.ECONNRESET, f"reset by peer: {secret_url!r}")
    stop = threading.Event()
    source = ScriptedSource({secret_url: [[0.0, reset]]}, stop)

    def stop_after_first_failure(attempt: int) -> float:
        stop.set()
        return 1.0

    hub = CameraStreamHub(
        {"garage": config},
        frame_source=source,
        clock=source.clock,
        reconnect_delay=stop_after_first_failure,
    )

    run_hub(hub, ignore_sample, ignore_status, stop)

    failure = {"error": "ConnectionResetError", "errno": errno.ECONNRESET}
    assert djev_events(caplog) == [
        (logging.INFO, {"event": "camera.connected", "camera": "garage"}),
        (
            logging.WARNING,
            {"event": "camera.disconnected", "camera": "garage", **failure},
        ),
        (
            logging.INFO,
            {
                "event": "camera.reconnecting",
                "camera": "garage",
                "attempt": 1,
                "delay": 1.0,
                **failure,
            },
        ),
    ]
    assert "hunter2" not in caplog.text
    assert "rtsp://" not in caplog.text
