# ABOUTME: Integration tests for djev_sensors.app: the real hub, detector, and scheduler
# ABOUTME: fed scripted frames, with a scripted model and a recording publisher.
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray

from djev_sensors import app as app_module
from djev_sensors.app import Application
from djev_sensors.camera import CameraStreamHub
from djev_sensors.config import AppConfig
from djev_sensors.detectors.base import ChangeDetector, ChangeResult
from djev_sensors.detectors.frame_difference import FrameDifferenceDetector
from djev_sensors.models.base import BinaryJudgment, ModelError
from djev_sensors.scheduler import SensorScheduler

CAR = "Is a car in the driveway?"
GATE = "Is the gate open?"
# Source frames are 640 wide, so they never match the 320-wide detection frames.
SOURCE_SHAPE = (360, 640, 3)
RUN_TIMEOUT = 10.0  # seconds before a run that never returns fails the test
LIFECYCLE = ["publisher.start", "publisher.stop", "model.aclose"]


def make_config() -> AppConfig:
    """One camera at 1 FPS, watched by two sensors with different change thresholds."""
    return AppConfig.model_validate(
        {
            "system": {
                "mqtt": {"host": "mqtt.test"},
                "model": {
                    "provider": "lunaroute",
                    "model": "djev",
                    "api_key_env": "LUNAROUTE_API_KEY",
                    # Without a wrapper, each sensor's instructions are its prompt.
                    "prompt_wrapper": "",
                },
            },
            "cameras": {"driveway": {"rtsp": "rtsp://driveway.test/stream", "fps": 1}},
            "sensors": {
                "car_present": {
                    "name": "Car Present",
                    "camera": "driveway",
                    "prompt": CAR,
                    "change_threshold_pct": 2.5,
                    "cooldown_seconds": 0,
                },
                "gate_open": {
                    "name": "Gate Open",
                    "camera": "driveway",
                    "prompt": GATE,
                    "change_threshold_pct": 10,
                    "cooldown_seconds": 0,
                },
            },
        },
        context={"env": {"LUNAROUTE_API_KEY": "test-key"}},
    )


def new_detector() -> FrameDifferenceDetector:
    detection = make_config().system.change_detection
    return FrameDifferenceDetector(
        detection.width, detection.pixel_delta_threshold, detection.blur
    )


def score(previous: NDArray[np.uint8], current: NDArray[np.uint8]) -> float:
    """The change score the real detector gives `current` against `previous`."""
    detector = new_detector()
    return detector.compare(
        detector.prepare(previous), detector.prepare(current)
    ).changed_pct


def solid(value: int, shape: tuple[int, int, int] = SOURCE_SHAPE) -> NDArray[np.uint8]:
    return np.full(shape, value, dtype=np.uint8)


def with_block(
    frame: NDArray[np.uint8], *, x: int, y: int, width: int, height: int
) -> NDArray[np.uint8]:
    """A copy of `frame` with one bright block, as if something moved into view."""
    changed = frame.copy()
    changed[y : y + height, x : x + width] = 220
    return changed


def small_change(frame: NDArray[np.uint8]) -> NDArray[np.uint8]:
    """About 5% of the frame: past the car sensor's threshold, short of the gate's."""
    return with_block(frame, x=20, y=20, width=120, height=96)


def judgment(probability: float) -> BinaryJudgment:
    return BinaryJudgment(
        true_probability=probability, sent_width=640, sent_height=360, latency_ms=241
    )


# The camera boundary: a frame source the real hub opens and decodes.


class StreamError(Exception):
    """A stream failure raised by a scripted connection."""


class ScriptExhausted(BaseException):
    """The hub opened the stream more often than the script allows.

    A BaseException, so the hub reports it as a bug instead of reconnecting.
    """


class Stop:
    """Script step: set the stop event, then end the stream."""


@dataclass(eq=False)
class Stall:
    """Script step: set the stop event, then stall the read until `release` is set."""

    release: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None  # the hub worker thread that stalled


@dataclass(eq=False)
class StopWhileConverting:
    """Script step: a frame whose BGR conversion sets the stop event, then returns it.

    That happens after the hub's own stop check, so the sample still reaches
    the app's callback.
    """

    image: NDArray[np.uint8]


Step = NDArray[np.uint8] | Exception | Stop | Stall | StopWhileConverting


class Frame:
    """A decoded frame that converts to `image`, first setting `stop` if given."""

    def __init__(
        self, image: NDArray[np.uint8], stop: threading.Event | None = None
    ) -> None:
        self._image = image
        self._stop = stop

    def to_bgr(self) -> NDArray[np.uint8]:
        if self._stop is not None:
            self._stop.set()
        return self._image


class ScriptedCamera:
    """Frame source whose every open plays the next scripted connection.

    An array step yields a frame that converts to that very array. Each
    frame advances the clock seam by one second, so at 1 FPS the hub samples
    every frame.
    """

    def __init__(
        self, connections: Sequence[Sequence[Step]], stop: threading.Event
    ) -> None:
        self._connections = iter(connections)
        self._stop = stop
        self._now = 0.0

    def clock(self) -> float:
        return self._now

    def __call__(self, url: str) -> Iterator[Frame]:
        steps = next(self._connections, None)
        if steps is None:
            raise ScriptExhausted(url)
        return self._play(steps)

    def _play(self, steps: Sequence[Step]) -> Iterator[Frame]:
        for step in steps:
            if isinstance(step, Stop):
                self._stop.set()
                return
            if isinstance(step, Exception):
                raise step
            self._now += 1.0
            if isinstance(step, Stall):
                step.thread = threading.current_thread()
                self._stop.set()
                step.release.wait()
                yield Frame(solid(0))
            elif isinstance(step, StopWhileConverting):
                yield Frame(step.image, stop=self._stop)
            else:
                yield Frame(step)


# The model and publisher boundaries.

Outcome = BinaryJudgment | Exception


@dataclass
class Held:
    """A scripted model call that waits for `release`, then produces `outcome`."""

    outcome: Outcome
    release: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class Delayed:
    """A scripted model call that takes `seconds`, then produces `outcome`."""

    outcome: Outcome
    seconds: float


ModelStep = Outcome | Held | Delayed


class ScriptedModel:
    """ModelClient fake: calls with the same instructions consume one script in order.

    Records each call's instructions and image as the call begins, the
    instructions of each call cancelled while held, and aclose in `lifecycle`.
    """

    def __init__(
        self, scripts: Mapping[str, Sequence[ModelStep]], lifecycle: list[str]
    ) -> None:
        self._scripts = {key: list(steps) for key, steps in scripts.items()}
        self._lifecycle = lifecycle
        self.calls: list[tuple[str, NDArray[np.uint8]]] = []
        self.cancelled: list[str] = []

    async def evaluate_binary(
        self, image: NDArray[np.uint8], instructions: str
    ) -> BinaryJudgment:
        self.calls.append((instructions, image))
        step = self._scripts[instructions].pop(0)
        if isinstance(step, Held):
            try:
                await step.release.wait()
            except asyncio.CancelledError:
                self.cancelled.append(instructions)
                raise
            step = step.outcome
        elif isinstance(step, Delayed):
            await asyncio.sleep(step.seconds)
            step = step.outcome
        if isinstance(step, Exception):
            raise step
        return step

    async def aclose(self) -> None:
        self._lifecycle.append("model.aclose")


class RecordingPublisher:
    """Publisher fake: records each publication as (kind, sensor, value) in order,
    and start and stop in `lifecycle`."""

    def __init__(self, lifecycle: list[str]) -> None:
        self._lifecycle = lifecycle
        self.calls: list[tuple[str, str, Any]] = []
        self.calls_at_stop: int | None = None  # how many had been published

    def start(self) -> None:
        self._lifecycle.append("publisher.start")

    def stop(self) -> None:
        self._lifecycle.append("publisher.stop")
        self.calls_at_stop = len(self.calls)

    def publish_state(self, sensor_id: str, state: bool) -> None:
        self.calls.append(("state", sensor_id, state))

    def publish_attributes(
        self, sensor_id: str, attributes: Mapping[str, object]
    ) -> None:
        self.calls.append(("attributes", sensor_id, dict(attributes)))

    def publish_availability(self, sensor_id: str, online: bool) -> None:
        self.calls.append(("availability", sensor_id, online))

    def values(self, kind: str, sensor_id: str) -> list[Any]:
        """The values of one sensor's publications of one kind, in order."""
        return [value for k, s, value in self.calls if k == kind and s == sensor_id]

    def change_pcts(self) -> list[tuple[str, float]]:
        """Each published attribute set's sensor and change score, in order."""
        return [
            (sensor_id, value["change_pct"])
            for kind, sensor_id, value in self.calls
            if kind == "attributes"
        ]


@dataclass
class Harness:
    app: Application
    hub: CameraStreamHub
    model: ScriptedModel
    publisher: RecordingPublisher
    lifecycle: list[str]


def make_harness(
    source: ScriptedCamera,
    scripts: Mapping[str, Sequence[ModelStep]],
    detector: ChangeDetector | None = None,
) -> Harness:
    """The app wired to the real hub, scheduler, and (by default) detector."""
    config = make_config()
    lifecycle: list[str] = []
    model = ScriptedModel(scripts, lifecycle)
    publisher = RecordingPublisher(lifecycle)
    hub = CameraStreamHub(
        config.cameras,
        frame_source=source,
        clock=source.clock,
        reconnect_delay=lambda attempt: 0.0,
    )
    app = Application(
        config,
        hub,
        detector or new_detector(),
        SensorScheduler(config, model, publisher),
        publisher,
        model,
    )
    return Harness(app, hub, model, publisher, lifecycle)


def run_app(app: Application, stop: threading.Event) -> None:
    """Run the app on a fresh event loop until it returns; fail instead of hanging."""
    asyncio.run(asyncio.wait_for(app.run(stop), RUN_TIMEOUT))


def logged(caplog: pytest.LogCaptureFixture) -> list[tuple[int, dict[str, Any]]]:
    """Each event logged on the djev_sensors logger as (level, fields), in order."""
    return [
        (record.levelno, json.loads(record.getMessage()))
        for record in caplog.records
        if record.name == "djev_sensors"
    ]


def events_named(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    return [fields for _, fields in logged(caplog) if fields["event"] == name]


def camera_hub_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name == app_module.HUB_THREAD_NAME}


def test_each_sample_is_scored_against_the_previous_one_for_every_eligible_sensor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="djev_sensors")
    baseline = solid(60)
    unchanged = baseline.copy()
    car_only = small_change(unchanged)
    drift = with_block(car_only, x=520, y=280, width=48, height=48)
    both = with_block(drift, x=200, y=20, width=240, height=144)
    names = {
        id(image): name
        for name, image in [
            ("baseline", baseline),
            ("unchanged", unchanged),
            ("car_only", car_only),
            ("drift", drift),
            ("both", both),
        ]
    }
    car_only_pct = score(unchanged, car_only)
    drift_pct = score(car_only, drift)
    both_pct = score(drift, both)
    # What the scenario relies on: car_only passes only the car sensor's
    # threshold, drift passes neither, both passes both, and scoring both
    # against any earlier frame, such as the last one that triggered a
    # sensor, would give a different score.
    assert 2.5 <= car_only_pct < 10
    assert 0 < drift_pct < 2.5
    assert both_pct >= 10
    stale = {round(score(frame, both), 2) for frame in (baseline, car_only)}
    assert round(both_pct, 2) not in stale
    stop = threading.Event()
    source = ScriptedCamera(
        [[baseline, unchanged, car_only, drift, both, Stop()]], stop
    )
    h = make_harness(
        source, {CAR: [judgment(0.9), judgment(0.95)], GATE: [judgment(0.1)]}
    )

    run_app(h.app, stop)

    # Nothing for the baseline, the unchanged sample, or the drift; one request
    # per eligible sensor, each carrying the source frame its score came from.
    assert [(prompt, names.get(id(image))) for prompt, image in h.model.calls] == [
        (CAR, "car_only"),
        (CAR, "both"),
        (GATE, "both"),
    ]
    assert {image.shape for _, image in h.model.calls} == {SOURCE_SHAPE}
    # One score per sample, shared by every sensor it triggered. The drift
    # became the baseline though it triggered nothing.
    assert h.publisher.change_pcts() == [
        ("car_present", round(car_only_pct, 2)),
        ("car_present", round(both_pct, 2)),
        ("gate_open", round(both_pct, 2)),
    ]
    assert h.publisher.values("state", "car_present") == [True]
    assert h.publisher.values("state", "gate_open") == [False]
    # Only scores that reach the camera's lowest sensor threshold are logged.
    assert events_named(caplog, "frame.change") == [
        {"event": "frame.change", "camera": "driveway", "change_pct": pct}
        for pct in (round(car_only_pct, 2), round(both_pct, 2))
    ]
    events = [fields for _, fields in logged(caplog)]
    assert events[0] == {
        "event": "service.started",
        "cameras": ["driveway"],
        "sensors": ["car_present", "gate_open"],
    }
    assert events[-1] == {"event": "service.stopped"}
    assert h.lifecycle == LIFECYCLE


def test_a_camera_recovery_leaves_a_sensor_offline_while_its_model_is_failing() -> None:
    before_outage = solid(60)
    changed = small_change(before_outage)
    after_outage = solid(200)
    stop = threading.Event()
    source = ScriptedCamera(
        [
            [before_outage, changed, StreamError()],
            [after_outage, after_outage.copy(), Stop()],
        ],
        stop,
    )
    h = make_harness(source, {CAR: [ModelError("LunaRoute returned HTTP 503")]})

    run_app(h.app, stop)

    # The first sample after the reconnect only sets a new baseline, however
    # much it differs from the last sample before the outage.
    assert [(prompt, image is changed) for prompt, image in h.model.calls] == [
        (CAR, True)
    ]
    # The car sensor's model failed before the outage, so the camera's
    # recovery leaves it offline; the gate sensor comes back online.
    assert h.publisher.values("availability", "car_present") == [True, False]
    assert h.publisher.values("availability", "gate_open") == [True, False, True]
    assert h.lifecycle == LIFECYCLE


def test_a_resolution_change_starts_a_new_baseline_instead_of_failing() -> None:
    wide = solid(60)
    # A different aspect ratio gives a detection frame of a different shape.
    taller = solid(60, shape=(480, 640, 3))
    changed = small_change(taller)
    assert 2.5 <= score(taller, changed) < 10  # the car sensor's threshold only
    stop = threading.Event()
    source = ScriptedCamera([[wide, taller, changed, Stop()]], stop)
    h = make_harness(source, {CAR: [judgment(0.9)]})

    run_app(h.app, stop)

    assert [(prompt, image is changed) for prompt, image in h.model.calls] == [
        (CAR, True)
    ]
    assert h.publisher.change_pcts() == [
        ("car_present", round(score(taller, changed), 2))
    ]


def test_a_sample_that_arrives_after_stop_starts_no_evaluation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="djev_sensors")
    before = solid(60)
    stop = threading.Event()
    source = ScriptedCamera([[before, StopWhileConverting(small_change(before))]], stop)
    h = make_harness(source, {CAR: [judgment(0.9)]})

    run_app(h.app, stop)

    assert h.model.calls == []
    assert events_named(caplog, "frame.change") == []
    assert h.lifecycle == LIFECYCLE


def test_shutdown_lets_a_running_evaluation_finish_and_publish_first() -> None:
    before = solid(60)
    stop = threading.Event()
    source = ScriptedCamera([[before, small_change(before), Stop()]], stop)
    # Still running when stop arrives, and done well inside the drain bound.
    h = make_harness(source, {CAR: [Delayed(judgment(0.9), seconds=0.5)]})

    run_app(h.app, stop)

    assert h.publisher.values("state", "car_present") == [True]
    # Everything was published before the publisher stopped.
    assert h.publisher.calls_at_stop == len(h.publisher.calls)
    assert h.lifecycle == LIFECYCLE


def test_shutdown_cancels_an_evaluation_still_running_at_the_drain_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "DRAIN_TIMEOUT_SECONDS", 0.3)
    before = solid(60)
    stop = threading.Event()
    source = ScriptedCamera([[before, small_change(before), Stop()]], stop)
    h = make_harness(source, {CAR: [Held(judgment(0.9))]})  # never released

    run_app(h.app, stop)

    assert h.model.cancelled == [CAR]
    # A cancelled evaluation publishes nothing; only the camera's status did.
    assert [kind for kind, _, _ in h.publisher.calls] == ["availability"] * 2
    assert h.lifecycle == LIFECYCLE


def test_shutdown_stops_waiting_for_a_stalled_camera_hub_at_its_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "HUB_STOP_TIMEOUT_SECONDS", 0.5)
    stop = threading.Event()
    stall = Stall()
    source = ScriptedCamera([[solid(60), stall]], stop)
    h = make_harness(source, {})
    hub_threads_before = camera_hub_threads()

    started = time.monotonic()
    try:
        run_app(h.app, stop)
        elapsed = time.monotonic() - started
        # run() returned while the stalled read still held the decoder open.
        assert h.hub.open_decoder_count() == 1
    finally:
        stall.release.set()

    assert elapsed < 2.0
    assert h.lifecycle == LIFECYCLE
    # Once the read returns, the hub and its thread finish without an error.
    assert stall.thread is not None
    stall.thread.join(timeout=5)
    for thread in camera_hub_threads() - hub_threads_before:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert h.hub.open_decoder_count() == 0


class BrokenDetector:
    """A detector with a bug: preparing any frame raises."""

    def prepare(self, frame: NDArray[np.uint8]) -> NDArray[np.uint8]:
        raise RuntimeError("detector bug")

    def compare(
        self, previous: NDArray[np.uint8], current: NDArray[np.uint8]
    ) -> ChangeResult:
        raise RuntimeError("detector bug")


def test_a_callback_bug_stops_the_service_and_is_reraised_after_cleanup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="djev_sensors")
    stop = threading.Event()
    source = ScriptedCamera([[solid(60)]], stop)
    h = make_harness(source, {}, detector=BrokenDetector())

    with pytest.raises(RuntimeError, match="detector bug"):
        run_app(h.app, stop)

    assert h.lifecycle == LIFECYCLE
    assert logged(caplog)[-1] == (
        logging.ERROR,
        {"event": "service.stopped", "error": "RuntimeError"},
    )
