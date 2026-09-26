# ABOUTME: The service loop: scores each camera sample against the one before and passes
# ABOUTME: the score to the scheduler; also starts and stops the hub, MQTT, and model.
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from djev_sensors.camera import CameraStreamHub, FrameSample
from djev_sensors.config import AppConfig
from djev_sensors.detectors.base import ChangeDetector
from djev_sensors.events import log_event
from djev_sensors.models.base import ModelClient
from djev_sensors.scheduler import SensorScheduler

# Shutdown bounds. With the publisher's own stop, they fit Docker's default
# 10 s stop grace period.
HUB_STOP_TIMEOUT_SECONDS = 3.0
DRAIN_TIMEOUT_SECONDS = 3.0

HUB_THREAD_NAME = "camera-hub"

# How often the event loop checks for the first MQTT connect and for stop.
_POLL_SECONDS = 0.1


class ServicePublisher(Protocol):
    """The publisher as the app drives it; MqttPublisher implements it."""

    def start(self) -> None: ...

    def wait_for_first_connect(self, timeout: float | None = None) -> bool: ...

    def stop(self) -> None: ...


@dataclass
class CameraRuntime:
    """What the app tracks for one camera ID (spec section 29)."""

    # The lowest change threshold among the camera's sensors, below which a
    # score is not worth a frame.change line. None when no sensor uses it.
    lowest_change_threshold_pct: float | None
    # The previous sample's detection frame; None until the first sample after
    # start or after the camera went offline.
    baseline: NDArray[np.uint8] | None = None


class Application:
    """Runs the camera-to-sensor pipeline (spec section 30) and its lifecycle.

    The camera hub runs on its own daemon thread. Its worker threads hand
    each sample and status change to the event loop and wait until it has
    been handled, so every camera's samples and status changes are handled
    in order, and all pipeline state lives on the loop.
    """

    def __init__(
        self,
        config: AppConfig,
        camera_hub: CameraStreamHub,
        detector: ChangeDetector,
        scheduler: SensorScheduler,
        publisher: ServicePublisher,
        model: ModelClient,
    ) -> None:
        self._config = config
        self._camera_hub = camera_hub
        self._detector = detector
        self._scheduler = scheduler
        self._publisher = publisher
        self._model = model
        self._cameras = {
            camera_id: CameraRuntime(
                min(
                    (
                        sensor.change_threshold_pct
                        for sensor in config.sensors.values()
                        if sensor.camera == camera_id
                    ),
                    default=None,
                )
            )
            for camera_id in config.cameras
        }

    async def run(self, stop_event: threading.Event) -> None:
        """Run until `stop_event` is set, then shut down in bounded time.

        Call on the scheduler's event loop. `stop_event` may be set from any
        thread; the hub also sets it when an app callback raises. The camera
        hub starts once the publisher's first connect has published discovery
        (spec section 27), or never, if stop comes first. Shutdown waits up to
        HUB_STOP_TIMEOUT_SECONDS for the hub, then up to DRAIN_TIMEOUT_SECONDS
        for running evaluations, cancelling the rest, then stops the
        publisher, which marks the service offline, and closes the model
        client. If the hub raised, its exception is re-raised after that
        cleanup.
        """
        loop = asyncio.get_running_loop()
        hub_outcome: asyncio.Future[None] = loop.create_future()
        # Not asyncio.to_thread: at exit, asyncio.run waits up to 300 s for the
        # default executor's threads, and a stalled read can outlast the bound.
        hub_thread = threading.Thread(
            target=self._run_hub,
            args=(loop, stop_event, hub_outcome),
            name=HUB_THREAD_NAME,
            daemon=True,  # nor may it keep the process alive
        )
        try:
            self._publisher.start()
            log_event(
                "service.started",
                cameras=list(self._config.cameras),
                sensors=list(self._config.sensors),
            )
            # A state computed before MQTT connects could only be dropped.
            await _wait_until(
                lambda: (
                    stop_event.is_set()
                    or self._publisher.wait_for_first_connect(timeout=0)
                )
            )
            if not stop_event.is_set():
                hub_thread.start()
                await _wait_until(stop_event.is_set)
        finally:
            stop_event.set()
            if hub_thread.ident is not None:  # the thread was started
                await asyncio.wait({hub_outcome}, timeout=HUB_STOP_TIMEOUT_SECONDS)
            await self._drain()
            try:
                await asyncio.to_thread(self._publisher.stop)
            finally:
                await self._model.aclose()
        hub_failure = hub_outcome.exception() if hub_outcome.done() else None
        if hub_failure is None:
            log_event("service.stopped")
            return
        log_event(
            "service.stopped", level=logging.ERROR, error=type(hub_failure).__name__
        )
        raise hub_failure

    async def _drain(self) -> None:
        """Wait for running evaluations, up to DRAIN_TIMEOUT_SECONDS."""
        try:
            await asyncio.wait_for(self._scheduler.drain(), DRAIN_TIMEOUT_SECONDS)
        except TimeoutError:
            pass  # the timeout cancelled drain, and with it every evaluation left

    def _run_hub(
        self,
        loop: asyncio.AbstractEventLoop,
        stop_event: threading.Event,
        outcome: asyncio.Future[None],
    ) -> None:
        """Run the camera hub on this thread, then report how it ended to the loop."""

        # The hub calls these on its worker threads. Once stop is requested
        # they start no new work, and the loop, which keeps running until the
        # hub returns or its bound expires, answers any call already made.
        def on_sample(sample: FrameSample) -> None:
            if not stop_event.is_set():
                asyncio.run_coroutine_threadsafe(
                    self._handle_sample(sample), loop
                ).result()

        def on_status(camera_id: str, connected: bool) -> None:
            if not stop_event.is_set():
                asyncio.run_coroutine_threadsafe(
                    self._handle_status(camera_id, connected), loop
                ).result()

        failure: BaseException | None = None
        try:
            self._camera_hub.run(on_sample, on_status, stop_event)
        except BaseException as exc:  # a callback bug, re-raised once the hub stopped
            failure = exc
        try:
            loop.call_soon_threadsafe(_settle, outcome, failure)
        except RuntimeError:
            pass  # the loop has closed: shutdown stopped waiting for the hub

    async def _handle_sample(self, sample: FrameSample) -> None:
        """Score a sample against its camera's previous one and pass the score on."""
        camera = self._cameras[sample.camera_id]
        detection_frame = self._detector.prepare(sample.image)
        previous, camera.baseline = camera.baseline, detection_frame
        if previous is None or previous.shape != detection_frame.shape:
            # Nothing to compare with: the first sample after start or an
            # outage, or a new resolution. It only becomes the baseline.
            return
        changed_pct = self._detector.compare(previous, detection_frame).changed_pct
        threshold = camera.lowest_change_threshold_pct
        if threshold is not None and changed_pct >= threshold:
            log_event(
                "frame.change",
                camera=sample.camera_id,
                change_pct=round(changed_pct, 2),
            )
        # The source frame, never the reduced detection frame, goes on to Djev.
        self._scheduler.on_change(sample.camera_id, sample.image, changed_pct)

    async def _handle_status(self, camera_id: str, connected: bool) -> None:
        """Record a camera connecting or disconnecting."""
        if not connected:
            self._cameras[camera_id].baseline = None
        self._scheduler.camera_status(camera_id, connected)


def _settle(outcome: asyncio.Future[None], failure: BaseException | None) -> None:
    """Complete `outcome` with how the hub ended; runs on the event loop."""
    if failure is None:
        outcome.set_result(None)
    else:
        outcome.set_exception(failure)


async def _wait_until(condition: Callable[[], bool]) -> None:
    """Return once `condition()` holds; it reads state other threads change."""
    while not condition():
        await asyncio.sleep(_POLL_SECONDS)
