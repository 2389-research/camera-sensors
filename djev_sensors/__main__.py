# ABOUTME: Command line, python -m djev_sensors --config config.yaml: loads the config,
# ABOUTME: logs one JSON event per line to stdout, and runs until SIGINT or SIGTERM.
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from djev_sensors.app import Application
from djev_sensors.camera import CameraStreamHub
from djev_sensors.config import AppConfig, ConfigError, load_config
from djev_sensors.detectors.frame_difference import FrameDifferenceDetector
from djev_sensors.models.lunaroute_djev import LunaRouteDjevClient
from djev_sensors.mqtt.client import MqttPublisher
from djev_sensors.scheduler import SensorScheduler

EXIT_UNSIGNALLED_STOP = 1
EXIT_CONFIG_ERROR = 2


def main(argv: Sequence[str] | None = None) -> int:
    """Run the service and return the process exit status.

    Returns 0 after SIGINT or SIGTERM stops the service, 2 when the
    configuration is invalid, and 1 when anything else stopped it, such as
    the camera hub after a callback bug whose re-raise missed the shutdown
    bound; the hub logged that traceback when the bug happened. An exception
    from the running service, such as that re-raise when it comes in time,
    propagates: Python prints its traceback and exits with status 1.
    """
    parser = argparse.ArgumentParser(
        prog="python -m djev_sensors",
        description="Turn RTSP camera streams into Home Assistant binary sensors.",
    )
    parser.add_argument(
        "--config", type=Path, required=True, help="the YAML configuration file"
    )
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, os.environ)
    except ConfigError as exc:
        print(f"{parser.prog}: error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    _configure_logging()
    if not asyncio.run(_serve(config)):
        print(
            f"{parser.prog}: error: the service stopped without SIGINT or SIGTERM",
            file=sys.stderr,
        )
        return EXIT_UNSIGNALLED_STOP
    return 0


def _configure_logging() -> None:
    """Write each log event, one JSON object, as a line on stdout (spec section 33)."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    # Their INFO lines narrate every model request.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


async def _serve(config: AppConfig) -> bool:
    """Build the service from `config` and run it until it stops.

    Returns whether SIGINT or SIGTERM caused the stop. A signal that arrives
    after something else, such as the camera hub, requested the stop does not.
    """
    stop_event = threading.Event()
    stopped_by_signal = False

    def stop_on_signal() -> None:
        nonlocal stopped_by_signal
        if not stop_event.is_set():
            stopped_by_signal = True
        stop_event.set()

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop_on_signal)
    model_config = config.system.model
    model = LunaRouteDjevClient(
        model_config.api_key, model_config.model, model_config.timeout_seconds
    )
    publisher = MqttPublisher(config)
    detection = config.system.change_detection
    detector = FrameDifferenceDetector(
        detection.width, detection.pixel_delta_threshold, detection.blur
    )
    app = Application(
        config,
        CameraStreamHub(config.cameras),
        detector,
        SensorScheduler(config, model, publisher),
        publisher,
        model,
    )
    await app.run(stop_event)
    return stopped_by_signal


if __name__ == "__main__":
    sys.exit(main())
