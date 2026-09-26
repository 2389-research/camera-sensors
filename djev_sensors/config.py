# ABOUTME: Loads and validates the YAML service configuration into typed models.
# ABOUTME: Resolves secrets from environment variables and never logs their values.
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

DEFAULT_PROMPT_WRAPPER = (
    "Evaluate only the visible contents of the supplied camera frame.\n"
    "Answer the requested binary condition based only on visible evidence.\n"
    "Do not assume facts that cannot be seen."
)

_ID_PATTERN = re.compile(r"^[a-z0-9_]+$")
# <topic_prefix>/service/availability is the whole service's availability topic.
_RESERVED_SENSOR_ID = "service"
_ENV_TOKEN_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_CREDENTIAL_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://[^/@]*:([^/@]*)@")
_SINGLE_TOKEN_PATTERN = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")


class ConfigError(Exception):
    """Raised for every configuration load or validation failure.

    The message never contains a secret value or an RTSP URL.
    """


def _env_from_context(context: object) -> Mapping[str, str]:
    if isinstance(context, Mapping):
        env = context.get("env")
        if isinstance(env, Mapping):
            return env
    return {}


def _lookup_env(name: str, env: Mapping[str, str]) -> str:
    value = env.get(name)
    if not value:
        raise ValueError(f"environment variable {name} is not set")
    return value


def _reject_inline_password(value: str) -> None:
    match = _CREDENTIAL_PATTERN.match(value)
    if match is None:
        return
    password = match.group(1)
    if not _SINGLE_TOKEN_PATTERN.match(password):
        raise ValueError(
            "rtsp credentials must supply the password as a single ${VAR} token, "
            "not a literal value"
        )


def _expand_env_tokens(value: str, env: Mapping[str, str]) -> str:
    def _replace(match: re.Match[str]) -> str:
        return _lookup_env(match.group(1), env)

    return _ENV_TOKEN_PATTERN.sub(_replace, value)


def _validate_ids(ids: Iterable[str], kind: str) -> None:
    for id_ in ids:
        if not _ID_PATTERN.match(id_):
            raise ValueError(f"invalid {kind} id {id_!r}: must match [a-z0-9_]+")


class CameraConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rtsp: SecretStr
    fps: float = Field(default=1, gt=0, allow_inf_nan=False)

    @field_validator("rtsp", mode="before")
    @classmethod
    def _resolve_rtsp(cls, value: object, info: ValidationInfo) -> str:
        if not isinstance(value, str):
            raise TypeError("rtsp must be a string")
        _reject_inline_password(value)
        return _expand_env_tokens(value, _env_from_context(info.context))


class SensorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    camera: str
    prompt: str
    device_class: str | None = None
    true_threshold: float = Field(default=0.80, gt=0.5, le=1)
    change_threshold_pct: float = Field(default=2.5, gt=0, le=100)
    cooldown_seconds: float = Field(default=10, ge=0, allow_inf_nan=False)

    @field_validator("prompt")
    @classmethod
    def _strip_and_require_prompt(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("prompt must not be empty")
        return stripped


class MqttConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str
    port: int = 1883
    username: str | None = None
    password: SecretStr | None = None
    discovery_prefix: str = "homeassistant"
    topic_prefix: str = "djev-sensors"
    client_id: str = "djev-sensors"

    @model_validator(mode="before")
    @classmethod
    def _resolve_password(cls, data: object, info: ValidationInfo) -> object:
        """Fill `password` from the variable `password_env` names.

        `password` is never read from the file itself.
        """
        if not isinstance(data, dict):
            return data
        if "password" in data:
            raise ValueError(
                "password must not be set in the config file; set password_env "
                "to the name of the environment variable that holds it"
            )
        resolved = dict(data)
        password_env = resolved.pop("password_env", None)
        if password_env is not None:
            if not password_env:
                raise ValueError("password_env must name an environment variable")
            # Paho sends a password only along with a username.
            if not resolved.get("username"):
                raise ValueError("password_env requires username")
            env = _env_from_context(info.context)
            resolved["password"] = _lookup_env(password_env, env)
        return resolved

    @field_validator("discovery_prefix", "topic_prefix")
    @classmethod
    def _prefix_is_a_topic_level(cls, value: str) -> str:
        """Each prefix starts topic names, where MQTT allows no wildcard.

        A wildcard in `topic_prefix` would reach the Last Will topic too,
        which would make the CONNECT itself invalid.
        """
        if not value:
            raise ValueError("must not be empty")
        if "+" in value or "#" in value:
            raise ValueError("must not contain the MQTT wildcards + or #")
        return value


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["lunaroute"]
    model: Literal["djev"]
    api_key: SecretStr
    timeout_seconds: float = Field(default=15, gt=0, allow_inf_nan=False)
    prompt_wrapper: str = DEFAULT_PROMPT_WRAPPER

    @model_validator(mode="before")
    @classmethod
    def _resolve_api_key(cls, data: object, info: ValidationInfo) -> object:
        """Fill `api_key` from the variable `api_key_env` names.

        `api_key` is never read from the file itself.
        """
        if not isinstance(data, dict):
            return data
        if "api_key" in data:
            raise ValueError(
                "api_key must not be set in the config file; set api_key_env "
                "to the name of the environment variable that holds it"
            )
        resolved = dict(data)
        api_key_env = resolved.pop("api_key_env", None)
        if not api_key_env:
            raise ValueError("api_key_env is required")
        resolved["api_key"] = _lookup_env(api_key_env, _env_from_context(info.context))
        return resolved

    @field_validator("prompt_wrapper")
    @classmethod
    def _strip_wrapper(cls, value: str) -> str:
        return value.strip()


class InferenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrent_requests: int = Field(default=4, ge=1)


class ChangeDetectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["frame_difference"] = "frame_difference"
    width: int = Field(default=320, gt=0)
    pixel_delta_threshold: int = Field(default=20, ge=0, le=255)
    blur: int = Field(default=5, ge=0)

    @field_validator("blur")
    @classmethod
    def _blur_zero_or_odd(cls, value: int) -> int:
        if value != 0 and value % 2 == 0:
            raise ValueError("blur must be 0 or a positive odd integer")
        return value


class SystemConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mqtt: MqttConfig
    model: ModelConfig
    inference: InferenceConfig = Field(default_factory=InferenceConfig)
    change_detection: ChangeDetectionConfig = Field(
        default_factory=ChangeDetectionConfig
    )


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system: SystemConfig
    cameras: dict[str, CameraConfig]
    sensors: dict[str, SensorConfig]

    @field_validator("cameras")
    @classmethod
    def _validate_camera_ids(
        cls, value: dict[str, CameraConfig]
    ) -> dict[str, CameraConfig]:
        _validate_ids(value.keys(), "camera")
        return value

    @field_validator("sensors")
    @classmethod
    def _validate_sensor_ids(
        cls, value: dict[str, SensorConfig]
    ) -> dict[str, SensorConfig]:
        if not value:
            raise ValueError("at least one sensor is required")
        _validate_ids(value.keys(), "sensor")
        if _RESERVED_SENSOR_ID in value:
            raise ValueError(
                f"sensor id {_RESERVED_SENSOR_ID!r} is reserved for the "
                "service's own availability topic"
            )
        return value

    @model_validator(mode="after")
    def _validate_camera_references(self) -> AppConfig:
        camera_refs = {sensor.camera for sensor in self.sensors.values()}
        unknown = sorted(camera_refs - set(self.cameras))
        if unknown:
            names = ", ".join(unknown)
            raise ValueError(f"sensors reference undefined cameras: {names}")
        return self


def _format_validation_error(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors(include_input=False, include_url=False):
        loc = ".".join(str(part) for part in error["loc"])
        message = error["msg"]
        parts.append(f"{loc}: {message}" if loc else message)
    return "; ".join(parts)


def _describe_yaml_error(exc: yaml.YAMLError) -> str:
    """PyYAML's account of a parse failure, without the source it quotes.

    str() of a marked error quotes the lines around each mark, and a broken
    line can hold a secret, so this keeps only each message with the line and
    column it points at. PyYAML's other errors quote no source.
    """
    if not isinstance(exc, yaml.MarkedYAMLError):
        return str(exc)
    parts = []
    for message, mark in (
        (exc.context, exc.context_mark),
        (exc.problem, exc.problem_mark),
    ):
        if message is None:
            continue
        if mark is not None:
            message += f" (line {mark.line + 1}, column {mark.column + 1})"
        parts.append(message)
    return ": ".join(parts)


def load_config(path: Path, env: Mapping[str, str]) -> AppConfig:
    """Load and validate the YAML service configuration.

    Raises ConfigError for every failure: an unreadable or non-UTF-8 file,
    invalid YAML, a validation error, or a missing/empty environment
    variable. The error message never contains a secret value or an RTSP URL.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"could not read config file {path}: {exc}") from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"invalid YAML in {path}: {_describe_yaml_error(exc)}"
        ) from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"config file {path} must contain a mapping at the top level")

    try:
        return AppConfig.model_validate(raw, context={"env": env})
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc
