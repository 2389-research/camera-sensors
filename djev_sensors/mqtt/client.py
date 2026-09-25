# ABOUTME: Publishes the sensors to Home Assistant over MQTT with paho-mqtt: retained
# ABOUTME: discovery and availability, a retained Last Will, and non-retained state.
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion, MQTTErrorCode
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from djev_sensors.config import AppConfig
from djev_sensors.events import log_event
from djev_sensors.mqtt import discovery

# How long stop() waits for the broker to acknowledge the retained "offline".
OFFLINE_ACK_TIMEOUT_SECONDS = 2.0


class MqttPublisher:
    """Publishes the scheduler's output for Home Assistant; a SensorPublisher.

    The publish methods never raise. A publish that fails, or that cannot
    happen because the client is not connected, is logged as
    `mqtt.publish_failed` and dropped; nothing queues it for later.

    Every successful connect, first or later, publishes the service "online",
    each sensor's discovery config, and each sensor's last known availability,
    which starts "offline" until camera status arrives. State and attributes
    are published once, only while connected, and never replayed.

    Paho runs the connection callbacks on its network thread while callers
    publish from theirs, so `_lock` guards the connection flag and the
    availability record. The connect callback holds it while it publishes, so
    an availability change cannot slip between its read and its publish;
    callers release it before calling paho, whose own locks the network thread
    may hold while it runs a callback.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._service_topic = discovery.service_availability_topic(config)
        self._lock = threading.Lock()
        self._connected = False
        self._availability = dict.fromkeys(config.sensors, False)
        self._client = mqtt.Client(
            CallbackAPIVersion.VERSION2, client_id=config.system.mqtt.client_id
        )
        self._client.on_connect = self._on_connect
        self._client.on_connect_fail = self._on_connect_fail
        self._client.on_disconnect = self._on_disconnect

    def start(self) -> None:
        """Connect in the background; paho's network thread reconnects after any loss.

        Call once. Returns at once: paho's thread makes each attempt, and
        after a failure or a lost connection waits a delay that starts at
        1 s and doubles up to 120 s. Each failed attempt logs
        `mqtt.connect_failed`.
        """
        mqtt_config = self._config.system.mqtt
        # The broker publishes this if the connection ends without a DISCONNECT.
        self._client.will_set(
            self._service_topic, discovery.PAYLOAD_NOT_AVAILABLE, qos=1, retain=True
        )
        if mqtt_config.username is not None:
            password = mqtt_config.password
            self._client.username_pw_set(
                mqtt_config.username,
                None if password is None else password.get_secret_value(),
            )
        self._client.connect_async(mqtt_config.host, mqtt_config.port)
        self._client.loop_start()

    def stop(self) -> None:
        """Mark the service offline, disconnect, and stop the network thread.

        A clean disconnect cancels the Last Will, so while connected this
        first publishes the retained "offline" and waits up to
        OFFLINE_ACK_TIMEOUT_SECONDS for the broker to acknowledge it. While
        disconnected, the broker publishes the lost connection's Last Will.
        """
        with self._lock:
            connected = self._connected
        if connected:
            info = self._publish(
                self._service_topic, discovery.PAYLOAD_NOT_AVAILABLE, qos=1, retain=True
            )
            if info is not None:
                info.wait_for_publish(OFFLINE_ACK_TIMEOUT_SECONDS)
        self._client.disconnect()
        self._client.loop_stop()
        # Paho closes the socket pair that wakes its network thread only when
        # the client is garbage collected. These bound methods make a reference
        # cycle with this publisher; dropping them lets the client go with it.
        self._client.on_connect = None
        self._client.on_connect_fail = None
        self._client.on_disconnect = None

    def publish_discovery(self) -> None:
        """Publish every sensor's retained discovery config, if connected.

        Every connect publishes discovery anyway, so while disconnected this
        does nothing.
        """
        with self._lock:
            connected = self._connected
        if connected:
            self._publish_discovery()

    def publish_state(self, sensor_id: str, state: bool) -> None:
        """Publish ON or OFF once, not retained, if connected."""
        payload = discovery.PAYLOAD_ON if state else discovery.PAYLOAD_OFF
        self._publish_event(discovery.state_topic(sensor_id, self._config), payload)

    def publish_attributes(
        self, sensor_id: str, attributes: Mapping[str, object]
    ) -> None:
        """Publish the attributes as a JSON object once, not retained, if connected.

        Not retained, like state: after a Home Assistant restart, retained
        attributes would outlive the state they describe.
        """
        topic = discovery.attributes_topic(sensor_id, self._config)
        try:
            payload = json.dumps(dict(attributes), allow_nan=False)
        except (TypeError, ValueError) as exc:
            log_event(
                "mqtt.publish_failed",
                level=logging.WARNING,
                topic=topic,
                error=str(exc),
            )
            return
        self._publish_event(topic, payload)

    def publish_availability(self, sensor_id: str, online: bool) -> None:
        """Record the sensor's availability and publish it, retained, if connected.

        While disconnected only the record changes; the next connect publishes it.
        """
        with self._lock:
            self._availability[sensor_id] = online
            connected = self._connected
        if connected:
            self._publish_availability(sensor_id, online)

    def _publish_availability(self, sensor_id: str, online: bool) -> None:
        """Publish one sensor's availability, retained, at QoS 0.

        Paho resends a lost connection's unacknowledged QoS 1 messages after
        the next connect's replay, so a stale value could land after it. QoS 0
        is never resent, and the replay on every connect already republishes
        the current value after any loss.
        """
        self._publish(
            discovery.availability_topic(sensor_id, self._config),
            discovery.PAYLOAD_AVAILABLE if online else discovery.PAYLOAD_NOT_AVAILABLE,
            qos=0,
            retain=True,
        )

    def _publish_discovery(self) -> None:
        published = 0
        for sensor_id in self._config.sensors:
            info = self._publish(
                discovery.discovery_topic(sensor_id, self._config),
                json.dumps(discovery.discovery_payload(sensor_id, self._config)),
                qos=1,
                retain=True,
            )
            if info is not None:
                published += 1
        log_event("mqtt.discovery_published", sensors=published)

    def _publish_event(self, topic: str, payload: str) -> None:
        """Publish at QoS 0, not retained, if connected; otherwise log and drop it."""
        with self._lock:
            connected = self._connected
        if not connected:
            # Dropped here, never handed to paho, so no later connection sends it.
            self._log_publish_failed(topic, MQTTErrorCode.MQTT_ERR_NO_CONN)
            return
        self._publish(topic, payload, qos=0, retain=False)

    def _publish(
        self, topic: str, payload: str, *, qos: int, retain: bool
    ) -> mqtt.MQTTMessageInfo | None:
        """Hand one message to paho: its info, or None if paho refused it (logged)."""
        try:
            info = self._client.publish(topic, payload, qos=qos, retain=retain)
        except ValueError as exc:
            # A configured prefix can make a topic paho refuses, such as a wildcard.
            log_event(
                "mqtt.publish_failed",
                level=logging.WARNING,
                topic=topic,
                error=str(exc),
            )
            return None
        if info.rc != MQTTErrorCode.MQTT_ERR_SUCCESS:
            self._log_publish_failed(topic, info.rc)
            return None
        return info

    def _log_publish_failed(self, topic: str, rc: MQTTErrorCode) -> None:
        log_event(
            "mqtt.publish_failed",
            level=logging.WARNING,
            topic=topic,
            rc=int(rc),
            error=mqtt.error_string(rc),
        )

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: object,
        flags: mqtt.ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        mqtt_config = self._config.system.mqtt
        if reason_code.is_failure:
            # The broker refused, as for bad credentials; paho closes and retries.
            log_event(
                "mqtt.connect_failed",
                level=logging.WARNING,
                host=mqtt_config.host,
                port=mqtt_config.port,
                reason_code=reason_code.value,
                reason=str(reason_code),
            )
            return
        with self._lock:
            self._connected = True
            log_event("mqtt.connected", host=mqtt_config.host, port=mqtt_config.port)
            self._publish(
                self._service_topic, discovery.PAYLOAD_AVAILABLE, qos=1, retain=True
            )
            self._publish_discovery()
            for sensor_id, online in self._availability.items():
                self._publish_availability(sensor_id, online)

    def _on_connect_fail(self, client: mqtt.Client, userdata: object) -> None:
        """Paho could not open a TCP connection; it retries after its delay."""
        mqtt_config = self._config.system.mqtt
        log_event(
            "mqtt.connect_failed",
            level=logging.WARNING,
            host=mqtt_config.host,
            port=mqtt_config.port,
        )

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: object,
        flags: mqtt.DisconnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        with self._lock:
            was_connected = self._connected
            self._connected = False
        # Paho also calls this after a refused connect, which was never connected.
        if was_connected:
            log_event(
                "mqtt.disconnected",
                level=logging.WARNING if reason_code.is_failure else logging.INFO,
                reason_code=reason_code.value,
                reason=str(reason_code),
            )
