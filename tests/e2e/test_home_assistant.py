# ABOUTME: Product end-to-end tests: the service's container reads RTSP from MediaMTX
# ABOUTME: and publishes to Mosquitto; Home Assistant's REST API shows what users see.
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.local_ports import require_free, wait_until_serving

E2E_DIR = Path(__file__).parent
COMPOSE_FILE = E2E_DIR / "compose.yaml"
COMPOSE_PROJECT = "djev-sensors-e2e"
SERVICE = "djev-sensors"
# Holds the service's config, which compose.yaml mounts read-only. It sits in the repo
# rather than pytest's tmp_path because Colima shares only the home directory.
RUN_DIR = E2E_DIR / ".run"
HOME_ASSISTANT_PORT = 18123
RTSP_PORT = 18555
MQTT_PORT = 18884
HOME_ASSISTANT_URL = f"http://127.0.0.1:{HOME_ASSISTANT_PORT}"
# Home Assistant's OAuth client ID must be a URL; its own address will do.
CLIENT_ID = f"{HOME_ASSISTANT_URL}/"

HOME_ASSISTANT_BOOT_TIMEOUT = 300.0  # first boot, on a small VM
STATE_TIMEOUT = 60.0  # seconds for Home Assistant to show any one expected state
POLL_SECONDS = 0.5

CAMERA = "e2e_camera"
SOURCE_SIZE = (640, 360)
SOURCE_FPS = 5
STATIC_PATH = "static"
SWITCHING_PATH = "black-to-white"
WHITE_AFTER_SECONDS = 30.0  # long enough for the service to start and connect first
# Shaped like a LunaRoute key but never valid, and never sent: the static clip
# never changes, so no sample triggers an inference.
FAKE_API_KEY = "lr_e2e_fake_key_never_sent"
SERVICE_AVAILABILITY_TOPIC = "djev-sensors/service/availability"


def compose(
    *args: str, env: Mapping[str, str] | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run docker compose on the end-to-end stack; fail the test on error if `check`.

    `env` adds to this process's environment, as for the key the service's
    container takes from whoever starts it.
    """
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            COMPOSE_PROJECT,
            "--file",
            str(COMPOSE_FILE),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env} if env else None,
    )
    if check and result.returncode != 0:
        tail = "\n".join(result.stderr.splitlines()[-40:])
        pytest.fail(f"docker compose {' '.join(args)} failed:\n{tail}")
    return result


def succeeded(response: httpx.Response) -> httpx.Response:
    """The response if it succeeded; otherwise fail the test with what came back."""
    if not response.is_success:
        request = response.request
        pytest.fail(
            f"{request.method} {request.url.path} returned HTTP "
            f"{response.status_code}: {response.text[:500]}"
        )
    return response


def ok(response: httpx.Response) -> Any:
    """The JSON body of a successful response; otherwise fail the test."""
    return succeeded(response).json()


class HomeAssistant:
    """Home Assistant's REST API, as its onboarded owner."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def state(self, entity_id: str) -> dict[str, Any] | None:
        """The entity's state object, or None while Home Assistant has none."""
        response = self._client.get(f"/api/states/{entity_id}")
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        state: dict[str, Any] = ok(response)
        return state

    def wait_for_state(
        self, entity_id: str, expected: str, timeout: float = STATE_TIMEOUT
    ) -> dict[str, Any]:
        """Poll until the entity's state is `expected`, then return its state object."""
        deadline = time.monotonic() + timeout
        while True:
            current = self.state(entity_id)
            if current is not None and current["state"] == expected:
                return current
            if time.monotonic() > deadline:
                pytest.fail(
                    f"{entity_id} was not {expected!r} within {timeout:.0f} s; "
                    f"last seen: {current}"
                )
            time.sleep(POLL_SECONDS)

    def render(self, template: str, **variables: str) -> str:
        """Render a template in Home Assistant, through its template API."""
        response = self._client.post(
            "/api/template", json={"template": template, "variables": variables}
        )
        return succeeded(response).text


def wait_for_onboarding_api(client: httpx.Client) -> None:
    """Wait until Home Assistant's first boot serves its onboarding status."""
    deadline = time.monotonic() + HOME_ASSISTANT_BOOT_TIMEOUT
    while True:
        try:
            if client.get("/api/onboarding").status_code == httpx.codes.OK:
                return
        except httpx.TransportError:
            pass  # not listening yet, or the port forward dropped the connection
        if time.monotonic() > deadline:
            pytest.fail(
                "Home Assistant served no onboarding status within "
                f"{HOME_ASSISTANT_BOOT_TIMEOUT:.0f} s"
            )
        time.sleep(1.0)


def onboard(client: httpx.Client) -> str:
    """Create the owner, trade its auth code for a token, and finish onboarding.

    Returns the owner's access token, which lasts 30 minutes, longer than the
    module runs. The endpoints and payloads are those of the image's
    homeassistant/components/onboarding/views.py and auth/__init__.py.
    """
    owner = ok(
        client.post(
            "/api/onboarding/users",
            json={
                "client_id": CLIENT_ID,
                "name": "End To End",
                "username": "e2e",
                "password": "e2e-password",
                "language": "en",
            },
        )
    )
    tokens = ok(
        client.post(
            "/auth/token",
            data={
                "grant_type": "authorization_code",
                "code": owner["auth_code"],
                "client_id": CLIENT_ID,
            },
        )
    )
    access_token: str = tokens["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}
    ok(client.post("/api/onboarding/core_config", headers=headers))
    ok(client.post("/api/onboarding/analytics", headers=headers))
    ok(
        client.post(
            "/api/onboarding/integration",
            headers=headers,
            json={
                "client_id": CLIENT_ID,
                "redirect_uri": f"{CLIENT_ID}?auth_callback=1",
            },
        )
    )
    steps = ok(client.get("/api/onboarding"))
    assert all(step["done"] for step in steps), steps
    return access_token


def add_mqtt_integration(client: httpx.Client) -> None:
    """Add Home Assistant's MQTT integration through its config flow, with no YAML.

    Without Supervisor, the flow's user step goes straight to the broker form
    (the image's homeassistant/components/mqtt/config_flow.py). Its defaults
    keep the discovery prefix "homeassistant".
    """
    flow = ok(client.post("/api/config/config_entries/flow", json={"handler": "mqtt"}))
    assert (flow["type"], flow["step_id"]) == ("form", "broker"), flow
    result = ok(
        client.post(
            f"/api/config/config_entries/flow/{flow['flow_id']}",
            json={
                "broker": "mosquitto",
                "port": 1883,
                "other_settings": {"set_ca_cert": "off", "set_client_cert": False},
            },
        )
    )
    assert result["type"] == "create_entry", result
    assert result["result"]["state"] == "loaded", result


def ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        pytest.fail("ffmpeg is not on PATH; the end-to-end tests publish RTSP with it")
    return path


class Publisher:
    """Host FFmpeg encoding a synthetic picture live into MediaMTX over RTSP/TCP.

    The picture is flat gray, or, given `white_after`, black until that many
    seconds in and white from then on. One process encodes it all, so the
    RTSP connection stays open across the switch.
    """

    def __init__(
        self, path: str, log_path: Path, *, white_after: float | None = None
    ) -> None:
        width, height = SOURCE_SIZE
        if white_after is None:
            picture = ["-i", f"color=c=gray:s={width}x{height}:r={SOURCE_FPS}"]
        else:
            picture = [
                "-i",
                f"color=c=black:s={width}x{height}:r={SOURCE_FPS}",
                # A filled box as big as the frame (w and h default to the input's).
                "-vf",
                f"drawbox=color=white:t=fill:enable='gte(t,{white_after})'",
            ]
        self._command = [
            ffmpeg(),
            "-nostdin",
            "-loglevel",
            "warning",
            "-re",
            "-f",
            "lavfi",
            *picture,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-g",
            str(SOURCE_FPS),  # a keyframe every second
            "-pix_fmt",
            "yuv420p",
            "-f",
            "rtsp",
            "-rtsp_transport",
            "tcp",
            f"rtsp://127.0.0.1:{RTSP_PORT}/{path}",
        ]
        self._log_path = log_path
        self._process: subprocess.Popen[bytes] | None = None
        self.started_at = 0.0  # monotonic seconds, set by start()

    def start(self) -> None:
        assert self._process is None, "publisher already running"
        self.started_at = time.monotonic()
        with self._log_path.open("ab") as log:
            self._process = subprocess.Popen(
                self._command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
            )

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        self._process = None


class Service:
    """The service's container, run from the image the module built."""

    def start(self, config: Mapping[str, Any], api_key: str) -> None:
        """Mount `config` read-only and start the container with `api_key` set."""
        (RUN_DIR / "config.yaml").write_text(yaml.safe_dump(dict(config)))
        compose(
            "up", "--detach", "--no-build", SERVICE, env={"LUNAROUTE_API_KEY": api_key}
        )

    def kill(self) -> None:
        compose("kill", "--signal", "SIGKILL", SERVICE)

    def event_names(self) -> list[str]:
        """The names of the structured events the container has logged, in order."""
        logs = compose("logs", "--no-color", "--no-log-prefix", SERVICE).stdout
        return [
            json.loads(line)["event"]
            for line in logs.splitlines()
            if line.startswith("{")
        ]

    def remove(self) -> None:
        """Print the container's logs, then stop and remove the container.

        The removal runs even when reading the logs fails, so the next test
        never reuses a stale container.
        """
        try:
            # pytest shows this only when the test fails.
            print(compose("logs", "--no-color", "--no-log-prefix", SERVICE).stdout)
        finally:
            compose("rm", "--stop", "--force", SERVICE)


def service_config(
    sensor_id: str, sensor: Mapping[str, object], rtsp_path: str
) -> dict[str, Any]:
    """One camera on the stack's MediaMTX and one sensor, publishing to its broker."""
    return {
        "system": {
            "mqtt": {"host": "mosquitto", "port": 1883},
            "model": {
                "provider": "lunaroute",
                "model": "djev",
                "api_key_env": "LUNAROUTE_API_KEY",
            },
        },
        "cameras": {CAMERA: {"rtsp": f"rtsp://mediamtx:8554/{rtsp_path}", "fps": 1}},
        "sensors": {sensor_id: {"camera": CAMERA, **sensor}},
    }


def static_scene(sensor_id: str, name: str) -> dict[str, Any]:
    """A sensor watching the static clip, whose prompt never reaches the model."""
    sensor = {
        "name": name,
        "prompt": "Is anyone in view?",
        "device_class": "occupancy",
        # No sample of the flat clip differs from the one before. Demanding a
        # change in every pixel also keeps a decoding glitch from sending the
        # fake key anywhere.
        "change_threshold_pct": 100,
    }
    return service_config(sensor_id, sensor, STATIC_PATH)


def assert_no_model_request(service: Service) -> None:
    """The fake key never left: no sample changed enough to start an inference."""
    assert "inference.started" not in service.event_names()


def read_retained() -> subprocess.CompletedProcess[str]:
    """Run mosquitto_sub in the broker's container to print every retained message."""
    return compose(
        "exec",
        "-T",
        "mosquitto",
        "mosquitto_sub",
        "--retained-only",
        "-v",
        "-W",
        "2",
        "-t",
        "#",
        check=False,
    )


def wait_for_retained(
    what: str, condition: Callable[[dict[str, str]], bool]
) -> dict[str, str]:
    """Poll the broker's retained messages, by topic, until `condition` holds."""
    deadline = time.monotonic() + STATE_TIMEOUT
    while True:
        result = read_retained()
        retained: dict[str, str] = {}
        for line in result.stdout.splitlines():  # -v prints "topic payload"
            topic, _, payload = line.partition(" ")
            retained[topic] = payload
        # -W ends the wait with libmosquitto's timeout code, 27; a broker still
        # starting refuses the connection instead.
        if result.returncode in (0, 27) and condition(retained):
            return retained
        if time.monotonic() > deadline:
            pytest.fail(
                f"timed out after {STATE_TIMEOUT:.0f} s waiting for {what}; "
                f"mosquitto_sub exited {result.returncode} ({result.stderr.strip()}) "
                f"with {retained}"
            )
        time.sleep(POLL_SECONDS)


@pytest.fixture(scope="module")
def stack() -> Iterator[None]:
    """Run MediaMTX, Mosquitto, and Home Assistant for the module, then remove it all.

    Teardown also removes the service's container and its locally built image.
    """
    compose("down", "--volumes", "--remove-orphans", "--rmi", "local", check=False)
    shutil.rmtree(RUN_DIR, ignore_errors=True)  # both left by an interrupted run
    for port in (HOME_ASSISTANT_PORT, RTSP_PORT, MQTT_PORT):
        require_free(port)
    RUN_DIR.mkdir()
    try:
        compose("up", "--detach", "mediamtx", "mosquitto", "homeassistant")
        for port in (RTSP_PORT, MQTT_PORT):
            wait_until_serving(port)
        yield
    finally:
        down = compose(
            "down", "--volumes", "--remove-orphans", "--rmi", "local", check=False
        )
        shutil.rmtree(RUN_DIR, ignore_errors=True)
        if down.returncode != 0:
            pytest.fail(f"docker compose down failed:\n{down.stderr}")


@pytest.fixture(scope="module")
def home_assistant(stack: None) -> Iterator[HomeAssistant]:
    """Home Assistant, onboarded, with its MQTT integration on the stack's Mosquitto."""
    with httpx.Client(base_url=HOME_ASSISTANT_URL, timeout=30.0) as client:
        wait_for_onboarding_api(client)
        client.headers["Authorization"] = f"Bearer {onboard(client)}"
        add_mqtt_integration(client)
        yield HomeAssistant(client)


@pytest.fixture(scope="module")
def service_image(stack: None) -> None:
    """Build the service's image from the repo's Dockerfile, as compose.yaml says."""
    compose("build", SERVICE)


@pytest.fixture
def service(service_image: None) -> Iterator[Service]:
    """The service's container, removed after the test."""
    service = Service()
    try:
        yield service
    finally:
        service.remove()


@pytest.fixture
def start_publisher(stack: None, tmp_path: Path) -> Iterator[Callable[..., Publisher]]:
    """Start RTSP publishers for the test; all of them stop after it."""
    started: list[Publisher] = []

    def start(path: str, *, white_after: float | None = None) -> Publisher:
        publisher = Publisher(path, tmp_path / f"{path}.log", white_after=white_after)
        publisher.start()
        started.append(publisher)
        return publisher

    try:
        yield start
    finally:
        for publisher in started:
            publisher.stop()


def test_home_assistant_discovers_the_sensor_and_it_turns_available_with_its_camera(
    home_assistant: HomeAssistant,
    service: Service,
    start_publisher: Callable[..., Publisher],
) -> None:
    service.start(static_scene("discovered_scene", "Discovered Scene"), FAKE_API_KEY)
    entity_id = "binary_sensor.discovered_scene"

    # Discovery created the entity under the ID the service asked for. Nothing
    # publishes RTSP yet, so the camera is down and the entity unavailable.
    entity = home_assistant.wait_for_state(entity_id, "unavailable")
    assert entity["attributes"]["device_class"] == "occupancy"
    assert (
        entity["attributes"]["friendly_name"] == "Djev Vision Sensors Discovered Scene"
    )
    device = home_assistant.render(
        "{{ device_attr(entity, 'name') }}|{{ device_attr(entity, 'manufacturer') }}|"
        "{{ ('mqtt', 'djev_sensors') in device_attr(entity, 'identifiers') }}",
        entity=entity_id,
    )
    assert device == "Djev Vision Sensors|2389 Research|True"

    start_publisher(STATIC_PATH)

    # Available, and unknown until a confident judgment.
    home_assistant.wait_for_state(entity_id, "unknown")
    assert_no_model_request(service)


def test_losing_the_rtsp_publisher_makes_the_sensor_unavailable_until_it_returns(
    home_assistant: HomeAssistant,
    service: Service,
    start_publisher: Callable[..., Publisher],
) -> None:
    publisher = start_publisher(STATIC_PATH)
    service.start(static_scene("rtsp_outage_scene", "RTSP Outage Scene"), FAKE_API_KEY)
    entity_id = "binary_sensor.rtsp_outage_scene"
    home_assistant.wait_for_state(entity_id, "unknown")

    publisher.stop()
    home_assistant.wait_for_state(entity_id, "unavailable")
    publisher.start()
    home_assistant.wait_for_state(entity_id, "unknown")

    names = service.event_names()
    assert names.count("camera.disconnected") == 1
    assert names.count("camera.connected") == 2
    assert_no_model_request(service)


def test_killing_the_service_fires_the_shared_last_will(
    home_assistant: HomeAssistant,
    service: Service,
    start_publisher: Callable[..., Publisher],
) -> None:
    start_publisher(STATIC_PATH)
    service.start(static_scene("last_will_scene", "Last Will Scene"), FAKE_API_KEY)
    entity_id = "binary_sensor.last_will_scene"
    home_assistant.wait_for_state(entity_id, "unknown")
    assert_no_model_request(service)

    service.kill()

    home_assistant.wait_for_state(entity_id, "unavailable")
    retained = wait_for_retained(
        "the Last Will",
        lambda retained: retained.get(SERVICE_AVAILABILITY_TOPIC) == "offline",
    )
    # The broker marked every sensor's shared topic offline, while this sensor's
    # own topic, which only the dead service could change, still says online.
    assert retained["djev-sensors/last_will_scene/availability"] == "online"


def test_a_broker_restart_brings_back_discovery_and_availability_without_state(
    home_assistant: HomeAssistant,
    service: Service,
    start_publisher: Callable[..., Publisher],
) -> None:
    start_publisher(STATIC_PATH)
    sensor_id = "broker_restart_scene"
    service.start(static_scene(sensor_id, "Broker Restart Scene"), FAKE_API_KEY)
    entity_id = f"binary_sensor.{sensor_id}"
    config_topic = f"homeassistant/binary_sensor/djev_sensors/{sensor_id}/config"
    availability_topic = f"djev-sensors/{sensor_id}/availability"
    home_assistant.wait_for_state(entity_id, "unknown")

    compose("stop", "mosquitto")
    home_assistant.wait_for_state(entity_id, "unavailable")  # its broker went too
    compose("start", "mosquitto")

    # Mosquitto keeps nothing across a restart, so all of this is the service's
    # reconnect replay.
    retained = wait_for_retained(
        "discovery and availability after the restart",
        lambda retained: (
            config_topic in retained
            and retained.get(availability_topic) == "online"
            and retained.get(SERVICE_AVAILABILITY_TOPIC) == "online"
        ),
    )
    assert (
        json.loads(retained[config_topic])["unique_id"] == f"djev_sensors_{sensor_id}"
    )
    assert f"djev-sensors/{sensor_id}/state" not in retained
    # Home Assistant reconnected and still has no state for the sensor.
    home_assistant.wait_for_state(entity_id, "unknown")
    names = service.event_names()
    assert names.count("mqtt.connected") == 2
    assert "sensor.state_changed" not in names
    assert_no_model_request(service)


def test_a_confident_djev_judgment_turns_the_sensor_on_with_its_attributes(
    home_assistant: HomeAssistant,
    service: Service,
    start_publisher: Callable[..., Publisher],
) -> None:
    api_key = os.environ.get("LUNAROUTE_API_KEY")
    if not api_key:
        pytest.fail("LUNAROUTE_API_KEY is not set; this test needs a rotated key")
    publisher = start_publisher(SWITCHING_PATH, white_after=WHITE_AFTER_SECONDS)
    # The earliest the picture can turn white; FFmpeg starts after this clock.
    white_at = publisher.started_at + WHITE_AFTER_SECONDS
    sensor = {
        "name": "Frame Mostly White",
        "prompt": "Is the frame mostly white?",
        "true_threshold": 0.55,
        "change_threshold_pct": 50,
    }
    service.start(service_config("frame_mostly_white", sensor, SWITCHING_PATH), api_key)
    entity_id = "binary_sensor.frame_mostly_white"

    home_assistant.wait_for_state(entity_id, "unknown")
    assert time.monotonic() < white_at, (
        "the camera connected after the frame turned white, so no black sample "
        "preceded the change; raise WHITE_AFTER_SECONDS"
    )

    entity = home_assistant.wait_for_state(
        entity_id, "on", timeout=white_at - time.monotonic() + STATE_TIMEOUT
    )
    attributes = entity["attributes"]
    assert attributes["true_probability"] >= 0.55
    assert attributes["true_threshold"] == 0.55
    # A plain frame fits the request budget at full size.
    assert (attributes["sent_width"], attributes["sent_height"]) == SOURCE_SIZE
