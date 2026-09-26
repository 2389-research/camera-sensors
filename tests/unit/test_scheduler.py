# ABOUTME: Unit tests for djev_sensors.scheduler: which sensors a change triggers,
# ABOUTME: cooldowns, request limits, stale camera results, and what outcomes publish.
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from unittest.mock import ANY

import numpy as np
import pytest
from numpy.typing import NDArray

from djev_sensors.config import AppConfig
from djev_sensors.models.base import BinaryJudgment, InvalidModelResponse, ModelError
from djev_sensors.scheduler import SensorScheduler

GATE = "Is the gate open?"
CAR = "Is a car in the driveway?"
PORCH = "Is someone on the porch?"

# What the utc_now seam returns; evaluated_at must drop the microseconds.
NOW_UTC = datetime(2026, 9, 25, 19, 32, 17, 480000, tzinfo=UTC)
EVALUATED_AT = "2026-09-25T19:32:17Z"


def new_frame() -> NDArray[np.uint8]:
    return np.zeros((4, 4, 3), dtype=np.uint8)


def judgment(probability: float) -> BinaryJudgment:
    """A valid Djev judgment for a 768x432 image that took 241 ms."""
    return BinaryJudgment(
        true_probability=probability, sent_width=768, sent_height=432, latency_ms=241
    )


Outcome = BinaryJudgment | Exception


@dataclass
class Held:
    """A scripted model call that waits for `release`, then produces `outcome`."""

    outcome: Outcome
    release: asyncio.Event = field(default_factory=asyncio.Event)


class ScriptedModel:
    """ModelClient fake: calls with the same instructions consume one script in order.

    Records each call's instructions and image as the call begins, and the
    instructions of each call cancelled while held.
    """

    def __init__(self, scripts: Mapping[str, Sequence[Outcome | Held]]) -> None:
        self._scripts = {key: list(steps) for key, steps in scripts.items()}
        self.calls: list[str] = []
        self.images: list[NDArray[np.uint8]] = []
        self.cancelled: list[str] = []

    async def evaluate_binary(
        self, image: NDArray[np.uint8], instructions: str
    ) -> BinaryJudgment:
        self.calls.append(instructions)
        self.images.append(image)
        step = self._scripts[instructions].pop(0)
        if isinstance(step, Held):
            try:
                await step.release.wait()
            except asyncio.CancelledError:
                self.cancelled.append(instructions)
                raise
            step = step.outcome
        if isinstance(step, Exception):
            raise step
        return step

    async def aclose(self) -> None:
        return None


class RecordingPublisher:
    """SensorPublisher fake: records each call in order as (kind, sensor, value)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def publish_state(self, sensor_id: str, state: bool) -> None:
        self.calls.append(("state", sensor_id, state))

    def publish_attributes(
        self, sensor_id: str, attributes: Mapping[str, object]
    ) -> None:
        self.calls.append(("attributes", sensor_id, dict(attributes)))

    def publish_availability(self, sensor_id: str, online: bool) -> None:
        self.calls.append(("availability", sensor_id, online))

    def values(self, kind: str, sensor_id: str) -> list[object]:
        """The values of one sensor's calls of one kind, in order."""
        return [value for k, s, value in self.calls if k == kind and s == sensor_id]


class FakeClock:
    """Monotonic clock seam that tests set by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def sensor(prompt: str, camera: str = "yard", **fields: object) -> dict[str, object]:
    """One sensor's config fields; the rest take their defaults."""
    return {"camera": camera, "prompt": prompt, **fields}


def make_config(
    sensors: Mapping[str, Mapping[str, object]],
    max_concurrent_requests: int,
    prompt_wrapper: str,
) -> AppConfig:
    cameras = {str(fields["camera"]) for fields in sensors.values()}
    return AppConfig.model_validate(
        {
            "system": {
                "mqtt": {"host": "mqtt.test"},
                "model": {
                    "provider": "lunaroute",
                    "model": "djev",
                    "api_key_env": "LUNAROUTE_API_KEY",
                    "prompt_wrapper": prompt_wrapper,
                },
                "inference": {"max_concurrent_requests": max_concurrent_requests},
            },
            "cameras": {
                camera: {"rtsp": f"rtsp://{camera}.test/stream"} for camera in cameras
            },
            "sensors": {
                sensor_id: {"name": sensor_id, **fields}
                for sensor_id, fields in sensors.items()
            },
        },
        context={"env": {"LUNAROUTE_API_KEY": "test-key"}},
    )


@dataclass
class Harness:
    scheduler: SensorScheduler
    model: ScriptedModel
    publisher: RecordingPublisher
    clock: FakeClock


def make_harness(
    sensors: Mapping[str, Mapping[str, object]],
    scripts: Mapping[str, Sequence[Outcome | Held]],
    *,
    max_concurrent_requests: int = 4,
    prompt_wrapper: str = "",
) -> Harness:
    """A scheduler wired to fakes.

    The wrapper defaults to empty, so each sensor's instructions are its
    prompt alone and scripts can be keyed by prompt.
    """
    model = ScriptedModel(scripts)
    publisher = RecordingPublisher()
    clock = FakeClock()
    scheduler = SensorScheduler(
        make_config(sensors, max_concurrent_requests, prompt_wrapper),
        model,
        publisher,
        clock=clock,
        utc_now=lambda: NOW_UTC,
    )
    return Harness(scheduler, model, publisher, clock)


def run(scenario: Coroutine[Any, Any, None]) -> None:
    """Run one scenario on a fresh event loop; fail instead of hanging after 5 s."""
    asyncio.run(asyncio.wait_for(scenario, timeout=5.0))


async def settle() -> None:
    """Let every ready task run until it blocks; scenarios do no real I/O."""
    for _ in range(20):
        await asyncio.sleep(0)


def logged(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    """The fields of each `event` logged on the djev_sensors logger, in order."""
    found = []
    for record in caplog.records:
        if record.name == "djev_sensors":
            fields = json.loads(record.getMessage())
            if fields["event"] == event:
                found.append(fields)
    return found


# What each inference outcome publishes


def test_valid_result_publishes_attributes_then_the_new_state() -> None:
    async def scenario() -> None:
        h = make_harness(
            {"gate": sensor(GATE, true_threshold=0.9)}, {GATE: [judgment(0.91)]}
        )
        h.scheduler.camera_status("yard", True)
        h.publisher.calls.clear()

        h.scheduler.on_change("yard", new_frame(), 4.7134)
        await h.scheduler.drain()

        assert h.publisher.calls == [
            (
                "attributes",
                "gate",
                {
                    "true_probability": 0.91,
                    "true_threshold": 0.9,
                    "change_pct": 4.71,
                    "camera": "yard",
                    "model": "djev",
                    "evaluated_at": EVALUATED_AT,
                    "latency_ms": 241,
                    "sent_width": 768,
                    "sent_height": 432,
                    "parse_error": False,
                    "trigger": "change",
                },
            ),
            ("state", "gate", True),
        ]

    run(scenario())


@pytest.mark.parametrize(
    ("wrapper", "instructions"),
    [
        (
            "Judge only what the frame shows.",
            "Judge only what the frame shows.\n\nIs the gate open?",
        ),
        ("", "Is the gate open?"),
    ],
    ids=["wrapper", "empty-wrapper"],
)
def test_instructions_are_the_wrapper_and_prompt_one_blank_line_apart(
    wrapper: str, instructions: str
) -> None:
    async def scenario() -> None:
        h = make_harness(
            {"gate": sensor(GATE)},
            {instructions: [judgment(0.91)]},
            prompt_wrapper=wrapper,
        )
        h.scheduler.camera_status("yard", True)

        h.scheduler.on_change("yard", new_frame(), 5.0)
        await h.scheduler.drain()

        assert h.model.calls == [instructions]

    run(scenario())


def test_uncertain_first_result_publishes_attributes_but_no_state() -> None:
    async def scenario() -> None:
        h = make_harness(
            {"gate": sensor(GATE, true_threshold=0.9)}, {GATE: [judgment(0.85)]}
        )
        h.scheduler.camera_status("yard", True)
        h.publisher.calls.clear()

        h.scheduler.on_change("yard", new_frame(), 5.0)
        await h.scheduler.drain()

        assert h.publisher.calls == [("attributes", "gate", ANY)]

    run(scenario())


def test_state_is_published_only_when_it_changes() -> None:
    async def scenario() -> None:
        results = [judgment(p) for p in (0.91, 0.95, 0.55, 0.09, 0.02)]
        h = make_harness({"gate": sensor(GATE, cooldown_seconds=0)}, {GATE: results})
        h.scheduler.camera_status("yard", True)
        h.publisher.calls.clear()

        for _ in results:
            h.scheduler.on_change("yard", new_frame(), 5.0)
            await h.scheduler.drain()

        assert h.publisher.calls == [
            ("attributes", "gate", ANY),
            ("state", "gate", True),
            ("attributes", "gate", ANY),
            ("attributes", "gate", ANY),
            ("attributes", "gate", ANY),
            ("state", "gate", False),
            ("attributes", "gate", ANY),
        ]

    run(scenario())


def test_malformed_response_publishes_off_even_when_already_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="djev_sensors")

    async def scenario() -> None:
        malformed = InvalidModelResponse("answers.result.noul was not numeric: 'yes'")
        h = make_harness(
            {"gate": sensor(GATE, true_threshold=0.9, cooldown_seconds=0)},
            {GATE: [judgment(0.09), malformed]},
        )
        h.scheduler.camera_status("yard", True)
        h.scheduler.on_change("yard", new_frame(), 5.0)
        await h.scheduler.drain()
        assert h.publisher.values("state", "gate") == [False]
        h.publisher.calls.clear()

        h.scheduler.on_change("yard", new_frame(), 3.0)
        await h.scheduler.drain()

        assert h.publisher.calls == [
            ("state", "gate", False),
            (
                "attributes",
                "gate",
                {
                    "parse_error": True,
                    "true_threshold": 0.9,
                    "change_pct": 3.0,
                    "camera": "yard",
                    "model": "djev",
                    "evaluated_at": EVALUATED_AT,
                    "trigger": "change",
                },
            ),
        ]

    run(scenario())
    assert logged(caplog, "inference.invalid_response") == [
        {"event": "inference.invalid_response", "sensor": "gate", "camera": "yard"}
    ]


def test_malformed_response_turns_the_sensor_off_until_a_confident_yes() -> None:
    async def scenario() -> None:
        h = make_harness(
            {"gate": sensor(GATE, cooldown_seconds=0)},
            {GATE: [judgment(0.91), InvalidModelResponse("bad"), judgment(0.91)]},
        )
        h.scheduler.camera_status("yard", True)

        for _ in range(3):
            h.scheduler.on_change("yard", new_frame(), 5.0)
            await h.scheduler.drain()

        assert h.publisher.values("state", "gate") == [True, False, True]

    run(scenario())


def test_malformed_response_restores_model_availability() -> None:
    async def scenario() -> None:
        h = make_harness(
            {"gate": sensor(GATE, cooldown_seconds=0)},
            {GATE: [ModelError("LunaRoute timed out"), InvalidModelResponse("bad")]},
        )
        h.scheduler.camera_status("yard", True)
        h.publisher.calls.clear()

        for _ in range(2):
            h.scheduler.on_change("yard", new_frame(), 5.0)
            await h.scheduler.drain()

        assert h.publisher.calls == [
            ("availability", "gate", False),
            ("availability", "gate", True),
            ("state", "gate", False),
            ("attributes", "gate", ANY),
        ]

    run(scenario())


def test_model_failure_marks_the_sensor_offline_and_keeps_its_state() -> None:
    async def scenario() -> None:
        h = make_harness(
            {"gate": sensor(GATE, cooldown_seconds=0)},
            {GATE: [judgment(0.91), ModelError("HTTP 503"), judgment(0.95)]},
        )
        h.scheduler.camera_status("yard", True)
        h.publisher.calls.clear()

        for _ in range(3):
            h.scheduler.on_change("yard", new_frame(), 5.0)
            await h.scheduler.drain()

        # The failure publishes no attributes, and the kept ON state means the
        # recovering result publishes no state either.
        assert h.publisher.calls == [
            ("attributes", "gate", ANY),
            ("state", "gate", True),
            ("availability", "gate", False),
            ("availability", "gate", True),
            ("attributes", "gate", ANY),
        ]

    run(scenario())


def test_model_failure_logs_the_error_class_and_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="djev_sensors")

    async def scenario() -> None:
        h = make_harness({"gate": sensor(GATE)}, {GATE: [ModelError("HTTP 503")]})
        h.scheduler.camera_status("yard", True)

        h.scheduler.on_change("yard", new_frame(), 5.0)
        await h.scheduler.drain()

    run(scenario())
    assert logged(caplog, "inference.failed") == [
        {
            "event": "inference.failed",
            "sensor": "gate",
            "camera": "yard",
            "error": "ModelError",
            "message": "HTTP 503",
        }
    ]


def test_unexpected_model_exception_is_a_model_failure_logged_at_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="djev_sensors")

    async def scenario() -> None:
        h = make_harness(
            {"gate": sensor(GATE, cooldown_seconds=0)},
            {GATE: [RuntimeError("reset by 10.0.0.5"), judgment(0.91)]},
        )
        h.scheduler.camera_status("yard", True)
        h.publisher.calls.clear()

        h.scheduler.on_change("yard", new_frame(), 5.0)
        await h.scheduler.drain()
        assert h.publisher.calls == [("availability", "gate", False)]

        # The sensor is not stuck: its next evaluation runs and recovers it.
        h.scheduler.on_change("yard", new_frame(), 5.0)
        await h.scheduler.drain()
        assert h.publisher.calls[1:] == [
            ("availability", "gate", True),
            ("attributes", "gate", ANY),
            ("state", "gate", True),
        ]

    run(scenario())
    [record] = [
        record
        for record in caplog.records
        if record.name == "djev_sensors" and '"inference.failed"' in record.getMessage()
    ]
    assert record.levelno == logging.ERROR
    assert json.loads(record.getMessage()) == {
        "event": "inference.failed",
        "sensor": "gate",
        "camera": "yard",
        "error": "RuntimeError",
    }


# Availability


def test_availability_needs_both_the_camera_and_the_model() -> None:
    async def scenario() -> None:
        h = make_harness(
            {"gate": sensor(GATE, cooldown_seconds=0)},
            {GATE: [ModelError("LunaRoute unreachable"), judgment(0.91)]},
        )
        h.scheduler.camera_status("yard", True)
        h.scheduler.on_change("yard", new_frame(), 5.0)
        await h.scheduler.drain()

        # A recovered camera must not clear the outstanding model failure.
        h.scheduler.camera_status("yard", False)
        h.scheduler.camera_status("yard", True)
        h.scheduler.on_change("yard", new_frame(), 5.0)
        await h.scheduler.drain()

        assert h.publisher.calls == [
            ("availability", "gate", True),
            ("availability", "gate", False),
            ("availability", "gate", True),
            ("attributes", "gate", ANY),
            ("state", "gate", True),
        ]

    run(scenario())


def test_camera_status_sets_availability_for_each_sensor_on_that_camera() -> None:
    h = make_harness(
        {
            "gate": sensor(GATE),
            "car": sensor(CAR),
            "porch": sensor(PORCH, camera="street"),
        },
        {},
    )

    h.scheduler.camera_status("yard", True)
    h.scheduler.camera_status("yard", False)

    assert h.publisher.calls == [
        ("availability", "gate", True),
        ("availability", "car", True),
        ("availability", "gate", False),
        ("availability", "car", False),
    ]


# Which changes start a model request


def test_a_change_evaluates_the_sensors_whose_threshold_it_meets() -> None:
    async def scenario() -> None:
        h = make_harness(
            {
                "gate": sensor(GATE, change_threshold_pct=2.0),
                "car": sensor(CAR, change_threshold_pct=3.0),
                "porch": sensor(PORCH, change_threshold_pct=5.0),
            },
            {GATE: [judgment(0.91)], CAR: [judgment(0.91)]},
        )
        h.scheduler.camera_status("yard", True)

        h.scheduler.on_change("yard", new_frame(), 3.0)
        await h.scheduler.drain()

        assert sorted(h.model.calls) == sorted([GATE, CAR])

    run(scenario())


def test_a_change_leaves_sensors_on_other_cameras_alone() -> None:
    async def scenario() -> None:
        h = make_harness(
            {
                "gate": sensor(GATE, change_threshold_pct=1.0),
                "porch": sensor(PORCH, camera="street", change_threshold_pct=1.0),
            },
            {GATE: [judgment(0.91)], PORCH: [judgment(0.91)]},
        )
        h.scheduler.camera_status("yard", True)
        h.scheduler.camera_status("street", True)

        h.scheduler.on_change("yard", new_frame(), 50.0)
        await h.scheduler.drain()

        assert h.model.calls == [GATE]

    run(scenario())


def test_two_eligible_sensors_get_independent_requests_and_results() -> None:
    async def scenario() -> None:
        h = make_harness(
            {"gate": sensor(GATE), "car": sensor(CAR)},
            {GATE: [judgment(0.91)], CAR: [judgment(0.09)]},
        )
        frame = new_frame()
        h.scheduler.camera_status("yard", True)

        h.scheduler.on_change("yard", frame, 5.0)
        await h.scheduler.drain()

        assert sorted(h.model.calls) == sorted([GATE, CAR])
        assert [image is frame for image in h.model.images] == [True, True]
        assert h.publisher.values("state", "gate") == [True]
        assert h.publisher.values("state", "car") == [False]

    run(scenario())


def test_a_qualifying_change_during_the_cooldown_is_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="djev_sensors")

    async def scenario() -> None:
        h = make_harness(
            {"gate": sensor(GATE, cooldown_seconds=10)},
            {GATE: [judgment(0.91), judgment(0.91)]},
        )
        h.scheduler.camera_status("yard", True)

        h.scheduler.on_change("yard", new_frame(), 5.0)
        await h.scheduler.drain()
        assert h.model.calls == [GATE]

        h.clock.now = 9.9
        h.scheduler.on_change("yard", new_frame(), 5.0)
        await h.scheduler.drain()
        assert h.model.calls == [GATE]

        h.clock.now = 10.0
        h.scheduler.on_change("yard", new_frame(), 5.0)
        await h.scheduler.drain()
        assert h.model.calls == [GATE, GATE]

    run(scenario())
    skipped = logged(caplog, "sensor.cooldown_skipped")
    assert [(event["sensor"], event["reason"]) for event in skipped] == [
        ("gate", "cooldown")
    ]


def test_qualifying_changes_while_evaluations_are_pending_are_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="djev_sensors")

    async def scenario() -> None:
        held = Held(judgment(0.91))
        h = make_harness(
            {
                "gate": sensor(GATE, cooldown_seconds=0),
                "car": sensor(CAR, cooldown_seconds=0),
            },
            {GATE: [held], CAR: [judgment(0.09)]},
            max_concurrent_requests=1,
        )
        h.scheduler.camera_status("yard", True)
        h.scheduler.on_change("yard", new_frame(), 5.0)
        await settle()
        # The gate request holds the only slot; the car evaluation waits for it.
        assert h.model.calls == [GATE]

        h.scheduler.on_change("yard", new_frame(), 5.0)
        await settle()
        held.release.set()
        await h.scheduler.drain()

        assert h.model.calls == [GATE, CAR]

    run(scenario())
    skipped = logged(caplog, "sensor.cooldown_skipped")
    assert [(event["sensor"], event["reason"]) for event in skipped] == [
        ("gate", "in_flight"),
        ("car", "in_flight"),
    ]


@pytest.mark.parametrize(
    ("slots", "started_while_gate_runs"), [(1, [GATE]), (2, [GATE, CAR])]
)
def test_requests_beyond_the_slot_limit_wait_for_a_free_slot(
    slots: int, started_while_gate_runs: list[str]
) -> None:
    async def scenario() -> None:
        held_gate = Held(judgment(0.91))
        held_car = Held(judgment(0.09))
        h = make_harness(
            {"gate": sensor(GATE), "car": sensor(CAR)},
            {GATE: [held_gate], CAR: [held_car]},
            max_concurrent_requests=slots,
        )
        h.scheduler.camera_status("yard", True)

        h.scheduler.on_change("yard", new_frame(), 5.0)
        await settle()
        assert h.model.calls == started_while_gate_runs

        held_gate.release.set()
        await settle()
        assert h.model.calls == [GATE, CAR]
        held_car.release.set()
        await h.scheduler.drain()

    run(scenario())


def test_cooldown_starts_when_the_request_gets_a_slot() -> None:
    async def scenario() -> None:
        held_gate = Held(judgment(0.91))
        held_car = Held(judgment(0.09))
        h = make_harness(
            {
                "gate": sensor(GATE, cooldown_seconds=10),
                "car": sensor(CAR, cooldown_seconds=10),
            },
            {GATE: [held_gate, judgment(0.91)], CAR: [held_car, judgment(0.09)]},
            max_concurrent_requests=1,
        )
        h.scheduler.camera_status("yard", True)

        # Both are scheduled at t=0; car gets the slot at t=6 and finishes at t=8.
        h.scheduler.on_change("yard", new_frame(), 5.0)
        await settle()
        h.clock.now = 6.0
        held_gate.release.set()
        await settle()
        h.clock.now = 8.0
        held_car.release.set()
        await h.scheduler.drain()
        assert h.model.calls == [GATE, CAR]

        # Counting from scheduling (t=0) would let car run at t=12.
        h.clock.now = 12.0
        h.scheduler.on_change("yard", new_frame(), 5.0)
        await h.scheduler.drain()
        assert h.model.calls == [GATE, CAR, GATE]

        # Counting from completion (t=8) would still refuse car at t=16.
        h.clock.now = 16.0
        h.scheduler.on_change("yard", new_frame(), 5.0)
        await h.scheduler.drain()
        assert h.model.calls == [GATE, CAR, GATE, CAR]

    run(scenario())


# Camera generations


@pytest.mark.parametrize(
    "outcome",
    [judgment(0.91), InvalidModelResponse("bad"), ModelError("LunaRoute timed out")],
    ids=["result", "malformed", "failure"],
)
def test_outcome_from_before_a_camera_disconnect_is_discarded(
    outcome: Outcome, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="djev_sensors")

    async def scenario() -> None:
        held = Held(outcome)
        h = make_harness({"gate": sensor(GATE)}, {GATE: [held, judgment(0.09)]})
        h.scheduler.camera_status("yard", True)
        h.scheduler.on_change("yard", new_frame(), 5.0)
        await settle()
        assert h.model.calls == [GATE]
        h.scheduler.camera_status("yard", False)
        h.scheduler.camera_status("yard", True)
        h.publisher.calls.clear()

        held.release.set()
        await h.scheduler.drain()
        assert h.publisher.calls == []

        # The discarded evaluation still frees the sensor for the next change.
        h.clock.now = 10.0
        h.scheduler.on_change("yard", new_frame(), 5.0)
        await h.scheduler.drain()
        assert h.publisher.calls == [
            ("attributes", "gate", ANY),
            ("state", "gate", False),
        ]

    run(scenario())
    discarded = logged(caplog, "inference.discarded")
    assert [event["sensor"] for event in discarded] == ["gate"]


def test_evaluation_waiting_for_a_slot_skips_the_model_after_a_disconnect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="djev_sensors")

    async def scenario() -> None:
        held = Held(judgment(0.91))
        h = make_harness(
            {"porch": sensor(PORCH, camera="street"), "gate": sensor(GATE)},
            {PORCH: [held], GATE: [judgment(0.91)]},
            max_concurrent_requests=1,
        )
        h.scheduler.camera_status("street", True)
        h.scheduler.camera_status("yard", True)
        h.scheduler.on_change("street", new_frame(), 5.0)
        await settle()
        h.scheduler.on_change("yard", new_frame(), 5.0)
        await settle()
        h.scheduler.camera_status("yard", False)

        held.release.set()
        await h.scheduler.drain()
        assert h.model.calls == [PORCH]

        # The skipped evaluation sent no request, so it started no cooldown.
        h.scheduler.camera_status("yard", True)
        h.scheduler.on_change("yard", new_frame(), 5.0)
        await h.scheduler.drain()
        assert h.model.calls == [PORCH, GATE]

    run(scenario())
    discarded = logged(caplog, "inference.discarded")
    assert [event["sensor"] for event in discarded] == ["gate"]


# Shutdown


def test_drain_timeout_cancels_evaluations_and_publishes_nothing() -> None:
    async def scenario() -> None:
        held = Held(judgment(0.91))
        h = make_harness(
            {"gate": sensor(GATE), "car": sensor(CAR)},
            {GATE: [held, judgment(0.91)], CAR: [judgment(0.09)]},
            max_concurrent_requests=1,
        )
        h.scheduler.camera_status("yard", True)
        h.scheduler.on_change("yard", new_frame(), 5.0)
        await settle()
        h.publisher.calls.clear()

        # Gate is inside its model call; car is still waiting for the slot.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(h.scheduler.drain(), timeout=0.05)

        assert h.model.cancelled == [GATE]
        assert h.model.calls == [GATE]
        assert h.publisher.calls == []

        # Both in-flight bits are clear: past gate's cooldown, both run again.
        h.clock.now = 10.0
        h.scheduler.on_change("yard", new_frame(), 5.0)
        await h.scheduler.drain()
        assert sorted(h.model.calls) == sorted([GATE, GATE, CAR])

    run(scenario())


def test_drain_also_waits_for_evaluations_started_while_draining() -> None:
    async def scenario() -> None:
        held_gate = Held(judgment(0.91))
        held_porch = Held(judgment(0.91))
        h = make_harness(
            {"gate": sensor(GATE), "porch": sensor(PORCH, camera="street")},
            {GATE: [held_gate], PORCH: [held_porch]},
        )
        h.scheduler.camera_status("yard", True)
        h.scheduler.camera_status("street", True)
        h.scheduler.on_change("yard", new_frame(), 5.0)
        await settle()

        drain = asyncio.create_task(h.scheduler.drain())
        # Let drain start waiting on gate's evaluation alone before porch's begins.
        await settle()
        h.scheduler.on_change("street", new_frame(), 5.0)
        await settle()
        held_gate.release.set()
        await settle()
        assert not drain.done()

        held_porch.release.set()
        await drain
        assert h.publisher.values("state", "porch") == [True]

    run(scenario())


# Structured events


def test_an_evaluation_logs_named_events_without_the_prompt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="djev_sensors")
    wrapper = "Judge only what the frame shows."

    async def scenario() -> None:
        h = make_harness(
            {"gate": sensor(GATE)},
            {f"{wrapper}\n\n{GATE}": [judgment(0.91)]},
            prompt_wrapper=wrapper,
        )
        h.scheduler.camera_status("yard", True)

        h.scheduler.on_change("yard", new_frame(), 4.7134)
        await h.scheduler.drain()

    run(scenario())
    messages = [r.getMessage() for r in caplog.records if r.name == "djev_sensors"]
    assert [json.loads(message)["event"] for message in messages] == [
        "sensor.availability_changed",
        "sensor.triggered",
        "inference.started",
        "inference.completed",
        "sensor.state_changed",
    ]
    assert logged(caplog, "inference.completed") == [
        {
            "event": "inference.completed",
            "sensor": "gate",
            "camera": "yard",
            "true_probability": 0.91,
            "change_pct": 4.71,
            "latency_ms": 241,
        }
    ]
    assert not [m for m in messages if wrapper in m or GATE in m]


# Rechecks: a slow trickle of looks at the newest frame after movement


def same_frames(
    sent: Sequence[NDArray[np.uint8]], expected: Sequence[NDArray[np.uint8]]
) -> bool:
    """True when the model received exactly these frame objects, in order."""
    return len(sent) == len(expected) and all(
        a is b for a, b in zip(sent, expected, strict=True)
    )


async def samples(h: Harness, steps: Sequence[tuple[float, float]]) -> None:
    """Feed one camera sample per (time, change) step, letting evaluations run."""
    for now, change in steps:
        h.clock.now = now
        h.scheduler.on_change("yard", new_frame(), change)
        await settle()


def test_after_movement_the_newest_frame_is_rechecked_on_a_slow_trickle() -> None:
    frames = [new_frame() for _ in range(4)]
    h = make_harness(
        {"couch": sensor(PORCH, recheck_count=3, recheck_interval_seconds=10)},
        {PORCH: [judgment(0.5)] * 4},
    )

    async def scenario() -> None:
        h.scheduler.on_change("yard", frames[0], 3.0)  # movement: the first look
        await settle()
        for now, frame in [
            (5.0, new_frame()),  # still inside the interval: no look
            (10.0, frames[1]),
            (15.0, new_frame()),
            (20.0, frames[2]),
            (30.0, frames[3]),
            (40.0, new_frame()),  # three rechecks done: the trickle stops
        ]:
            h.clock.now = now
            h.scheduler.on_change("yard", frame, 0.0)
            await settle()

    run(scenario())
    assert same_frames(h.model.images, frames)


def test_new_movement_restarts_the_trickle() -> None:
    h = make_harness(
        {"couch": sensor(PORCH, recheck_count=2, recheck_interval_seconds=10)},
        {PORCH: [judgment(0.5)] * 4},
    )
    # Movement at 15 s arrives inside the cooldown, so it earns no look of its
    # own, but it restores both rechecks: looks at 0, 10, 20, and 30 s.
    run(samples(h, [(0, 3.0), (10, 0.0), (15, 3.0), (20, 0.0), (30, 0.0), (40, 0.0)]))
    assert len(h.model.calls) == 4


def test_rechecks_never_come_sooner_than_the_cooldown() -> None:
    h = make_harness(
        {
            "couch": sensor(
                PORCH, cooldown_seconds=10, recheck_count=1, recheck_interval_seconds=2
            )
        },
        {PORCH: [judgment(0.5)] * 2},
    )
    run(samples(h, [(0, 3.0), (2, 0.0), (9.9, 0.0)]))
    assert len(h.model.calls) == 1
    run(samples(h, [(10, 0.0)]))
    assert len(h.model.calls) == 2


def test_a_recheck_count_of_zero_turns_rechecks_off() -> None:
    h = make_harness(
        {"couch": sensor(PORCH, recheck_count=0)}, {PORCH: [judgment(0.5)]}
    )
    run(samples(h, [(0, 3.0), (10, 0.0), (20, 0.0), (30, 0.0)]))
    assert len(h.model.calls) == 1


def test_a_camera_outage_cancels_the_rechecks_it_owed() -> None:
    h = make_harness(
        {"couch": sensor(PORCH, recheck_count=3)}, {PORCH: [judgment(0.5)]}
    )

    async def scenario() -> None:
        await samples(h, [(0, 3.0)])
        h.scheduler.camera_status("yard", False)
        h.scheduler.camera_status("yard", True)
        await samples(h, [(10, 0.0), (20, 0.0)])

    run(scenario())
    assert len(h.model.calls) == 1


def test_a_recheck_can_settle_an_uncertain_first_look() -> None:
    h = make_harness(
        {"couch": sensor(PORCH, recheck_count=1)},
        {PORCH: [judgment(0.66), judgment(0.95)]},
    )
    run(samples(h, [(0, 3.0), (10, 0.0)]))
    assert h.publisher.values("state", "couch") == [True]
    triggers = [
        attributes["trigger"]  # type: ignore[index]
        for attributes in h.publisher.values("attributes", "couch")
    ]
    assert triggers == ["change", "recheck"]


def test_each_recheck_is_logged_with_the_rechecks_left(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="djev_sensors")
    h = make_harness(
        {"couch": sensor(PORCH, recheck_count=2)}, {PORCH: [judgment(0.5)] * 3}
    )
    run(samples(h, [(0, 3.0), (10, 0.4), (20, 0.0)]))
    assert logged(caplog, "sensor.rechecking") == [
        {
            "event": "sensor.rechecking",
            "sensor": "couch",
            "camera": "yard",
            "change_pct": 0.4,
            "remaining": 1,
        },
        {
            "event": "sensor.rechecking",
            "sensor": "couch",
            "camera": "yard",
            "change_pct": 0.0,
            "remaining": 0,
        },
    ]
