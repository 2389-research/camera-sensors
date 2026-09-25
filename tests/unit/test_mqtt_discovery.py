# ABOUTME: Unit tests for djev_sensors.mqtt.discovery: Home Assistant discovery topics
# ABOUTME: and payloads for configured sensors, under default and custom MQTT prefixes.
from __future__ import annotations

from collections.abc import Mapping

from djev_sensors.config import AppConfig
from djev_sensors.mqtt.discovery import discovery_payload, discovery_topic

DEVICE = {
    "identifiers": ["djev_sensors"],
    "name": "Djev Vision Sensors",
    "manufacturer": "2389 Research",
}


def make_config(mqtt: Mapping[str, object] | None = None) -> AppConfig:
    """Two sensors on one camera: gate_open has a device class, car_present has none."""
    return AppConfig.model_validate(
        {
            "system": {
                "mqtt": {"host": "mqtt.test", **(mqtt or {})},
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
        },
        context={"env": {"LUNAROUTE_API_KEY": "test-key"}},
    )


def test_discovery_topic_uses_the_default_prefix_and_node_id() -> None:
    assert (
        discovery_topic("gate_open", make_config())
        == "homeassistant/binary_sensor/djev_sensors/gate_open/config"
    )


def test_discovery_payload_follows_the_spec_example() -> None:
    assert discovery_payload("gate_open", make_config()) == {
        "name": "Gate Open",
        "unique_id": "djev_sensors_gate_open",
        "default_entity_id": "binary_sensor.gate_open",
        "state_topic": "djev-sensors/gate_open/state",
        "json_attributes_topic": "djev-sensors/gate_open/attributes",
        "availability": [
            {"topic": "djev-sensors/gate_open/availability"},
            {"topic": "djev-sensors/service/availability"},
        ],
        "availability_mode": "all",
        "payload_on": "ON",
        "payload_off": "OFF",
        "payload_available": "online",
        "payload_not_available": "offline",
        "device_class": "opening",
        "device": DEVICE,
    }


def test_a_sensor_without_a_device_class_omits_the_key() -> None:
    payload = discovery_payload("car_present", make_config())

    assert "device_class" not in payload
    assert payload["name"] == "Car Present"
    assert payload["unique_id"] == "djev_sensors_car_present"


def test_every_sensor_shares_one_device() -> None:
    config = make_config()

    assert discovery_payload("gate_open", config)["device"] == DEVICE
    assert discovery_payload("car_present", config)["device"] == DEVICE


def test_custom_prefixes_move_every_topic_but_not_the_ids() -> None:
    config = make_config({"discovery_prefix": "ha", "topic_prefix": "yard-cams"})

    assert (
        discovery_topic("gate_open", config)
        == "ha/binary_sensor/djev_sensors/gate_open/config"
    )
    payload = discovery_payload("gate_open", config)
    assert payload["state_topic"] == "yard-cams/gate_open/state"
    assert payload["json_attributes_topic"] == "yard-cams/gate_open/attributes"
    assert payload["availability"] == [
        {"topic": "yard-cams/gate_open/availability"},
        {"topic": "yard-cams/service/availability"},
    ]
    assert payload["unique_id"] == "djev_sensors_gate_open"
    assert payload["default_entity_id"] == "binary_sensor.gate_open"
