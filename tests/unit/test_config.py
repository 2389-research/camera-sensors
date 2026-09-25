# ABOUTME: Unit tests for djev_sensors.config: parsing, validation, and defaults.
# ABOUTME: Exercises cross references, environment expansion, and safe error messages.
from __future__ import annotations

from pathlib import Path

import pytest

from djev_sensors.config import DEFAULT_PROMPT_WRAPPER, ConfigError, load_config

BASE_ENV = {
    "GARAGE_RTSP_URL": "rtsp://localhost:8554/garage",
    "BACKYARD_RTSP_URL": "rtsp://localhost:8554/backyard",
    "LUNAROUTE_API_KEY": "lr_test",
    "MQTT_PASSWORD": "test",
}


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body)
    return path


def test_sensor_defaults_and_camera_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("""
system:
  mqtt: {host: localhost, password_env: MQTT_PASSWORD}
  model: {provider: lunaroute, model: djev, api_key_env: LUNAROUTE_API_KEY}
cameras:
  garage: {rtsp: "${GARAGE_RTSP_URL}"}
sensors:
  car: {name: Car, camera: garage, prompt: "Is a car visible?"}
""")
    env = {
        "GARAGE_RTSP_URL": "rtsp://localhost:8554/garage",
        "LUNAROUTE_API_KEY": "lr_test",
        "MQTT_PASSWORD": "test",
    }
    config = load_config(path, env)
    assert config.cameras["garage"].fps == 1
    assert config.sensors["car"].true_threshold == 0.80
    assert config.sensors["car"].camera == "garage"


def test_two_sensors_on_one_camera(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
system:
  mqtt: {host: localhost}
  model: {provider: lunaroute, model: djev, api_key_env: LUNAROUTE_API_KEY}
cameras:
  garage: {rtsp: "${GARAGE_RTSP_URL}"}
sensors:
  car: {name: Car, camera: garage, prompt: "Is a car visible?"}
  door: {name: Door, camera: garage, prompt: "Is the door open?"}
""",
    )
    config = load_config(path, BASE_ENV)
    assert set(config.sensors) == {"car", "door"}
    assert config.sensors["car"].camera == "garage"
    assert config.sensors["door"].camera == "garage"


def test_undefined_camera_reference_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
system:
  mqtt: {host: localhost}
  model: {provider: lunaroute, model: djev, api_key_env: LUNAROUTE_API_KEY}
cameras:
  garage: {rtsp: "${GARAGE_RTSP_URL}"}
sensors:
  car: {name: Car, camera: driveway, prompt: "Is a car visible?"}
""",
    )
    with pytest.raises(ConfigError, match="driveway"):
        load_config(path, BASE_ENV)


@pytest.mark.parametrize("value", [0.5, 1.01, 0, -1])
def test_true_threshold_out_of_range_is_rejected(tmp_path: Path, value: float) -> None:
    path = _write(
        tmp_path,
        f"""
system:
  mqtt: {{host: localhost}}
  model: {{provider: lunaroute, model: djev, api_key_env: LUNAROUTE_API_KEY}}
cameras:
  garage: {{rtsp: "${{GARAGE_RTSP_URL}}"}}
sensors:
  car:
    name: Car
    camera: garage
    prompt: "Is a car visible?"
    true_threshold: {value}
""",
    )
    with pytest.raises(ConfigError, match="true_threshold"):
        load_config(path, BASE_ENV)


def test_change_threshold_pct_out_of_range_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
system:
  mqtt: {host: localhost}
  model: {provider: lunaroute, model: djev, api_key_env: LUNAROUTE_API_KEY}
cameras:
  garage: {rtsp: "${GARAGE_RTSP_URL}"}
sensors:
  car: {name: Car, camera: garage, prompt: "Is a car visible?", change_threshold_pct: 0}
""",
    )
    with pytest.raises(ConfigError, match="change_threshold_pct"):
        load_config(path, BASE_ENV)


def test_missing_garage_rtsp_url_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
system:
  mqtt: {host: localhost}
  model: {provider: lunaroute, model: djev, api_key_env: LUNAROUTE_API_KEY}
cameras:
  garage: {rtsp: "${GARAGE_RTSP_URL}"}
sensors:
  car: {name: Car, camera: garage, prompt: "Is a car visible?"}
""",
    )
    env = {k: v for k, v in BASE_ENV.items() if k != "GARAGE_RTSP_URL"}
    with pytest.raises(ConfigError, match="GARAGE_RTSP_URL"):
        load_config(path, env)


def test_empty_env_value_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
system:
  mqtt: {host: localhost}
  model: {provider: lunaroute, model: djev, api_key_env: LUNAROUTE_API_KEY}
cameras:
  garage: {rtsp: "${GARAGE_RTSP_URL}"}
sensors:
  car: {name: Car, camera: garage, prompt: "Is a car visible?"}
""",
    )
    env = dict(BASE_ENV, GARAGE_RTSP_URL="")
    with pytest.raises(ConfigError, match="GARAGE_RTSP_URL"):
        load_config(path, env)


def test_missing_api_key_env_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
system:
  mqtt: {host: localhost}
  model: {provider: lunaroute, model: djev, api_key_env: LUNAROUTE_API_KEY}
cameras:
  garage: {rtsp: "${GARAGE_RTSP_URL}"}
sensors:
  car: {name: Car, camera: garage, prompt: "Is a car visible?"}
""",
    )
    env = {k: v for k, v in BASE_ENV.items() if k != "LUNAROUTE_API_KEY"}
    with pytest.raises(ConfigError, match="LUNAROUTE_API_KEY"):
        load_config(path, env)


def test_missing_mqtt_password_env_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
system:
  mqtt: {host: localhost, password_env: MQTT_PASSWORD}
  model: {provider: lunaroute, model: djev, api_key_env: LUNAROUTE_API_KEY}
cameras:
  garage: {rtsp: "${GARAGE_RTSP_URL}"}
sensors:
  car: {name: Car, camera: garage, prompt: "Is a car visible?"}
""",
    )
    env = {k: v for k, v in BASE_ENV.items() if k != "MQTT_PASSWORD"}
    with pytest.raises(ConfigError, match="MQTT_PASSWORD"):
        load_config(path, env)


def test_literal_rtsp_password_is_rejected_and_not_leaked(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
system:
  mqtt: {host: localhost}
  model: {provider: lunaroute, model: djev, api_key_env: LUNAROUTE_API_KEY}
cameras:
  garage: {rtsp: "rtsp://admin:hunter2@10.0.0.5/live"}
sensors:
  car: {name: Car, camera: garage, prompt: "Is a car visible?"}
""",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(path, BASE_ENV)
    assert "hunter2" not in str(exc_info.value)


def test_rtsp_with_templated_password_is_accepted(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
system:
  mqtt: {host: localhost}
  model: {provider: lunaroute, model: djev, api_key_env: LUNAROUTE_API_KEY}
cameras:
  garage: {rtsp: "rtsp://admin:${GARAGE_PASS}@10.0.0.5/live"}
sensors:
  car: {name: Car, camera: garage, prompt: "Is a car visible?"}
""",
    )
    env = dict(BASE_ENV, GARAGE_PASS="hunter2")
    config = load_config(path, env)
    assert (
        config.cameras["garage"].rtsp.get_secret_value()
        == "rtsp://admin:hunter2@10.0.0.5/live"
    )


@pytest.mark.parametrize("blur", [0, 1, 3, 5])
def test_blur_zero_or_odd_is_allowed(tmp_path: Path, blur: int) -> None:
    path = _write(
        tmp_path,
        f"""
system:
  mqtt: {{host: localhost}}
  model: {{provider: lunaroute, model: djev, api_key_env: LUNAROUTE_API_KEY}}
  change_detection: {{blur: {blur}}}
cameras:
  garage: {{rtsp: "${{GARAGE_RTSP_URL}}"}}
sensors:
  car: {{name: Car, camera: garage, prompt: "Is a car visible?"}}
""",
    )
    config = load_config(path, BASE_ENV)
    assert config.system.change_detection.blur == blur


@pytest.mark.parametrize("blur", [2, 4, -1, -3])
def test_blur_positive_even_or_negative_is_rejected(tmp_path: Path, blur: int) -> None:
    path = _write(
        tmp_path,
        f"""
system:
  mqtt: {{host: localhost}}
  model: {{provider: lunaroute, model: djev, api_key_env: LUNAROUTE_API_KEY}}
  change_detection: {{blur: {blur}}}
cameras:
  garage: {{rtsp: "${{GARAGE_RTSP_URL}}"}}
sensors:
  car: {{name: Car, camera: garage, prompt: "Is a car visible?"}}
""",
    )
    with pytest.raises(ConfigError, match="blur"):
        load_config(path, BASE_ENV)


def test_anonymous_mqtt_has_no_password(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
system:
  mqtt: {host: localhost}
  model: {provider: lunaroute, model: djev, api_key_env: LUNAROUTE_API_KEY}
cameras:
  garage: {rtsp: "${GARAGE_RTSP_URL}"}
sensors:
  car: {name: Car, camera: garage, prompt: "Is a car visible?"}
""",
    )
    config = load_config(path, BASE_ENV)
    assert config.system.mqtt.password is None


def test_inference_and_change_detection_defaults(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
system:
  mqtt: {host: localhost}
  model: {provider: lunaroute, model: djev, api_key_env: LUNAROUTE_API_KEY}
cameras:
  garage: {rtsp: "${GARAGE_RTSP_URL}"}
sensors:
  car: {name: Car, camera: garage, prompt: "Is a car visible?"}
""",
    )
    config = load_config(path, BASE_ENV)
    assert config.system.inference.max_concurrent_requests == 4
    assert config.system.change_detection.type == "frame_difference"
    assert config.system.change_detection.width == 320
    assert config.system.change_detection.pixel_delta_threshold == 20
    assert config.system.change_detection.blur == 5
    assert config.system.mqtt.port == 1883
    assert config.system.mqtt.discovery_prefix == "homeassistant"
    assert config.system.mqtt.topic_prefix == "djev-sensors"
    assert config.system.mqtt.client_id == "djev-sensors"
    assert config.system.model.timeout_seconds == 15
    assert config.cameras["garage"].fps == 1
    assert config.sensors["car"].change_threshold_pct == 2.5
    assert config.sensors["car"].cooldown_seconds == 10


def test_secret_values_are_not_exposed_via_repr(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
system:
  mqtt: {host: localhost, password_env: MQTT_PASSWORD}
  model: {provider: lunaroute, model: djev, api_key_env: LUNAROUTE_API_KEY}
cameras:
  garage: {rtsp: "${GARAGE_RTSP_URL}"}
sensors:
  car: {name: Car, camera: garage, prompt: "Is a car visible?"}
""",
    )
    config = load_config(path, BASE_ENV)
    rendered = repr(config)
    assert "lr_test" not in rendered
    assert BASE_ENV["MQTT_PASSWORD"] not in rendered
    assert BASE_ENV["GARAGE_RTSP_URL"] not in rendered


def test_invalid_camera_id_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
system:
  mqtt: {host: localhost}
  model: {provider: lunaroute, model: djev, api_key_env: LUNAROUTE_API_KEY}
cameras:
  Garage: {rtsp: "${GARAGE_RTSP_URL}"}
sensors:
  car: {name: Car, camera: Garage, prompt: "Is a car visible?"}
""",
    )
    with pytest.raises(ConfigError, match="Garage"):
        load_config(path, BASE_ENV)


def test_unknown_top_level_key_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
system:
  mqtt: {host: localhost}
  model: {provider: lunaroute, model: djev, api_key_env: LUNAROUTE_API_KEY}
cameras:
  garage: {rtsp: "${GARAGE_RTSP_URL}"}
sensors:
  car: {name: Car, camera: garage, prompt: "Is a car visible?"}
extra_top_level_key: nope
""",
    )
    with pytest.raises(ConfigError):
        load_config(path, BASE_ENV)


def test_empty_sensor_prompt_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
system:
  mqtt: {host: localhost}
  model: {provider: lunaroute, model: djev, api_key_env: LUNAROUTE_API_KEY}
cameras:
  garage: {rtsp: "${GARAGE_RTSP_URL}"}
sensors:
  car: {name: Car, camera: garage, prompt: "   "}
""",
    )
    with pytest.raises(ConfigError, match="prompt"):
        load_config(path, BASE_ENV)


def test_prompt_and_wrapper_are_stripped(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
system:
  mqtt: {host: localhost}
  model:
    provider: lunaroute
    model: djev
    api_key_env: LUNAROUTE_API_KEY
    prompt_wrapper: |
      Custom wrapper text.
cameras:
  garage: {rtsp: "${GARAGE_RTSP_URL}"}
sensors:
  car:
    name: Car
    camera: garage
    prompt: |
      Is a car visible?
""",
    )
    config = load_config(path, BASE_ENV)
    assert config.sensors["car"].prompt == "Is a car visible?"
    assert config.system.model.prompt_wrapper == "Custom wrapper text."


def test_default_prompt_wrapper_matches_spec(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
system:
  mqtt: {host: localhost}
  model: {provider: lunaroute, model: djev, api_key_env: LUNAROUTE_API_KEY}
cameras:
  garage: {rtsp: "${GARAGE_RTSP_URL}"}
sensors:
  car: {name: Car, camera: garage, prompt: "Is a car visible?"}
""",
    )
    config = load_config(path, BASE_ENV)
    assert config.system.model.prompt_wrapper == DEFAULT_PROMPT_WRAPPER
    assert DEFAULT_PROMPT_WRAPPER == (
        "Evaluate only the visible contents of the supplied camera frame.\n"
        "Answer the requested binary condition based only on visible evidence.\n"
        "Do not assume facts that cannot be seen."
    )


def test_invalid_provider_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
system:
  mqtt: {host: localhost}
  model: {provider: openai, model: djev, api_key_env: LUNAROUTE_API_KEY}
cameras:
  garage: {rtsp: "${GARAGE_RTSP_URL}"}
sensors:
  car: {name: Car, camera: garage, prompt: "Is a car visible?"}
""",
    )
    with pytest.raises(ConfigError):
        load_config(path, BASE_ENV)


def test_invalid_model_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
system:
  mqtt: {host: localhost}
  model: {provider: lunaroute, model: gpt4, api_key_env: LUNAROUTE_API_KEY}
cameras:
  garage: {rtsp: "${GARAGE_RTSP_URL}"}
sensors:
  car: {name: Car, camera: garage, prompt: "Is a car visible?"}
""",
    )
    with pytest.raises(ConfigError):
        load_config(path, BASE_ENV)


def test_missing_api_key_env_key_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
system:
  mqtt: {host: localhost}
  model: {provider: lunaroute, model: djev}
cameras:
  garage: {rtsp: "${GARAGE_RTSP_URL}"}
sensors:
  car: {name: Car, camera: garage, prompt: "Is a car visible?"}
""",
    )
    with pytest.raises(ConfigError, match="api_key_env"):
        load_config(path, BASE_ENV)


def test_device_class_is_optional_free_string(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
system:
  mqtt: {host: localhost}
  model: {provider: lunaroute, model: djev, api_key_env: LUNAROUTE_API_KEY}
cameras:
  garage: {rtsp: "${GARAGE_RTSP_URL}"}
sensors:
  car: {name: Car, camera: garage, prompt: "Is a car visible?"}
  door: {name: Door, camera: garage, prompt: "Is the door open?", device_class: door}
""",
    )
    config = load_config(path, BASE_ENV)
    assert config.sensors["car"].device_class is None
    assert config.sensors["door"].device_class == "door"


def test_unreadable_config_file_raises_config_error(tmp_path: Path) -> None:
    path = tmp_path / "missing.yaml"
    with pytest.raises(ConfigError):
        load_config(path, BASE_ENV)


def test_invalid_yaml_raises_config_error(tmp_path: Path) -> None:
    path = _write(tmp_path, "system: [unterminated")
    with pytest.raises(ConfigError):
        load_config(path, BASE_ENV)


def test_example_config_loads_with_fake_environment_values() -> None:
    path = Path(__file__).resolve().parents[2] / "config.example.yaml"
    env = {
        "GARAGE_RTSP_URL": "rtsp://fake/garage",
        "BACKYARD_RTSP_URL": "rtsp://fake/backyard",
        "LUNAROUTE_API_KEY": "fake-key",
        "MQTT_PASSWORD": "fake-password",
    }
    config = load_config(path, env)
    assert set(config.cameras) == {"garage", "backyard"}
    assert set(config.sensors) == {"car_in_garage", "gate_open"}
