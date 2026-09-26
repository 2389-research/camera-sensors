# ABOUTME: Home Assistant MQTT Discovery (spec section 25): each sensor's retained
# ABOUTME: config topic and payload, plus its state, attribute, and availability topics.
from __future__ import annotations

from djev_sensors.config import SERVICE_TOPIC_ID, AppConfig

# Identifies this service to Home Assistant whatever the prefixes: the discovery
# node ID, the start of every unique ID, and the device identifier.
NODE_ID = "djev_sensors"

PAYLOAD_ON = "ON"
PAYLOAD_OFF = "OFF"
PAYLOAD_AVAILABLE = "online"
PAYLOAD_NOT_AVAILABLE = "offline"


def state_topic(sensor_id: str, config: AppConfig) -> str:
    return f"{config.system.mqtt.topic_prefix}/{sensor_id}/state"


def attributes_topic(sensor_id: str, config: AppConfig) -> str:
    return f"{config.system.mqtt.topic_prefix}/{sensor_id}/attributes"


def availability_topic(sensor_id: str, config: AppConfig) -> str:
    return f"{config.system.mqtt.topic_prefix}/{sensor_id}/availability"


def service_availability_topic(config: AppConfig) -> str:
    """The whole service's availability, which the MQTT Last Will sets offline."""
    return availability_topic(SERVICE_TOPIC_ID, config)


def discovery_topic(sensor_id: str, config: AppConfig) -> str:
    prefix = config.system.mqtt.discovery_prefix
    return f"{prefix}/binary_sensor/{NODE_ID}/{sensor_id}/config"


def discovery_payload(sensor_id: str, config: AppConfig) -> dict[str, object]:
    """The binary sensor config Home Assistant builds the entity from.

    The entity is available only while both its own availability topic and
    the service's say online. `default_entity_id` asks for
    `binary_sensor.<sensor_id>`, which Home Assistant would otherwise prefix
    with the device name.
    """
    sensor = config.sensors[sensor_id]
    payload: dict[str, object] = {
        "name": sensor.name,
        "unique_id": f"{NODE_ID}_{sensor_id}",
        "default_entity_id": f"binary_sensor.{sensor_id}",
        "state_topic": state_topic(sensor_id, config),
        "json_attributes_topic": attributes_topic(sensor_id, config),
        "availability": [
            {"topic": availability_topic(sensor_id, config)},
            {"topic": service_availability_topic(config)},
        ],
        "availability_mode": "all",
        "payload_on": PAYLOAD_ON,
        "payload_off": PAYLOAD_OFF,
        "payload_available": PAYLOAD_AVAILABLE,
        "payload_not_available": PAYLOAD_NOT_AVAILABLE,
    }
    if sensor.device_class is not None:
        payload["device_class"] = sensor.device_class
    payload["device"] = {
        "identifiers": [NODE_ID],
        "name": "Djev Vision Sensors",
        "manufacturer": "2389 Research",
    }
    return payload
