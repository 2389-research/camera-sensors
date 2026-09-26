# ABOUTME: Integration tests for djev_sensors.mqtt.client with real paho and the stack's
# ABOUTME: Mosquitto: retain flags, payloads, reconnect replay, Last Will, and failures.
from __future__ import annotations

import json
import logging
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
import pytest
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from djev_sensors.config import AppConfig
from djev_sensors.mqtt.client import MqttPublisher
from djev_sensors.mqtt.discovery import (
    attributes_topic,
    availability_topic,
    discovery_payload,
    discovery_topic,
    service_availability_topic,
    state_topic,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = 15.0  # seconds to wait for any one expected message or log event
MODEL_ENV = {"LUNAROUTE_API_KEY": "test-key"}
ATTRIBUTES: dict[str, object] = {
    "true_probability": 0.93,
    "true_threshold": 0.8,
    "change_pct": 4.71,
    "camera": "backyard",
    "model": "djev",
    "evaluated_at": "2026-09-25T19:32:17Z",
    "latency_ms": 241,
    "sent_width": 768,
    "sent_height": 432,
    "parse_error": False,
}
NOT_CONNECTED = {"rc": 4, "error": "The client is not currently connected."}
# An MQTT 3.1.1 CONNACK refusing the connection with return code 5, "not authorized".
CONNACK_NOT_AUTHORIZED = bytes([0x20, 0x02, 0x00, 0x05])

# Runs a publisher until killed. argv[1] is the raw config and argv[2] its env, as JSON.
CHILD_PUBLISHER = """
import json
import sys
import time

from djev_sensors.config import AppConfig
from djev_sensors.mqtt.client import MqttPublisher

config = AppConfig.model_validate(
    json.loads(sys.argv[1]), context={"env": json.loads(sys.argv[2])}
)
MqttPublisher(config).start()
time.sleep(600)
"""


def raw_config(address: tuple[str, int], **mqtt_fields: object) -> dict[str, Any]:
    """Two sensors on one camera, under a client ID and prefixes unique to one test.

    The session's broker keeps every test's retained messages, so tests must
    not share topics.
    """
    run = uuid.uuid4().hex[:12]
    host, port = address
    return {
        "system": {
            "mqtt": {
                "host": host,
                "port": port,
                "client_id": f"djev-test-{run}",
                "discovery_prefix": f"ha-test-{run}",
                "topic_prefix": f"djev-test-{run}",
                **mqtt_fields,
            },
            "model": {
                "provider": "lunaroute",
                "model": "djev",
                "api_key_env": "LUNAROUTE_API_KEY",
            },
        },
        "cameras": {"backyard": {"rtsp": "rtsp://backyard.test/stream"}},
        "sensors": {
            "gate_open": {
                "name": "Gate Open",
                "camera": "backyard",
                "prompt": "Is the gate open?",
                "device_class": "opening",
            },
            "car_present": {
                "name": "Car Present",
                "camera": "backyard",
                "prompt": "Is a car in the driveway?",
            },
        },
    }


def validate(raw: dict[str, Any], **env: str) -> AppConfig:
    """Validate `raw` with MODEL_ENV and `env` as the environment."""
    return AppConfig.model_validate(raw, context={"env": {**MODEL_ENV, **env}})


def topic_filters(config: AppConfig) -> list[str]:
    """Filters matching every topic the publisher uses for `config`."""
    mqtt_config = config.system.mqtt
    return [f"{mqtt_config.discovery_prefix}/#", f"{mqtt_config.topic_prefix}/#"]


@dataclass(frozen=True)
class Received:
    topic: str
    payload: str
    qos: int
    retain: bool


def payloads(messages: Sequence[Received], topic: str) -> list[str]:
    return [message.payload for message in messages if message.topic == topic]


def decode(message: Received) -> object:
    """The payload, parsed for the JSON topics (discovery config and attributes)."""
    if message.topic.endswith(("/config", "/attributes")):
        return json.loads(message.payload)
    return message.payload


class Subscriber:
    """A test client that records the messages on its topic filters in arrival order.

    A message arrives at the lower of its publish QoS and `qos`, so the
    default of 2 shows each message's publish QoS. Entering connects and
    returns once the broker has acknowledged the subscription.
    """

    def __init__(
        self, address: tuple[str, int], filters: Sequence[str], *, qos: int = 2
    ) -> None:
        self._address = address
        client_id = f"djev-test-subscriber-{uuid.uuid4().hex[:12]}"
        self._marker_topic = f"djev-test-markers/{client_id}"
        self._subscriptions = [(topic, qos) for topic in (*filters, self._marker_topic)]
        self._changed = threading.Condition()
        self._messages: list[Received] = []
        self._subscribed = False
        self._client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=client_id)
        self._client.on_connect = self._on_connect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_message = self._on_message

    def __enter__(self) -> Subscriber:
        host, port = self._address
        self._client.connect(host, port)
        self._client.loop_start()
        try:
            with self._changed:
                if not self._changed.wait_for(lambda: self._subscribed, TIMEOUT):
                    pytest.fail(f"no SUBACK within {TIMEOUT:.0f} s")
        except BaseException:
            self._close()
            raise
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._close()

    def wait_for(
        self, what: str, condition: Callable[[list[Received]], bool]
    ) -> list[Received]:
        """Block until `condition` holds for the messages so far, then return them."""
        with self._changed:
            if not self._changed.wait_for(lambda: condition(self._messages), TIMEOUT):
                pytest.fail(
                    f"timed out after {TIMEOUT:.0f} s waiting for {what}; "
                    f"received {self._messages}"
                )
            return list(self._messages)

    def sync(self) -> list[Received]:
        """The messages that arrived before a marker this client publishes now.

        Relies on the broker sending a new subscription's retained messages
        before it handles this client's next packet, so that a message
        missing after sync was not retained. Callers also assert that the
        retained messages they expect did arrive, which checks that premise.
        """
        marker = uuid.uuid4().hex

        def is_marker(message: Received) -> bool:
            return message.topic == self._marker_topic and message.payload == marker

        self._client.publish(self._marker_topic, marker, qos=1)
        received = self.wait_for(
            "the sync marker", lambda messages: any(map(is_marker, messages))
        )
        index = next(i for i, message in enumerate(received) if is_marker(message))
        return received[:index]

    def _close(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()
        # Paho closes its internal sockets only when the client is garbage
        # collected. These bound methods make a reference cycle with this
        # object; dropping them lets the client go as soon as this does.
        self._client.on_connect = None
        self._client.on_subscribe = None
        self._client.on_message = None

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: object,
        flags: mqtt.ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        client.subscribe(self._subscriptions)

    def _on_subscribe(
        self,
        client: mqtt.Client,
        userdata: object,
        mid: int,
        reason_codes: list[ReasonCode],
        properties: Properties | None,
    ) -> None:
        with self._changed:
            self._subscribed = True
            self._changed.notify_all()

    def _on_message(
        self, client: mqtt.Client, userdata: object, message: mqtt.MQTTMessage
    ) -> None:
        received = Received(
            message.topic, message.payload.decode(), message.qos, message.retain
        )
        with self._changed:
            self._messages.append(received)
            self._changed.notify_all()


def take_over_client_id(address: tuple[str, int], client_id: str) -> None:
    """Connect once with another client's ID, so the broker drops that client.

    MQTT 3.1.1 [MQTT-3.1.4-2]. This client runs its network loop by hand, so
    it never reconnects to fight over the ID, and it leaves once connected.
    """
    connected = threading.Event()
    intruder = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=client_id)
    intruder.on_connect = lambda *args: connected.set()
    host, port = address
    intruder.connect(host, port)
    deadline = time.monotonic() + TIMEOUT
    while not connected.is_set():
        if time.monotonic() > deadline:
            pytest.fail(f"the takeover connection got no CONNACK in {TIMEOUT:.0f} s")
        intruder.loop(timeout=0.1)
    intruder.disconnect()


def logged(caplog: pytest.LogCaptureFixture) -> list[tuple[int, dict[str, Any]]]:
    """Each event logged on the djev_sensors logger as (level, fields), in order."""
    return [
        (record.levelno, json.loads(record.getMessage()))
        for record in caplog.records
        if record.name == "djev_sensors"
    ]


def events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [fields for _, fields in logged(caplog)]


def event_names(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [fields["event"] for fields in events(caplog)]


def wait_until(condition: Callable[[], bool], what: str) -> None:
    """Poll `condition`, which checks something paho's thread does, until it holds."""
    deadline = time.monotonic() + TIMEOUT
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {TIMEOUT:.0f} s waiting for {what}")
        time.sleep(0.05)


def closed_port() -> int:
    """A local TCP port with nothing listening on it."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def test_a_connect_announces_retained_config_and_availability_but_state_is_not_retained(
    mqtt_address: tuple[str, int],
) -> None:
    config = validate(raw_config(mqtt_address))
    publisher = MqttPublisher(config)
    # A camera status known before MQTT connects is announced on connect.
    publisher.publish_availability("car_present", True)
    with Subscriber(mqtt_address, topic_filters(config)) as live:
        publisher.start()
        try:
            live.wait_for(
                "the connect announcement", lambda messages: len(messages) >= 5
            )
            publisher.publish_state("gate_open", True)
            publisher.publish_attributes("gate_open", ATTRIBUTES)
            publisher.publish_discovery()
            received = live.wait_for(
                "state, attributes, and discovery again", lambda m: len(m) >= 9
            )
            with Subscriber(mqtt_address, topic_filters(config)) as late:
                retained = late.sync()
        finally:
            publisher.stop()

    announcement = [
        (service_availability_topic(config), "online", 1),
        (
            discovery_topic("gate_open", config),
            discovery_payload("gate_open", config),
            1,
        ),
        (
            discovery_topic("car_present", config),
            discovery_payload("car_present", config),
            1,
        ),
        # No camera status yet: offline until the scheduler reports one.
        (availability_topic("gate_open", config), "offline", 0),
        (availability_topic("car_present", config), "online", 0),
    ]
    assert [(m.topic, decode(m), m.qos) for m in received] == [
        *announcement,
        (state_topic("gate_open", config), "ON", 0),
        (attributes_topic("gate_open", config), ATTRIBUTES, 0),
        # publish_discovery() while connected sends both configs again.
        *announcement[1:3],
    ]
    # A later subscriber gets only retained messages, each flagged retained:
    # the announcement, and never the state or the attributes.
    assert sorted(
        [(m.topic, decode(m), m.qos, m.retain) for m in retained],
        key=lambda item: item[0],
    ) == sorted(
        [(*item, True) for item in announcement],
        key=lambda item: item[0],
    )


def test_sensor_availability_goes_out_at_qos_0_and_stays_retained(
    mqtt_address: tuple[str, int],
) -> None:
    # Paho never resends a QoS 0 message, so no availability from a lost
    # connection can land after a reconnect's replay (ruling 14).
    config = validate(raw_config(mqtt_address))
    gate = availability_topic("gate_open", config)
    publisher = MqttPublisher(config)
    # At subscription QoS 1, a QoS 1 publish would arrive at QoS 1.
    with Subscriber(mqtt_address, [gate], qos=1) as live:
        publisher.start()
        try:
            live.wait_for("the connect replay", lambda messages: len(messages) >= 1)
            publisher.publish_availability("gate_open", True)
            received = live.wait_for(
                "the published availability", lambda messages: len(messages) >= 2
            )
            with Subscriber(mqtt_address, [gate], qos=1) as late:
                retained = late.wait_for(
                    "the retained availability", lambda messages: len(messages) >= 1
                )
        finally:
            publisher.stop()

    assert [(m.payload, m.qos) for m in received] == [("offline", 0), ("online", 0)]
    assert [(m.payload, m.qos, m.retain) for m in retained] == [("online", 0, True)]


def test_a_reconnect_replays_config_and_availability_but_never_state(
    mqtt_address: tuple[str, int], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="djev_sensors")
    config = validate(raw_config(mqtt_address))
    service = service_availability_topic(config)
    publisher = MqttPublisher(config)
    with Subscriber(mqtt_address, topic_filters(config)) as live:
        publisher.start()
        try:
            live.wait_for(
                "the connect announcement", lambda messages: len(messages) >= 5
            )
            publisher.publish_availability("gate_open", True)
            publisher.publish_state("gate_open", True)
            publisher.publish_attributes("gate_open", ATTRIBUTES)
            before = len(live.wait_for("state and attributes", lambda m: len(m) >= 8))
            take_over_client_id(mqtt_address, config.system.mqtt.client_id)
            live.wait_for(
                "the announcement after the reconnect",
                lambda m: (
                    payloads(m[before:], availability_topic("car_present", config))
                    != []
                ),
            )
            publisher.publish_state("gate_open", False)
            received = live.wait_for(
                "the state published after the reconnect",
                lambda m: payloads(m[before:], state_topic("gate_open", config)) != [],
            )
        finally:
            publisher.stop()

    after = [(m.topic, decode(m)) for m in received[before:]]
    # The broker may publish the dropped connection's Last Will before the replay.
    assert [item for item in after if item != (service, "offline")] == [
        (service, "online"),
        (discovery_topic("gate_open", config), discovery_payload("gate_open", config)),
        (
            discovery_topic("car_present", config),
            discovery_payload("car_present", config),
        ),
        (availability_topic("gate_open", config), "online"),
        (availability_topic("car_present", config), "offline"),
        # The earlier ON never comes back; OFF is the state published afterwards.
        (state_topic("gate_open", config), "OFF"),
    ]
    assert event_names(caplog) == [
        "mqtt.connected",
        "mqtt.discovery_published",
        "mqtt.disconnected",
        "mqtt.connected",
        "mqtt.discovery_published",
        "mqtt.disconnected",
    ]
    # Paho reports a connection the broker closed as "Unspecified error".
    assert logged(caplog)[2] == (
        logging.WARNING,
        {
            "event": "mqtt.disconnected",
            "reason_code": 128,
            "reason": "Unspecified error",
        },
    )


def test_the_last_will_marks_the_service_offline_when_the_process_dies(
    mqtt_address: tuple[str, int], tmp_path: Path
) -> None:
    raw = raw_config(mqtt_address)
    service = service_availability_topic(validate(raw))
    stderr_path = tmp_path / "publisher.stderr"
    with stderr_path.open("wb") as stderr:
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                CHILD_PUBLISHER,
                json.dumps(raw),
                json.dumps(MODEL_ENV),
            ],
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )
    try:
        with Subscriber(mqtt_address, [service]) as watcher:
            watcher.wait_for(
                "the child publisher's online, or its exit",
                lambda m: (
                    child.poll() is not None or payloads(m, service) == ["online"]
                ),
            )
            assert child.poll() is None, stderr_path.read_text()
            child.kill()  # SIGKILL: the connection ends without a DISCONNECT
            child.wait(timeout=TIMEOUT)
            watcher.wait_for(
                "the Last Will", lambda m: payloads(m, service) == ["online", "offline"]
            )
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=TIMEOUT)

    with Subscriber(mqtt_address, [service]) as late:
        retained = late.wait_for("the retained availability", lambda m: len(m) >= 1)
    assert [(m.payload, m.qos, m.retain) for m in retained] == [("offline", 1, True)]


def test_stop_leaves_the_service_offline_and_never_logs_the_password(
    mqtt_address: tuple[str, int], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    password = "mqtt-password-7f3a9c"
    config = validate(
        raw_config(mqtt_address, username="djev", password_env="MQTT_PASSWORD"),
        MQTT_PASSWORD=password,
    )
    service = service_availability_topic(config)
    publisher = MqttPublisher(config)
    with Subscriber(mqtt_address, [service]) as watcher:
        publisher.start()
        try:
            watcher.wait_for("online", lambda m: payloads(m, service) == ["online"])
        finally:
            publisher.stop()
        watcher.wait_for(
            "offline", lambda m: payloads(m, service) == ["online", "offline"]
        )

    with Subscriber(mqtt_address, [service]) as late:
        retained = late.wait_for("the retained availability", lambda m: len(m) >= 1)
    assert [(m.payload, m.qos, m.retain) for m in retained] == [("offline", 1, True)]
    host, port = mqtt_address
    assert logged(caplog) == [
        (logging.INFO, {"event": "mqtt.connected", "host": host, "port": port}),
        (logging.INFO, {"event": "mqtt.discovery_published", "sensors": 2}),
        (
            logging.INFO,
            {
                "event": "mqtt.disconnected",
                "reason_code": 0,
                "reason": "Normal disconnection",
            },
        ),
    ]
    assert password not in caplog.text


def test_stop_marks_every_sensor_offline_before_the_service(
    mqtt_address: tuple[str, int],
) -> None:
    # A sensor removed from the config before the next start then stays
    # unavailable in Home Assistant instead of showing its last state.
    config = validate(raw_config(mqtt_address))
    service = service_availability_topic(config)
    sensors = [availability_topic(s, config) for s in ("gate_open", "car_present")]
    filters = [f"{config.system.mqtt.topic_prefix}/#"]
    publisher = MqttPublisher(config)
    publisher.publish_availability("gate_open", True)
    publisher.publish_availability("car_present", True)
    with Subscriber(mqtt_address, filters) as live:
        publisher.start()
        try:
            live.wait_for("the connect announcement", lambda m: len(m) >= 3)
        finally:
            publisher.stop()
        received = live.wait_for(
            "the service offline",
            lambda m: payloads(m, service) == ["online", "offline"],
        )
    with Subscriber(mqtt_address, filters) as late:
        retained = late.sync()

    assert [(m.topic, m.payload, m.qos) for m in received] == [
        (service, "online", 1),
        *[(topic, "online", 0) for topic in sensors],
        *[(topic, "offline", 0) for topic in sensors],
        (service, "offline", 1),
    ]
    assert sorted((m.topic, m.payload, m.retain) for m in retained) == sorted(
        [(topic, "offline", True) for topic in (service, *sensors)]
    )


def test_the_first_connect_signal_waits_until_discovery_is_published(
    mqtt_address: tuple[str, int], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="djev_sensors")
    publisher = MqttPublisher(validate(raw_config(mqtt_address)))
    assert not publisher.wait_for_first_connect(0)
    publisher.start()
    try:
        assert publisher.wait_for_first_connect(TIMEOUT)
        logged_by_then = event_names(caplog)
    finally:
        publisher.stop()

    assert logged_by_then == ["mqtt.connected", "mqtt.discovery_published"]
    # It records the first connect, so it stays set after the disconnect.
    assert publisher.wait_for_first_connect(0)


def test_publishing_while_disconnected_logs_and_drops_state_and_attributes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="djev_sensors")
    port = closed_port()
    config = validate(raw_config(("127.0.0.1", port)))
    publisher = MqttPublisher(config)
    publisher.start()
    try:
        wait_until(
            lambda: "mqtt.connect_failed" in event_names(caplog),
            "a failed connection attempt",
        )
        assert not publisher.wait_for_first_connect(0)
        publisher.publish_state("gate_open", True)
        publisher.publish_attributes("gate_open", ATTRIBUTES)
        publisher.publish_attributes("gate_open", {"frame": b"\xff\xd8"})
        # Availability and discovery wait for the next connect instead.
        publisher.publish_availability("gate_open", True)
        publisher.publish_discovery()
    finally:
        publisher.stop()

    assert logged(caplog)[0] == (
        logging.WARNING,
        {"event": "mqtt.connect_failed", "host": "127.0.0.1", "port": port},
    )
    assert [e for e in events(caplog) if e["event"] == "mqtt.publish_failed"] == [
        {
            "event": "mqtt.publish_failed",
            "topic": state_topic("gate_open", config),
            **NOT_CONNECTED,
        },
        {
            "event": "mqtt.publish_failed",
            "topic": attributes_topic("gate_open", config),
            **NOT_CONNECTED,
        },
        {
            "event": "mqtt.publish_failed",
            "topic": attributes_topic("gate_open", config),
            "error": "Object of type bytes is not JSON serializable",
        },
    ]
    assert "mqtt.connected" not in event_names(caplog)


def test_a_refused_connection_is_logged_and_never_counts_as_connected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="djev_sensors")
    with socket.create_server(("127.0.0.1", 0)) as server:
        server.settimeout(TIMEOUT)
        port = server.getsockname()[1]
        config = validate(raw_config(("127.0.0.1", port)))
        publisher = MqttPublisher(config)
        publisher.start()
        try:
            connection, _ = server.accept()
            with connection:
                connection.settimeout(TIMEOUT)
                connection.recv(4096)  # the CONNECT packet
                connection.sendall(CONNACK_NOT_AUTHORIZED)
                wait_until(
                    lambda: "mqtt.connect_failed" in event_names(caplog),
                    "the refusal to be logged",
                )
            assert not publisher.wait_for_first_connect(0)
            publisher.publish_state("gate_open", True)
        finally:
            publisher.stop()

    assert logged(caplog) == [
        (
            logging.WARNING,
            {
                "event": "mqtt.connect_failed",
                "host": "127.0.0.1",
                "port": port,
                "reason_code": 135,
                "reason": "Not authorized",
            },
        ),
        (
            logging.WARNING,
            {
                "event": "mqtt.publish_failed",
                "topic": state_topic("gate_open", config),
                **NOT_CONNECTED,
            },
        ),
    ]


def test_a_connection_closed_before_connack_is_logged_as_a_failed_connect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # As when the port serves TLS or another protocol: the server takes the
    # connection, reads the CONNECT, and closes the connection unanswered.
    caplog.set_level(logging.INFO, logger="djev_sensors")
    with socket.create_server(("127.0.0.1", 0)) as server:
        server.settimeout(TIMEOUT)
        port = server.getsockname()[1]
        publisher = MqttPublisher(validate(raw_config(("127.0.0.1", port))))
        publisher.start()
        try:
            connection, _ = server.accept()
            with connection:
                connection.settimeout(TIMEOUT)
                connection.recv(4096)  # the CONNECT packet
            wait_until(
                lambda: "mqtt.connect_failed" in event_names(caplog),
                "the failed connect to be logged",
            )
            assert not publisher.wait_for_first_connect(0)
        finally:
            publisher.stop()

    # Paho reports the closed connection as "Unspecified error".
    assert logged(caplog) == [
        (
            logging.WARNING,
            {
                "event": "mqtt.connect_failed",
                "host": "127.0.0.1",
                "port": port,
                "reason_code": 128,
                "reason": "Unspecified error",
            },
        ),
    ]


def test_a_topic_paho_rejects_is_logged_and_the_rest_still_publishes(
    mqtt_address: tuple[str, int], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="djev_sensors")
    # Validation refuses wildcards but not length, and every discovery topic
    # under this prefix is over paho's 65,535-byte limit.
    config = validate(raw_config(mqtt_address, discovery_prefix="h" * 65_536))
    publisher = MqttPublisher(config)
    with Subscriber(mqtt_address, [f"{config.system.mqtt.topic_prefix}/#"]) as live:
        publisher.start()
        try:
            live.wait_for(
                "the connect announcement", lambda messages: len(messages) >= 3
            )
            publisher.publish_state("gate_open", True)
            received = live.wait_for("the state", lambda messages: len(messages) >= 4)
        finally:
            publisher.stop()

    assert [(m.topic, m.payload) for m in received] == [
        (service_availability_topic(config), "online"),
        (availability_topic("gate_open", config), "offline"),
        (availability_topic("car_present", config), "offline"),
        (state_topic("gate_open", config), "ON"),
    ]
    too_long = "Publish topic is too long."
    assert events(caplog)[1:4] == [
        {
            "event": "mqtt.publish_failed",
            "topic": discovery_topic("gate_open", config),
            "error": too_long,
        },
        {
            "event": "mqtt.publish_failed",
            "topic": discovery_topic("car_present", config),
            "error": too_long,
        },
        {"event": "mqtt.discovery_published", "sensors": 0},
    ]
