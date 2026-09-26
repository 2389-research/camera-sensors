# ABOUTME: Starts one Djev evaluation per eligible sensor for each camera change
# ABOUTME: and turns each outcome into state, attribute, and availability publications.
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from djev_sensors.config import AppConfig
from djev_sensors.events import log_event
from djev_sensors.models.base import (
    BinaryJudgment,
    InvalidModelResponse,
    ModelClient,
    ModelError,
)
from djev_sensors.state import SensorRuntime, resolve_state


class SensorPublisher(Protocol):
    """Receives the scheduler's output; the MQTT publisher implements it.

    Implementations log and drop their own failures, so these calls never raise.
    """

    def publish_state(self, sensor_id: str, state: bool) -> None: ...

    def publish_attributes(
        self, sensor_id: str, attributes: Mapping[str, object]
    ) -> None: ...

    def publish_availability(self, sensor_id: str, online: bool) -> None: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SensorScheduler:
    """Decides which sensors each camera change evaluates, and applies the results.

    Lives on one asyncio event loop: the app calls `on_change` and
    `camera_status` on that loop, and each evaluation runs as its own task.
    A semaphore caps model requests across all sensors, each sensor has at
    most one evaluation pending, and no frame is ever queued for later. After
    movement, a sensor takes a slow trickle of rechecks from later samples.
    """

    def __init__(
        self,
        config: AppConfig,
        model: ModelClient,
        publisher: SensorPublisher,
        *,
        clock: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._model = model
        self._publisher = publisher
        self._clock = clock
        self._utc_now = utc_now
        self._model_name = config.system.model.model
        self._prompt_wrapper = config.system.model.prompt_wrapper
        self._slots = asyncio.Semaphore(config.system.inference.max_concurrent_requests)
        self._sensors = [
            SensorRuntime(sensor_id, sensor_config)
            for sensor_id, sensor_config in config.sensors.items()
        ]
        # Bumped whenever a camera goes offline, so an evaluation can tell that
        # its frame came from an earlier connection.
        self._camera_generations = dict.fromkeys(config.cameras, 0)
        self._tasks: set[asyncio.Task[None]] = set()

    def on_change(
        self, camera_id: str, frame: NDArray[np.uint8], changed_pct: float
    ) -> None:
        """Start an evaluation for each sensor on `camera_id` this sample calls for.

        A change that meets a sensor's threshold triggers a look when the sensor
        has no evaluation pending and its cooldown has expired. It also owes the
        sensor `recheck_count` more looks at the newest frame, which later
        samples take up one at a time as each comes due. Never blocks.
        """
        now = self._clock()
        generation = self._camera_generations[camera_id]
        change_pct = round(changed_pct, 2)
        for sensor in self._sensors:
            if sensor.config.camera != camera_id:
                continue
            if changed_pct >= sensor.config.change_threshold_pct:
                # Movement, looked at now or not: the trickle restarts after it.
                sensor.rechecks_remaining = sensor.config.recheck_count
                if sensor.inference_in_flight or not sensor.cooldown_expired(now):
                    log_event(
                        "sensor.cooldown_skipped",
                        sensor=sensor.sensor_id,
                        camera=camera_id,
                        change_pct=change_pct,
                        reason="in_flight"
                        if sensor.inference_in_flight
                        else "cooldown",
                    )
                    continue
                log_event(
                    "sensor.triggered",
                    sensor=sensor.sensor_id,
                    camera=camera_id,
                    change_pct=change_pct,
                )
                self._start(sensor, frame, change_pct, generation, "change")
            elif sensor.recheck_due(now):
                sensor.rechecks_remaining -= 1
                log_event(
                    "sensor.rechecking",
                    sensor=sensor.sensor_id,
                    camera=camera_id,
                    change_pct=change_pct,
                    remaining=sensor.rechecks_remaining,
                )
                self._start(sensor, frame, change_pct, generation, "recheck")

    def _start(
        self,
        sensor: SensorRuntime,
        frame: NDArray[np.uint8],
        change_pct: float,
        generation: int,
        trigger: str,
    ) -> None:
        """Run one evaluation as its own task, tracked until it finishes."""
        # Set now, not when the request starts, so a sensor still waiting for a
        # slot also refuses a second evaluation.
        sensor.inference_in_flight = True
        task = asyncio.create_task(
            self._evaluate(sensor, frame, change_pct, generation, trigger)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def camera_status(self, camera_id: str, online: bool) -> None:
        """Record a camera connecting or disconnecting for every sensor it feeds."""
        if not online:
            self._camera_generations[camera_id] += 1
        for sensor in self._sensors:
            if sensor.config.camera == camera_id:
                if not online:
                    # Rechecks follow movement; after an outage, only new movement
                    # counts.
                    sensor.rechecks_remaining = 0
                self._set_availability(sensor, camera_available=online)

    async def drain(self) -> None:
        """Wait until no evaluation is running, including ones started meanwhile.

        Cancelling drain, as a timeout does, cancels the running evaluations;
        a cancelled evaluation publishes nothing.
        """
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _evaluate(
        self,
        sensor: SensorRuntime,
        frame: NDArray[np.uint8],
        change_pct: float,
        generation: int,
        trigger: str,
    ) -> None:
        camera_id = sensor.config.camera
        try:
            async with self._slots:
                if self._camera_generations[camera_id] != generation:
                    # Skipped before any request, so no cooldown starts.
                    log_event(
                        "inference.discarded", sensor=sensor.sensor_id, camera=camera_id
                    )
                    return
                sensor.cooldown_started_at = self._clock()
                outcome = await self._request(sensor, frame, change_pct)
            if self._camera_generations[camera_id] != generation:
                log_event(
                    "inference.discarded", sensor=sensor.sensor_id, camera=camera_id
                )
                return
            if isinstance(outcome, BinaryJudgment):
                self._apply_judgment(sensor, outcome, change_pct, trigger)
            elif isinstance(outcome, InvalidModelResponse):
                self._apply_invalid_response(sensor, change_pct, trigger)
            else:
                self._set_availability(sensor, model_available=False)
        finally:
            sensor.inference_in_flight = False

    async def _request(
        self, sensor: SensorRuntime, frame: NDArray[np.uint8], change_pct: float
    ) -> BinaryJudgment | Exception:
        """Ask the model once and log what came back.

        Returns the judgment, or the exception the model raised; only
        cancellation propagates.
        """
        sensor_id = sensor.sensor_id
        camera_id = sensor.config.camera
        log_event("inference.started", sensor=sensor_id, camera=camera_id)
        try:
            judgment = await self._model.evaluate_binary(
                frame, self._instructions(sensor)
            )
        except InvalidModelResponse as exc:
            # The message can quote the model's output, so it stays out of the log.
            log_event(
                "inference.invalid_response",
                level=logging.WARNING,
                sensor=sensor_id,
                camera=camera_id,
            )
            return exc
        except ModelError as exc:
            log_event(
                "inference.failed",
                level=logging.WARNING,
                sensor=sensor_id,
                camera=camera_id,
                error=type(exc).__name__,
                message=str(exc),
            )
            return exc
        except Exception as exc:
            # A bug or an unwrapped library error: treat it as a model failure,
            # but log only its class, since its message may hold anything.
            log_event(
                "inference.failed",
                level=logging.ERROR,
                sensor=sensor_id,
                camera=camera_id,
                error=type(exc).__name__,
            )
            return exc
        log_event(
            "inference.completed",
            sensor=sensor_id,
            camera=camera_id,
            true_probability=judgment.true_probability,
            change_pct=change_pct,
            latency_ms=judgment.latency_ms,
        )
        return judgment

    def _instructions(self, sensor: SensorRuntime) -> str:
        """The wrapper and the sensor prompt, one blank line apart (spec section 16)."""
        if not self._prompt_wrapper:
            return sensor.config.prompt
        return f"{self._prompt_wrapper}\n\n{sensor.config.prompt}"

    def _apply_judgment(
        self,
        sensor: SensorRuntime,
        judgment: BinaryJudgment,
        change_pct: float,
        trigger: str,
    ) -> None:
        self._set_availability(sensor, model_available=True)
        self._publisher.publish_attributes(
            sensor.sensor_id,
            {
                "true_probability": judgment.true_probability,
                "true_threshold": sensor.config.true_threshold,
                "change_pct": change_pct,
                "camera": sensor.config.camera,
                "model": self._model_name,
                "evaluated_at": self._evaluated_at(),
                "latency_ms": judgment.latency_ms,
                "sent_width": judgment.sent_width,
                "sent_height": judgment.sent_height,
                "parse_error": False,
                "trigger": trigger,
            },
        )
        state = resolve_state(
            sensor.state, judgment.true_probability, sensor.config.true_threshold
        )
        if state is not None and state != sensor.state:
            self._publish_state(sensor, state)

    def _apply_invalid_response(
        self, sensor: SensorRuntime, change_pct: float, trigger: str
    ) -> None:
        self._set_availability(sensor, model_available=True)
        # Spec sections 18 and 31: a malformed answer means OFF, published even
        # when the sensor is already OFF.
        self._publish_state(sensor, False)
        self._publisher.publish_attributes(
            sensor.sensor_id,
            {
                "parse_error": True,
                "true_threshold": sensor.config.true_threshold,
                "change_pct": change_pct,
                "camera": sensor.config.camera,
                "model": self._model_name,
                "evaluated_at": self._evaluated_at(),
                "trigger": trigger,
            },
        )

    def _publish_state(self, sensor: SensorRuntime, state: bool) -> None:
        if state != sensor.state:
            log_event(
                "sensor.state_changed",
                sensor=sensor.sensor_id,
                camera=sensor.config.camera,
                state="ON" if state else "OFF",
            )
        sensor.state = state
        self._publisher.publish_state(sensor.sensor_id, state)

    def _set_availability(
        self,
        sensor: SensorRuntime,
        *,
        camera_available: bool | None = None,
        model_available: bool | None = None,
    ) -> None:
        """Update either availability condition; publish the result if it changed."""
        was_available = sensor.available
        if camera_available is not None:
            sensor.camera_available = camera_available
        if model_available is not None:
            sensor.model_available = model_available
        if sensor.available == was_available:
            return
        log_event(
            "sensor.availability_changed",
            sensor=sensor.sensor_id,
            camera=sensor.config.camera,
            available=sensor.available,
            camera_available=sensor.camera_available,
            model_available=sensor.model_available,
        )
        self._publisher.publish_availability(sensor.sensor_id, sensor.available)

    def _evaluated_at(self) -> str:
        """UTC now in ISO 8601 with whole seconds and a Z suffix."""
        return self._utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
