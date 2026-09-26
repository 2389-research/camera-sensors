# ABOUTME: Shared RTSP streams: one decoder thread per distinct URL, sampled for each
# ABOUTME: camera ID at its own FPS, with online/offline status and backoff reconnects.
from __future__ import annotations

import logging
import math
import threading
import time
import traceback
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import NotRequired, Protocol, TypedDict

import av
import numpy as np
from numpy.typing import NDArray

from djev_sensors.config import CameraConfig
from djev_sensors.events import log_event

_MAX_RECONNECT_DELAY_SECONDS = 30.0


class DecodedFrame(Protocol):
    """A decoded video frame, converted to a BGR array only on request.

    The hub converts only frames that some camera ID samples, since conversion
    allocates a full-size array.
    """

    def to_bgr(self) -> NDArray[np.uint8]:
        """Return the frame as a BGR array at source resolution."""
        ...


# Opens a URL and yields its decoded frames, not yet converted.
FrameSource = Callable[[str], Iterator[DecodedFrame]]


@dataclass(frozen=True, eq=False)
class FrameSample:
    """One decoded frame sampled for one camera ID.

    `image` is BGR at source resolution. Camera IDs that share a URL may receive
    the same array object, so consumers must not modify it. `captured_at` is the
    monotonic time, in seconds, at which the frame was decoded.
    """

    camera_id: str
    image: NDArray[np.uint8]
    captured_at: float


def reconnect_delay(attempt: int) -> float:
    """Seconds to wait before reconnect attempt `attempt`, counted from 1.

    Doubles from 1 second and caps at 30: 1, 2, 4, 8, 16, 30, 30, ...
    """
    # Capping the exponent as well keeps a camera that stays down for hours from
    # overflowing the float power.
    return min(2.0 ** min(attempt - 1, 5), _MAX_RECONNECT_DELAY_SECONDS)


class _PyAVFrame:
    """A frame PyAV decoded, converted to BGR only when a camera samples it."""

    def __init__(self, frame: av.VideoFrame) -> None:
        self._frame = frame

    def to_bgr(self) -> NDArray[np.uint8]:
        return np.asarray(self._frame.to_ndarray(format="bgr24"), dtype=np.uint8)


def open_rtsp_frames(url: str) -> Iterator[DecodedFrame]:
    """Decode every frame of the first video stream of `url` with PyAV.

    Errors propagate: open, read, and decode errors from the generator, and
    conversion errors from `to_bgr()`. PyAV messages can quote the URL,
    credentials included, so callers must never log them. Closing the
    generator closes the container.
    """
    # FFmpeg's own log messages can quote the URL too. PyAV discards them by
    # default; pin that here so nothing that re-enabled them sees this URL.
    av.logging.set_level(None)
    with av.open(
        url, options={"rtsp_transport": "tcp"}, timeout=(10.0, 10.0)
    ) as container:
        for frame in container.decode(video=0):
            yield _PyAVFrame(frame)


class CameraStreamHub:
    """Decodes each distinct RTSP URL once and samples it for every camera ID on it.

    Camera IDs whose resolved URLs match exactly share one decoder but keep
    separate sample clocks.
    """

    def __init__(
        self,
        cameras: Mapping[str, CameraConfig],
        *,
        frame_source: FrameSource = open_rtsp_frames,
        clock: Callable[[], float] = time.monotonic,
        reconnect_delay: Callable[[int], float] = reconnect_delay,
    ) -> None:
        self._streams: dict[str, dict[str, float]] = {}
        for camera_id, camera in cameras.items():
            url = camera.rtsp.get_secret_value()
            self._streams.setdefault(url, {})[camera_id] = camera.fps
        self._frame_source = frame_source
        self._clock = clock
        self._reconnect_delay = reconnect_delay
        self._lock = threading.Lock()
        self._open_decoders = 0

    def open_decoder_count(self) -> int:
        """Number of decoders open right now; the hub keeps at most one per URL."""
        with self._lock:
            return self._open_decoders

    def run(
        self,
        on_sample: Callable[[FrameSample], None],
        on_status: Callable[[str, bool], None],
        stop_event: threading.Event,
    ) -> None:
        """Decode every distinct URL on its own worker thread until `stop_event` is set.

        `on_status(camera_id, connected)` fires only on transitions. Each camera
        ID starts offline and goes online at its URL's first decoded frame after
        a (re)connect. It goes offline when opening, reading, decoding, or
        converting a frame fails, a read times out, or the stream ends; the hub
        then waits `reconnect_delay(attempt)` seconds and reopens the URL.

        The first frame after each (re)connect is sampled at once, then each
        camera ID samples at its configured FPS. `on_sample` runs on the URL's
        worker thread, which reads no further frame until it returns. With
        several URLs, callbacks for different URLs run concurrently on
        different worker threads.

        An exception from `on_sample` or `on_status` is a bug, not a stream
        failure: the hub logs it with its traceback as `camera.callback_failed`,
        sets `stop_event`, stops every worker, and re-raises the exception here.

        Returns once every decoder is closed and every worker thread has
        exited. Shutdown can wait up to one read timeout for a stalled stream.
        """
        failures: list[BaseException] = []
        threads: list[threading.Thread] = []
        try:
            for url, cameras in self._streams.items():
                worker = _StreamWorker(
                    url,
                    cameras,
                    self._open_counted,
                    self._clock,
                    self._reconnect_delay,
                    on_sample,
                    on_status,
                    stop_event,
                )
                thread = threading.Thread(
                    target=_run_worker,
                    args=(worker, failures, stop_event),
                    # Name threads by camera ID only: the URL may hold credentials.
                    name=f"rtsp:{','.join(cameras)}",
                    # A caller abandoned at interpreter exit must not hang the process.
                    daemon=True,
                )
                thread.start()
                threads.append(thread)
            stop_event.wait()
        finally:
            stop_event.set()
            for thread in threads:
                thread.join()
        if failures:
            raise failures[0]

    def _open_counted(self, url: str) -> Generator[DecodedFrame, None, None]:
        """Yield frames from the frame source, counting it as open until closed."""
        with self._lock:
            self._open_decoders += 1
        try:
            yield from self._frame_source(url)
        finally:
            with self._lock:
                self._open_decoders -= 1


def _run_worker(
    worker: _StreamWorker,
    failures: list[BaseException],
    stop_event: threading.Event,
) -> None:
    try:
        worker.run()
    except BaseException as exc:  # a bug, not a stream failure: stop every worker
        failures.append(exc)
        stop_event.set()


class _SampleClock:
    """Picks which decoded frames one camera ID samples, at its configured FPS.

    Samples keep a steady cadence from the first frame. Slots that pass with no
    decoded frame, as in a stall, are skipped rather than made up in a burst.
    """

    def __init__(self, fps: float) -> None:
        self._interval = 1.0 / fps
        self._next_due: float | None = None

    def restart(self) -> None:
        """Sample the next frame at once and restart the cadence from it."""
        self._next_due = None

    def should_sample(self, now: float) -> bool:
        """Return whether the frame decoded at `now` is due, scheduling the next."""
        if self._next_due is None:
            self._next_due = now + self._interval
            return True
        if now < self._next_due:
            return False
        missed_slots = math.floor((now - self._next_due) / self._interval)
        self._next_due += (missed_slots + 1) * self._interval
        return True


class _StreamWorker:
    """Decodes one URL and samples it for every camera ID that shares it.

    Runs on its own thread. Stream failures are handled here; any other
    exception, including one raised by a callback, propagates out of run().
    """

    def __init__(
        self,
        url: str,
        cameras: Mapping[str, float],
        frame_source: Callable[[str], Generator[DecodedFrame, None, None]],
        clock: Callable[[], float],
        reconnect_delay: Callable[[int], float],
        on_sample: Callable[[FrameSample], None],
        on_status: Callable[[str, bool], None],
        stop_event: threading.Event,
    ) -> None:
        self._url = url
        self._sample_clocks = {
            camera_id: _SampleClock(fps) for camera_id, fps in cameras.items()
        }
        self._frame_source = frame_source
        self._clock = clock
        self._reconnect_delay = reconnect_delay
        self._on_sample = on_sample
        self._on_status = on_status
        self._stop_event = stop_event
        self._online = False
        self._attempt = 0  # failed attempts since the last decoded frame

    def run(self) -> None:
        while not self._stop_event.is_set():
            failure = _failure_fields(self._decode_until_failure())
            if self._stop_event.is_set():
                return
            self._mark_offline(failure)
            self._attempt += 1
            delay = self._reconnect_delay(self._attempt)
            for camera_id in self._sample_clocks:
                log_event(
                    "camera.reconnecting",
                    camera=camera_id,
                    attempt=self._attempt,
                    delay=delay,
                    **failure,
                )
            self._stop_event.wait(delay)

    def _decode_until_failure(self) -> Exception | None:
        """Sample decoded frames until the stream fails or stop is requested.

        Returns the exception that ended the stream, or None if it ended
        without one or stop was requested. The frame source opens lazily, so
        open failures surface here too. Only frames that some camera ID is due
        to sample get converted, once each.
        """
        for sample_clock in self._sample_clocks.values():
            sample_clock.restart()
        frames = self._frame_source(self._url)
        try:
            while True:
                try:
                    frame = next(frames)
                except StopIteration:
                    return None
                except Exception as exc:  # open, read, decode, or timeout failure
                    return exc
                if self._stop_event.is_set():
                    return None
                captured_at = self._clock()
                due = [
                    camera_id
                    for camera_id, sample_clock in self._sample_clocks.items()
                    if sample_clock.should_sample(captured_at)
                ]
                if not due:
                    continue
                try:
                    image = frame.to_bgr()
                except Exception as exc:  # the decoded frame would not convert
                    return exc
                # A connection's first frame is due for every camera ID, so the
                # stream goes online only once a frame has converted.
                if not self._online:
                    self._mark_online()
                for camera_id in due:
                    with _logging_failure("on_sample", camera_id):
                        self._on_sample(FrameSample(camera_id, image, captured_at))
        finally:
            frames.close()

    def _mark_online(self) -> None:
        self._online = True
        self._attempt = 0
        for camera_id in self._sample_clocks:
            log_event("camera.connected", camera=camera_id)
            with _logging_failure("on_status", camera_id):
                self._on_status(camera_id, True)

    def _mark_offline(self, failure: _FailureFields) -> None:
        if not self._online:
            return
        self._online = False
        for camera_id in self._sample_clocks:
            with _logging_failure("on_status", camera_id):
                self._on_status(camera_id, False)
            log_event(
                "camera.disconnected",
                level=logging.WARNING,
                camera=camera_id,
                **failure,
            )


@contextmanager
def _logging_failure(callback: str, camera_id: str) -> Iterator[None]:
    """Log an exception from a hub callback with its traceback, then let it propagate.

    A callback exception is an app bug that stops the hub. Logged here, as it
    happens, the traceback survives even when run() cannot re-raise it before
    a bounded shutdown stops waiting. Callbacks never run while a stream error
    is being handled, so no PyAV error, whose text can quote the RTSP URL, is
    ever chained into this traceback.
    """
    try:
        yield
    except BaseException as exc:
        log_event(
            "camera.callback_failed",
            level=logging.ERROR,
            camera=camera_id,
            callback=callback,
            error=type(exc).__name__,
            traceback="".join(traceback.format_exception(exc)),
        )
        raise


class _FailureFields(TypedDict):
    """Log fields describing why a stream stopped."""

    error: str
    errno: NotRequired[int]


def _failure_fields(failure: Exception | None) -> _FailureFields:
    """Log fields for a stream failure: the exception class and any errno.

    Never the message: PyAV error messages quote the URL, credentials included.
    """
    if failure is None:
        return {"error": "end_of_stream"}
    fields: _FailureFields = {"error": type(failure).__name__}
    errno = getattr(failure, "errno", None)
    if isinstance(errno, int):
        fields["errno"] = errno
    return fields
