# Djev RTSP Sensors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn configured RTSP cameras into Home Assistant MQTT binary sensors whose state changes only after a qualifying visual change and a Djev Noul result through LunaRoute.

**Architecture:** One decoder runs per distinct RTSP URL. Each camera ID samples that decoded stream at its configured rate and hands each sample to the detector before decoding the next frame, so the baseline is always the immediately previous sample. One change score and the corresponding full decoded frame reach eligible sensors. The LunaRoute adapter alone fits the image into the measured request budget; the scheduler owns cooldown and in-flight rules; the MQTT publisher owns discovery and availability.

**Tech Stack:** Python 3.12, uv, PyAV, NumPy, OpenCV headless, httpx, paho-mqtt, Pydantic 2, PyYAML, pytest, Ruff, mypy, Docker Compose, MediaMTX, Mosquitto, Home Assistant.

**Spec:** `docs/spec.md`. Doctor Biz approved transport resizing on 2026-09-25 because the measured LunaRoute request cap makes unconditional full-resolution delivery impossible.

## Now
- Step: deployed on docker-host (~/camera-sensors, `djev-sensors:latest` built from main): six sensors on four cameras, all online; the Mac copy is stopped
- Next: none
- Open: front yard change threshold (5% proposed); whether one model timeout should mark a sensor unavailable (spec section 19)
- Approved: "lets use subagents to build this project. it has been planned with superpowers" (2026-09-25) — execute this plan with subagent-driven-development
- Approved: "1" (2026-09-25) — finishing option 1, merge back to main locally
- Approved: "the old one isn't exposed. do 2, and 3" (2026-09-25) — run the live gates with the existing LunaRoute key (no rotation); fix the two before-shipping items
- Approved: "1" (2026-09-25) — finishing option 1 for `wip/live-gates-hardening`, merge back to main locally
- Approved: "sure. also push" (2026-09-25) — amend spec section 37 so a service restart keeps HA's last state; push `main` to origin
- Approved: "let's merge the rechecks, and deploy to docker host" (2026-09-26) — merge `wip/sensor-rechecks` into main; deploy to docker-host once its CPU allows
- Compactions: 0

## Global Constraints

- Only LunaRoute `POST https://gw.lunaroute.com/v1/systemone` may carry Djev requests; never fall back to direct Djev.
- Djev model name is `djev`; each sensor sends one Noul question named `result` in its own request.
- The image belongs in `questions.result.instructions.image`, with prompt text in `questions.result.instructions.text`. The gateway rejects top-level `images`.
- Serialize the exact request before dispatch and keep it at or below 45,000 bytes. Preserve source dimensions when they fit; otherwise send the largest fitted JPEG and publish `sent_width` and `sent_height`.
- Keep decoded frames in memory only. Never log images, RTSP credentials, model keys, or full request bodies.
- No startup inference, no model-generated prose, no FIFO frame backlog, no database, and no Home Assistant YAML.
- Sensor defaults: `true_threshold=0.80`, `change_threshold_pct=2.5`, `cooldown_seconds=10`. Camera default: `fps=1`. Global request limit: `max_concurrent_requests=4`.
- Discovery is retained. State messages are `ON` or `OFF` with `retain=false`. MQTT reconnect never republishes a prior state.
- Keep `test.sh` out of any commit until its literal token is removed. Rotate the exposed key before any live request. Stage named files only.
- Hand-written source and script files start with two `ABOUTME:` lines after any shebang. Commits use Conventional Commits. Use TDD for every feature and bug fix.
- Before declaring v0.1 complete, run the canonical check, a live LunaRoute image call with a rotated key, and an end-to-end run with actual RTSP, MQTT, and Home Assistant.

## File map

| Path | Responsibility |
|---|---|
| `pyproject.toml`, `uv.lock`, `scripts/check` | Dependencies and one local quality gate |
| `config.example.yaml`, `djev_sensors/config.py` | Operator config, defaults, env secrets, cross references |
| `djev_sensors/detectors/base.py`, `frame_difference.py` | Change detector contract and implementation |
| `djev_sensors/camera.py` | One RTSP decoder per URL, sampling, reconnect, frame delivery |
| `djev_sensors/models/base.py`, `djev_sensors/models/lunaroute_djev.py` | Model contract, request fitting, transport, response validation |
| `djev_sensors/state.py`, `scheduler.py` | Binary state, availability, per-sensor eligibility and concurrency |
| `djev_sensors/mqtt/discovery.py`, `client.py` | MQTT topics, discovery, Last Will, publications |
| `djev_sensors/app.py`, `__main__.py` | Wiring, lifecycle, logging, command line |
| `Dockerfile`, `compose.yaml`, `README.md` | Single-container deployment and operator instructions |
| `tests/unit/`, `tests/integration/`, `tests/e2e/` | Pure behavior, real local services, full product path |

The app owns one `CameraRuntime` per camera ID and one `SensorRuntime` per sensor ID. The RTSP hub owns one decoder per URL; camera IDs that share a URL keep separate sample clocks and change baselines. Passing a frame between units uses a NumPy BGR array plus a monotonic capture time. No unit besides the LunaRoute adapter knows its HTTP fields.

---

### Task 1: Project, configuration, and safe smoke script

**Files:** Create `pyproject.toml`, `uv.lock`, `scripts/check`, `config.example.yaml`, `djev_sensors/__init__.py`, `djev_sensors/config.py`, `tests/unit/test_config.py`. Modify `test.sh` only after replacing its literal key with `LUNAROUTE_API_KEY`.

**Interfaces:** Produce `load_config(path: Path, env: Mapping[str, str]) -> AppConfig`. `AppConfig.cameras` and `AppConfig.sensors` are maps keyed by validated IDs. The model and MQTT configs expose values needed by later tasks, never a logged secret.

- [ ] **Step 1: Initialize the project and tools.** Run `uv init --bare --name djev-sensors --no-workspace --vcs none`, `uv add av numpy opencv-python-headless httpx paho-mqtt pydantic pyyaml`, and `uv add --dev pytest ruff mypy types-PyYAML`. Set `requires-python = ">=3.12"`. Create `djev_sensors/`, `tests/unit/`, and `tests/integration/`; add an empty `djev_sensors/__init__.py` before test collection.
- [ ] **Step 2: Write failing config tests.** Cover defaults, two sensors on one camera, an undefined camera reference, invalid thresholds, missing `GARAGE_RTSP_URL`, missing `LUNAROUTE_API_KEY` and `MQTT_PASSWORD`, and a literal secret rejected in YAML. Use this concrete case:

```python
def test_sensor_defaults_and_camera_reference(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("""
system:
  mqtt: {host: localhost, password_env: MQTT_PASSWORD}
  model: {provider: lunaroute, model: djev, api_key_env: LUNAROUTE_API_KEY}
cameras:
  garage: {rtsp: "${GARAGE_RTSP_URL}"}
sensors:
  car: {name: Car, camera: garage, prompt: Is a car visible?}
""")
    env = {"GARAGE_RTSP_URL": "rtsp://localhost:8554/garage",
           "LUNAROUTE_API_KEY": "lr_test", "MQTT_PASSWORD": "test"}
    config = load_config(path, env)
    assert config.cameras["garage"].fps == 1
    assert config.sensors["car"].true_threshold == 0.80
    assert config.sensors["car"].camera == "garage"
```

- [ ] **Step 3: Run the failing test.** Run `uv run pytest tests/unit/test_config.py -q`. Expect a missing `load_config` failure.
- [ ] **Step 4: Implement config validation.** Implement typed Pydantic config models with `extra="forbid"`. Validate IDs with `[a-z0-9_]+`, `0.5 < true_threshold <= 1`, `0 < change_threshold_pct <= 100`, `fps > 0`, `cooldown_seconds >= 0`, `max_concurrent_requests >= 1`, detector width greater than zero, pixel delta in `[0, 255]`, timeout greater than zero, and odd nonnegative blur. Set the default prompt wrapper to the exact three sentences in spec section 16. Expand only exact `${NAME}` tokens in RTSP values; reject a literal RTSP URL containing a password. Resolve secret names from the supplied environment and fail before opening any camera or network connection. Populate `config.example.yaml` from spec section 5 with environment references, never values.
- [ ] **Step 5: Add the canonical check.** `scripts/check` takes no argument or `--live`, prints usage for anything else, and reports the failing command. The default runs `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy djev_sensors`, and `uv run pytest tests/unit tests/integration -q`. The `--live` mode also runs `uv run pytest tests/e2e -q`. Tests requiring local Docker services start and stop their own Compose test stack.
- [ ] **Step 6: Make `test.sh` safe.** Replace the embedded bearer token with `${LUNAROUTE_API_KEY:?set LUNAROUTE_API_KEY}` in the existing request; add a shebang, two `ABOUTME:` lines, `set -eu`, and an exit on non-2xx HTTP. Do not run it until the old key is rotated. Do not copy its ticket text into product tests.
- [ ] **Step 7: Run `scripts/check` and commit.** Expected: every default check exits zero. Stage the listed files by name, then `git commit -m "feat: add validated service configuration"`. The smoke script can enter that commit only after the key is gone and `rg 'lr_[a-f0-9]{32,}' test.sh` returns no match.

### Task 2: Frame difference and camera-level baseline

**Files:** Create `djev_sensors/detectors/base.py`, `djev_sensors/detectors/frame_difference.py`, `tests/unit/test_change_detection.py`.

**Interfaces:** `ChangeResult.changed_pct: float`. `FrameDifferenceDetector.prepare(frame: NDArray[np.uint8]) -> NDArray[np.uint8]` returns the reduced grayscale detection frame. `compare(previous, current) -> ChangeResult` never changes either input. The app stores and replaces the immediately previous prepared frame after every sample.

- [ ] **Step 1: Write failing tests.** Assert that identical frames score `0.0`; four changed pixels in a 10-by-10 prepared frame score `4.0` when their delta reaches the threshold; pixels one level below it do not count; and a three-frame sequence compares frame 3 against frame 2 even if frame 2 did not trigger. Assert output width 320 and aspect-preserving height for a 640-by-480 frame.
- [ ] **Step 2: Run `uv run pytest tests/unit/test_change_detection.py -q`.** Expect failure before implementation.
- [ ] **Step 3: Implement the exact calculation.** Convert BGR to grayscale, resize to configured width, apply `cv2.GaussianBlur` only when blur is positive, then calculate:

```python
delta = cv2.absdiff(previous, current)
changed_pct = float(np.count_nonzero(delta >= pixel_delta_threshold)) * 100.0 / delta.size
```

Reject mismatched prepared-frame shapes. Keep the baseline assignment in `app.py`, after `compare` and before scheduling, so eligibility cannot freeze it.
- [ ] **Step 4: Run `uv run pytest tests/unit/test_change_detection.py -q` and `scripts/check`.** Expect pass with no new warnings. Commit named files as `feat: add frame difference detection`.

### Task 3: Shared RTSP decoding, sampling, and reconnect

**Files:** Create `djev_sensors/camera.py`, `tests/unit/test_camera_sampling.py`, `tests/integration/test_rtsp_camera.py`, `tests/integration/compose.yaml`, and `tests/integration/mosquitto.conf`.

**Interfaces:** `FrameSample(camera_id: str, image: NDArray[np.uint8], captured_at: float)`. `CameraStreamHub.run(on_sample, on_status, stop_event) -> None` calls `on_sample(FrameSample)` at each camera ID's FPS and `on_status(camera_id, connected: bool)` on transitions. It has one decoder per distinct resolved URL.

- [ ] **Step 1: Write failing clock tests.** Feed decoded frames at 10 FPS to two camera IDs sharing one URL, one configured for 1 FPS and one for 2 FPS. Over two seconds, assert their sample counts differ as configured while decoder count stays one. Check the first frame after reconnect is a new baseline. Assert backoff values `1, 2, 4, 8, 16, 30, 30` seconds from a pure `reconnect_delay(attempt)` function.
- [ ] **Step 2: Run `uv run pytest tests/unit/test_camera_sampling.py -q`.** Expect failure before code.
- [ ] **Step 3: Implement the hub.** Group camera IDs by exact resolved URL. In one worker thread per URL use `av.open(url, options={"rtsp_transport": "tcp"}, timeout=(10.0, 10.0))` and `container.decode(video=0)`; convert each decoded frame with `to_ndarray(format="bgr24")`. Use monotonic clocks for independent camera sample intervals. Hand each due sample to the async app and wait only for its detector/baseline/scheduler-eligibility work to finish before reading the next decoded frame; model requests run in background. Do not queue or overwrite samples before comparison. Close containers and threads on stop. On open, decode, or timeout failure, mark all camera IDs on that URL offline, clear their detection baselines, log a redacted event, sleep the exponential delay, then reopen. A successful decoded frame marks them online and resets the retry count.
- [ ] **Step 4: Add a real RTSP integration test.** `tests/integration/compose.yaml` runs `bluenviron/mediamtx:1` with TCP RTSP and `eclipse-mosquitto:2` for later tasks. Bind host test ports `18554` and `18883` only to `127.0.0.1`; check both are free before starting the stack. `tests/integration/mosquitto.conf` permits anonymous access only in this isolated test stack. Generate a short H.264 clip and publish it with the MediaMTX documented FFmpeg RTSP command:

```sh
ffmpeg -y -f lavfi -i 'testsrc=size=320x240:rate=5' -t 4 -c:v libx264 -pix_fmt yuv420p /tmp/djev-rtsp-test.mp4
ffmpeg -re -stream_loop -1 -i /tmp/djev-rtsp-test.mp4 -c copy -f rtsp rtsp://127.0.0.1:18554/test
```

Observe sampled frames, stop the publisher, assert offline, restart it, and assert recovery without a second decoder for the same URL.
- [ ] **Step 5: Run `uv run pytest tests/unit/test_camera_sampling.py tests/integration/test_rtsp_camera.py -q` and `scripts/check`.** Expect pass. Commit named files as `feat: share RTSP decoders and recover streams`.

### Task 4: LunaRoute image fitting and typed Noul transport

**Files:** Create `djev_sensors/models/base.py`, `djev_sensors/models/lunaroute_djev.py`, `tests/unit/test_model_request.py`, `tests/integration/test_lunaroute_contract.py`.

**Interfaces:** `ModelClient.evaluate_binary(image: NDArray[np.uint8], instructions: str) -> BinaryJudgment` is async. `BinaryJudgment` has `true_probability`, `sent_width`, `sent_height`, and `latency_ms`. `ModelError` represents transport, auth, provider, capacity, or image-fit failure; `InvalidModelResponse` represents a successful HTTP response with invalid Noul content. The scheduler treats these differently.

- [ ] **Step 1: Write failing pure request tests.** Build a patterned 640-by-480 BGR frame. Assert the emitted JSON has `model="djev"`, `state=""`, exactly one `result` Noul question, image data in `questions.result.instructions.image`, no top-level `images`, and a prompt in `instructions.text`. Decode the JPEG data URL and check its dimensions. Test a highly compressible frame stays 640-by-480; a noisy frame shrinks; every complete serialized body is at most 45,000 bytes. A frame that cannot fit even at the minimum candidate size raises `ModelError` before HTTP.
- [ ] **Step 2: Run `uv run pytest tests/unit/test_model_request.py -q`.** Expect failure before code.
- [ ] **Step 3: Implement fitting and transport.** Try the source dimensions only when its longest side is at most 2048; otherwise start at max side 2048. Then try decreasing max sides (`1600, 1280, 1024, 896, 768, 640, 512, 448, 384, 320, 256, 192`) without enlarging the source. At each size try JPEG qualities `85, 75, 65, 55`. Pick the first candidate whose full compact UTF-8 JSON body is at most 45,000 bytes; do not send a candidate based on JPEG bytes alone. POST those exact bytes to `https://gw.lunaroute.com/v1/systemone` with bearer auth and the configured timeout. Do not log the body or Authorization header. Parse only `answers.result.type == "noul"` and a finite non-boolean numeric `answers.result.noul` in `[0, 1]`. Raise `InvalidModelResponse` for a 2xx response that fails this parse; raise `ModelError` for network, timeout, non-2xx, and fit failures.
- [ ] **Step 4: Test the wire boundary.** A local HTTP server receives a real request from `LunaRouteDjevClient` and checks its route, auth header shape, byte count, image MIME, and single question. Return one valid Noul body and one malformed 2xx body to verify the two result paths. This tests serialization and parsing, rather than asserting calls to a mock.
- [ ] **Step 5: Run a live contract probe.** With a newly rotated `LUNAROUTE_API_KEY` in the environment, send one small, nonprivate image through the adapter and assert a numeric Noul result. Run `uv run pytest tests/unit/test_model_request.py tests/integration/test_lunaroute_contract.py -q` and `scripts/check`. If the gateway contract has changed, update only this adapter and its contract tests. Commit named files as `feat: evaluate camera frames through LunaRoute`.

### Task 5: Sensor state, cooldown, and scheduling

**Files:** Create `djev_sensors/state.py`, `djev_sensors/scheduler.py`, `tests/unit/test_sensor_state.py`, `tests/unit/test_scheduler.py`.

**Interfaces:** `resolve_state(previous: bool | None, probability: float, threshold: float) -> bool | None`. `SensorScheduler.on_change(camera_id, frame, changed_pct, camera_generation)` schedules one task per eligible sensor. `SensorScheduler.camera_status(camera_id, online)` changes the camera availability bit and generation. Its output is typed events for the MQTT publisher, never MQTT calls from model code.

- [ ] **Step 1: Write failing state tests.** With threshold `.8`, assert `.91 -> ON`, `.09 -> OFF`, `.55 -> prior state`, and equality at `.8`/`.2` transitions. An uncertain first inference leaves state unknown. A malformed 2xx response forces `OFF` and emits `parse_error=true` even if the prior state was OFF. Model failure retains internal state and emits offline availability.
- [ ] **Step 2: Write failing scheduling tests.** At change `3%`, a sensor with `2%` threshold runs and one with `5%` does not. Two eligible sensors produce two independent model requests. Repeated qualifying frames within the cooldown or while in flight produce no second request. With concurrency one, two different sensors run serially. The cooldown starts only after a semaphore slot is acquired. An inference result from an earlier camera generation is discarded after disconnect.
- [ ] **Step 3: Run `uv run pytest tests/unit/test_sensor_state.py tests/unit/test_scheduler.py -q`.** Expect failure before code.
- [ ] **Step 4: Implement the state machine and scheduler.** Use `asyncio.Semaphore(max_concurrent_requests)` and one `inference_in_flight` bit per sensor. Do not keep a FIFO or pending frame in v0.1. Initialize binary state to `None` and model availability to true until the first model failure; camera availability begins false. Use a monotonic clock for cooldown and UTC for `evaluated_at`. Compose the wrapper and sensor prompt with one blank line. Publish attributes after every completed inference; for valid results include probability, threshold, change percentage, camera ID, model, UTC time, latency, and sent dimensions. Publish state only when it changes, except the specified malformed-response OFF. Recompute availability as `camera_available and model_available` after each status change. Emit named structured events without secrets or image bytes.

```python
def resolve_state(previous: bool | None, probability: float, threshold: float) -> bool | None:
    if probability >= threshold:
        return True
    if probability <= 1.0 - threshold:
        return False
    return previous
```
- [ ] **Step 5: Run the focused tests and `scripts/check`.** Expect pass. Commit named files as `feat: schedule and resolve semantic sensors`.

### Task 6: MQTT Discovery, availability, and reconnect

**Files:** Create `djev_sensors/mqtt/discovery.py`, `djev_sensors/mqtt/client.py`, `tests/unit/test_mqtt_discovery.py`, `tests/integration/test_mqtt_publishing.py`.

**Interfaces:** `discovery_topic(sensor, config) -> str`, `discovery_payload(sensor, config) -> dict`. `MqttPublisher.publish_discovery()`, `publish_state(sensor_id, state)`, `publish_attributes(sensor_id, attrs)`, `publish_availability(sensor_id, online)`. On reconnect it republishes discovery and current availability, never state.

- [ ] **Step 1: Write failing discovery tests.** Check topic `homeassistant/binary_sensor/djev_sensors/gate_open/config` with default prefix; `unique_id=djev_sensors_gate_open`; configured name, device class, state and JSON attribute topics; payload strings; and the shared device identifier. Check custom discovery and topic prefixes.
- [ ] **Step 2: Write failing broker integration tests.** Subscribe to actual Mosquitto topics. Assert discovery has `retain=true`; state `retain=false`; availability payloads are `online`/`offline`; attributes carry valid JSON. Disconnect and reconnect the publisher and assert discovery/availability return while no old state arrives. Kill the client connection and assert the shared Last Will makes the entity unavailable.
- [ ] **Step 3: Run `uv run pytest tests/unit/test_mqtt_discovery.py tests/integration/test_mqtt_publishing.py -q`.** Expect failure before code.
- [ ] **Step 4: Implement with Paho callback API v2.** Set a retained Last Will `<topic_prefix>/service/availability = offline` before connect. Publish `online` on successful connect. Discovery uses two availability topics: the per-sensor topic and the shared service topic, with `availability_mode="all"`. Retain per-sensor and service availability so Home Assistant sees correct status after its own restart. On initial MQTT connect publish sensor availability offline until camera status is known. Publish state at QoS 0 only while connected, with no application retry queue; a disconnected state event is logged and dropped. Paho reconnect callback publishes discovery and recomputed availability; it never calls `publish_state`. Check publish results and log failures.

```python
payload["availability"] = [
    {"topic": f"{topic_prefix}/{sensor_id}/availability"},
    {"topic": f"{topic_prefix}/service/availability"},
]
payload["availability_mode"] = "all"
```
- [ ] **Step 5: Run focused tests and `scripts/check`.** Expect pass. Commit named files as `feat: publish Home Assistant MQTT sensors`.

### Task 7: Application loop and lifecycle

**Files:** Create `djev_sensors/app.py`, `djev_sensors/__main__.py`, `tests/integration/test_pipeline.py`.

**Interfaces:** `Application(config, camera_hub, detector, scheduler, publisher).run(stop_event) -> None`. The CLI is `uv run python -m djev_sensors --config config.yaml`.

- [ ] **Step 1: Write a failing pipeline test.** Feed three real frames through the camera sampling boundary: baseline, unchanged, then changed. Assert no model request until the third sampled frame. Give two sensors the same camera and different thresholds; assert one camera score is used for both and one model request per eligible sensor. Verify the model gets the corresponding source frame, not the reduced detection frame. Include camera failure/recovery while model availability remains false.
- [ ] **Step 2: Run `uv run pytest tests/integration/test_pipeline.py -q`.** Expect failure before wiring.
- [ ] **Step 3: Wire the application.** Start MQTT, publish discovery, create one camera runtime per configured ID, start the shared RTSP hub, set a baseline on the first sample, and replace the baseline after every later sample. Pass each change result to the scheduler. On camera disconnect, clear baseline and mark attached sensors unavailable. On SIGINT/SIGTERM, stop decoders, await in-flight work with a bounded shutdown, publish service offline, disconnect MQTT, and close httpx. Log the event names from spec section 33 as structured JSON. Report count-based metrics from those events; do not add a Prometheus endpoint.
- [ ] **Step 4: Run `uv run pytest tests/integration/test_pipeline.py -q` and `scripts/check`.** Expect pass. Commit named files as `feat: connect camera to sensor pipeline`.

### Task 8: Container, operator docs, and product end-to-end gate

**Files:** Create `Dockerfile`, `.dockerignore`, `compose.yaml`, `README.md`, `tests/e2e/compose.yaml`, `tests/e2e/test_home_assistant.py`. Update `scripts/check` to call the live test under `--live`.

**Interfaces:** The container starts `python -m djev_sensors --config /app/config.yaml`. Compose mounts config read-only and passes `LUNAROUTE_API_KEY`, MQTT password, and RTSP URL environment variables. No persistent volume.

- [ ] **Step 1: Write the failing end-to-end test and its stack.** Create `tests/e2e/compose.yaml` with MediaMTX, Mosquitto, and `ghcr.io/home-assistant/home-assistant:stable`; boot it and configure Home Assistant's MQTT integration through its UI setup flow, with no sensor YAML. Use a dedicated retained discovery prefix. Publish an RTSP clip whose frames switch from black to white while the connection stays open; ask whether the frame is mostly white with `true_threshold=.55`. Start the service with a rotated LunaRoute key. Verify Home Assistant creates the discovered binary sensor, receives probability and sent dimensions as attributes, and moves state after a confident Noul result. Stop the RTSP publisher and confirm unavailable; restore it and confirm recovery. Drop MQTT and confirm the shared Last Will and reconnection behavior. E2E uses real services and the real model, with no mock mode.
- [ ] **Step 2: Run `scripts/check --live`.** Expect the new end-to-end test to fail before deployment wiring.
- [ ] **Step 3: Add deployment and documentation.** Build a single Python 3.12 container with uv-managed dependencies and `uv.lock`. The Dockerfile uses the following build and command; `.dockerignore` excludes `.venv`, `.git`, private config, `test.sh`, and test artifacts:

```dockerfile
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.25 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY djev_sensors ./djev_sensors
CMD ["uv", "run", "--no-sync", "python", "-m", "djev_sensors", "--config", "/app/config.yaml"]
```

Add the spec's two-camera config example, default thresholds, image-fitting rule, unknown state on restart, non-retained state, availability rules, troubleshooting for RTSP/model/MQTT, and a safe key-rotation note. Document `scripts/check` and `scripts/check --live` with prerequisites and exit behavior.
- [ ] **Step 4: Run all gates.** Run `scripts/check`, `scripts/check --live`, `docker compose config`, `docker compose build`, and a real container startup with read-only config. Expect zero warnings introduced by this work. Inspect Home Assistant state and attributes, not only MQTT packets. Commit named files as `feat: package and verify RTSP sensors`.
- [ ] **Step 5: Review and hand off.** Run the fresh-eyes review skill, fix findings, rerun affected checks, then create a PR from the WIP branch. Include the measured gateway limit and image-size tradeoff in the PR summary. Do not merge without Doctor Biz's direction.

## Spec coverage review

- Sections 5–9: Tasks 1–3 cover config, shared streams, sampling, detector contract, and immediate prior-frame baseline.
- Sections 10–21: Tasks 4–5 cover independent Noul requests, cooldown, in-flight cap, uncertainty, malformed responses, model and camera availability, and recovery.
- Sections 22–27: Task 6 covers topics, discovery, retained config, non-retained state, availability, Last Will, and reconnect without state replay.
- Sections 28–34: Task 7 covers runtime state, the camera loop, attributes, structured events, and log-derived metrics.
- Sections 35–40: Task 8 covers packaging, Docker, documented operation, and every acceptance path.
- Measured LunaRoute behavior comes from `test.sh` plus `../mm-decisions/gotchas.md`. Revalidate it with a rotated key before treating v0.1 as shipped.

## Research references

- [Djev request and image limits](https://api.djev.dev/docs)
- [Home Assistant MQTT binary sensors](https://www.home-assistant.io/integrations/binary_sensor.mqtt/)
- [Home Assistant MQTT Discovery](https://www.home-assistant.io/integrations/mqtt/)
- [Paho Python callback API](https://eclipse.dev/paho/clients/python/docs/)
- [MediaMTX container and FFmpeg publishing](https://mediamtx.org/docs/kickoff/install)

## Execution rulings

Decisions made while executing this plan, copied from the SDD ledger in order. Each names what it costs if wrong.

- Ruling: work on the existing `wip/djev-sensors-plan` branch in this checkout, no worktree — CLAUDE.md says day-to-day work never uses worktrees and user instructions override the skill — cost if wrong: none, branch is already isolated from main.
- Ruling: pin Python 3.12 with `.python-version` (system python3 is 3.14) so local runs match the `python:3.12-slim` container — cost if wrong: one file to change.
- Ruling: `LUNAROUTE_API_KEY` is not in the environment and the only known keys are test.sh's exposed key and the tracker `.env` key; no live request uses the exposed key. Rotation is Doctor Biz's action. Live steps (Task 4 step 5, Task 8 live gate) run only with a key that is not the exposed one — cost if wrong: the live steps wait for Doctor Biz.
- Pre-flight (T1 → T2): T1 says "odd nonnegative blur", T2 says "blur only when positive"; 0 is not odd. Ruling: blur is 0 or a positive odd integer — 0 disables blur, OpenCV needs odd positive kernels — cost if wrong: one validator.
- Pre-flight (T1 → T4): Consistent. Ruling: the gateway URL is a constant in the adapter with a constructor `base_url` override used only by the wire-boundary test; config never exposes it (only LunaRoute may carry requests) — cost if wrong: one parameter.
- Pre-flight (T1 → T5): ` scalars keep a trailing newline, so "one blank line" needs stripped ends. Ruling: config strips leading/trailing whitespace from `prompt` and `prompt_wrapper` and rejects an empty prompt; the scheduler joins with `"\n\n"` — cost if wrong: two validators.
- Pre-flight (T3 → T7): Ruling: the hub owns no baselines. It calls `on_status(camera_id, False)` before any reconnect attempt and `on_status(camera_id, True)` after the first decoded frame; the app clears the baseline on the offline status. `on_sample` is a plain callable invoked on the decoder thread that returns only after the app finished detector, baseline, and eligibility work (`asyncio.run_coroutine_threadsafe(...).result()` on the app side) — cost if wrong: one callback contract.
- Pre-flight (T3/T5 → T7): Ruling: the scheduler owns per-camera generation, bumps it in `camera_status(camera_id, False)`, exposes `camera_generation(camera_id) -> int`; the app passes that value into `on_change` (one source of truth, plan signature kept) — cost if wrong: one accessor.
- Pre-flight (T5 → T6 → T7): Ruling: T5 defines a `SensorPublisher` Protocol — `publish_state(sensor_id, state: bool)`, `publish_attributes(sensor_id, attrs: dict)`, `publish_availability(sensor_id, online: bool)`; T6's `MqttPublisher` satisfies it structurally and renders `ON`/`OFF`, `online`/`offline`; T7 passes the publisher straight in — cost if wrong: an adapter of three lines.
- Pre-flight (T3, T5, T7): Ruling: T3 creates `djev_sensors/events.py` with `log_event(event, **fields)` that logs one JSON object per event; T7 configures the root logger for JSON lines — cost if wrong: one small module.
- Pre-flight (T1): Consistent. Ruling: `username` optional; `timeout_seconds` default 15; `inference` and `change_detection` sections optional with spec 32 defaults; `provider` is Literal `lunaroute`, `model` is Literal `djev`; `device_class` is an optional free string (HA validates) — cost if wrong: validators.
- Ruling: mypy strict and pytest `filterwarnings=error` for the whole project — enforces the no-new-warnings rule mechanically — cost if wrong: later tasks spend turns on stub casts; relax per-module.
- Ruling: sent Doctor Biz one terminal notification asking for key rotation (the tracker `OPENAI_COMPAT_API_KEY` equals test.sh's exposed key) — only Doctor Biz can rotate; early notice lets live gates run this session — cost if wrong: one unneeded notification.
- Task 1: Ruling: an empty `prompt_wrapper` is allowed (spec 16 calls the wrapper optional); Task 5 composes instructions as the prompt alone when the wrapper is empty, and as `wrapper + "\n\n" + prompt` otherwise — cost if wrong: one branch in prompt composition.
- Task 2: Ruling: preprocessing order follows the plan (grayscale, resize, blur) rather than spec section 8's diagram (resize first) — the orders agree up to uint8 rounding and the spec's intent is a reduced blurred grayscale frame — cost if wrong: swap two lines.
- Task 3: Ruling: accept the hub setting the caller's `stop_event` on a callback bug — Task 7 passes the app's shutdown `threading.Event`, so a callback bug stops the service and `run()` re-raises, and the process exits non-zero for the container restart policy — cost if wrong: switch to a hub-private event.
- Task 3: Ruling: fix the reviewer's plan-mandated Minor now — convert a decoded frame to BGR only when some camera ID on that URL is due, once per frame, shared by the due IDs. Brief step 3 and my ruling 11 said "convert each decoded frame"; spec section 1 requires watching streams cheaply and section 7 says change detection need not run on every decoded frame, so the spec wins — cost if wrong: one seam change in camera.py and its tests.
- Task 3: Ruling: stalled-open shutdown can take ~20 s (PyAV applies the open timeout twice). Worker threads are daemon threads (camera.py:144). Task 7 must bound its wait for `hub.run()` and finish shutdown (service offline, MQTT disconnect, httpx close) without it — cost if wrong: a slower stop.
- Task 3: Ruling: any mid-stream decode error takes the URL offline and reconnects (brief step 3 mandates it); watch for flapping in live testing — cost if wrong: availability flaps on cameras with corrupt packets.
- Task 3: Ruling: accept the implementer's extra change in fix round 1 — a failed BGR conversion is a stream failure (offline, reconnect), and a camera goes online only after its first frame converts — it keeps conversion errors on the same path as decode errors instead of crashing the hub as a callback bug — cost if wrong: one branch in camera.py.
- Task 4: Ruling: the live LunaRoute probe lives in `tests/e2e/test_lunaroute_live.py` (run by `scripts/check --live`) instead of `tests/integration/test_lunaroute_contract.py` — the default check must not spend a paid API call or need a secret, and the plan's own check split puts live dependencies under --live — cost if wrong: move one test file.
- Task 4: Ruling: fitting runs through `asyncio.to_thread` — up to 52 JPEG encodes would otherwise block the event loop that processes camera samples — cost if wrong: none functionally.
- Task 4: Ruling: `ModelError` and `InvalidModelResponse` are sibling exceptions (neither subclasses the other) — prevents a broad except from turning a malformed response into unavailability — cost if wrong: none.
- Task 5: Ruling: `on_change(camera_id, frame, changed_pct)` drops the plan's `camera_generation` argument; the scheduler captures its own per-camera generation at scheduling time — one source of truth, and the hub serializes a camera's samples and status changes through the loop so no stale sample can arrive after a status change — cost if wrong: re-add one parameter.
- Task 5: Ruling: `resolve_state` uses `probability + threshold <= 1.0` instead of the brief's literal `probability <= 1.0 - threshold` — in floats `1.0 - 0.8` is 0.19999999999999996, so the literal form keeps p=0.2 in the band and fails the brief's own boundary test; spec 17's inclusive `p <= 1 - t` is the binding intent — cost if wrong: one line and one test.
- Task 5: Ruling: an evaluation skipped because its camera generation changed starts no cooldown (spec 11: the cooldown starts when a request is dispatched) — cost if wrong: one branch.
- Task 5: Ruling: include the gotchas.md count correction in fix round 1 even though the reviewer graded it Minor — the file is read by collaborators and other agents, and CLAUDE.md says fix broken things in our path — cost if wrong: one doc line in the re-review surface.
- Task 6: Ruling: discovery adds `"default_entity_id": "binary_sensor.<sensor_id>"` — spec 40 expects `binary_sensor.someone_at_door`, HA prefixes MQTT entity names with the device name, and HA's MQTT docs (fetched 2026-09-25) name `default_entity_id` for this and no longer mention `object_id` — cost if wrong: entity IDs gain a device-name prefix; Task 8's real HA run checks it.
- Task 6: Ruling: attributes publish like state (QoS 0, not retained, dropped while disconnected) — retained attributes would outlive non-retained state after an HA restart — cost if wrong: one flag.
- Task 6: Ruling: per-sensor availability publishes at QoS 0 (still retained), amending Task 6 ruling 5 — paho resends unacknowledged QoS 1 messages after `on_connect` returns, so a stale `offline` could overwrite the replayed `online`; the connect-time replay of cached availability already covers what QoS 1 added. Discovery and service availability stay QoS 1 (a resend repeats identical content) — cost if wrong: one argument.
- Task 6: Ruling: accept the implementer's extras (package `__init__.py`, `mqtt_address` fixture, `mqtt.connect_failed` event so bad host or credentials don't retry silently) — cost if wrong: small removals.
- Task 7: Ruling: `Application(config, camera_hub, detector, scheduler, publisher, model)` adds `model` so httpx close lives with the rest of the lifecycle — cost if wrong: one parameter.
- Task 7: Ruling: the hub runs on a dedicated daemon thread, never `asyncio.to_thread` — Python 3.12's `asyncio.run` waits up to 300 s (`THREAD_JOIN_TIMEOUT`, confirmed in installed 3.12.12) for default-executor threads — cost if wrong: none.
- Task 7: Ruling: shutdown bounds hub 3 s, drain 3 s, then publisher stop and model close, to fit Docker's 10 s stop grace — cost if wrong: tune constants.
- Task 7: Ruling: log `frame.change` only when the score reaches the camera's lowest sensor threshold; metrics stay log-derived with no counters or periodic summary (spec 34 allows log-derived; README maps them) — cost if wrong: ~30 lines for a periodic summary.
- Task 7: Ruling: the app waits for the first successful MQTT connect (discovery published) before starting the camera hub, interruptible by stop — spec 27 orders discovery before connecting cameras, and a first state computed while the broker is down would be dropped — cost if wrong: startup waits on the broker.
- Task 7: Ruling: a stop not caused by a signal is abnormal: the process exits 1 even when the hub's re-raise misses the 3 s bound, and the hub logs a callback failure with its traceback when it happens, not only when `run()` re-raises — otherwise a bug during another camera's stalled open exits 0 with no traceback — cost if wrong: a few lines in camera.py and the CLI.
- Task 7: Ruling: worst-case shutdown (~11 s when hub, drain, and paho connect timeouts coincide) is handled in Task 8 by `stop_grace_period: 15s` in compose.yaml rather than tighter bounds — cost if wrong: Docker SIGKILLs and the Last Will covers availability.
- Task 7: Ruling: accept the implementer's additions — a camera resolution change starts a new baseline instead of crashing; callbacks after stop are ignored; CLI tests in `tests/integration/test_cli.py`; no explicit `publish_discovery()` because `start()` publishes discovery on every connect — cost if wrong: small edits.
- Task 8: Ruling: split the e2e into model-free tests (static clip, fake never-sent key: discovery and entity ID, availability, RTSP loss and recovery, Last Will via SIGKILL, broker restart replay) and one model test (black-to-white clip, real rotated key, fails when unset) — lets everything but the model path run now without touching the exposed key — cost if wrong: test reorganization.
- Task 8: Ruling: keep HA's default discovery prefix; Mosquitto runs without persistence and fresh per run, which gives the isolation the plan's "dedicated retained discovery prefix" aimed at — cost if wrong: an options-flow step.
- Task 8: Ruling: no PR — the repository has no git remote; finishing options go to Doctor Biz — cost if wrong: none.
- Task 8: Ruling: the SDD final whole-branch review, run with the fresh-eyes checklist, is the plan's step-5 fresh-eyes review — CLAUDE.md says review passes are never multiplied — cost if wrong: one extra review pass later.
- Final review: Ruling: the fix wave (final-fix-findings.md, F1-F18) covers all 4 Important, the 4 fix-before-merge minors, the final review's minors (YAML snippet echo, non-root container, empty sensors, test.sh pipefail, .env mode note, stale 10 s comment), the README bundle, and cheap minors in files the wave already touches (inf floats, UnicodeDecodeError, `password_env: ""`, malformed-noul parametrized test, e2e teardown, bearer-never-logged test) — one dispatch, per the process — cost if wrong: a larger fix diff to re-review.
- Final review: parked — Task 1: partial-token password test; test.sh mode 100755 — Ruling: can wait; anchored regex handles the partial token by construction, and the mode is harmless.
- Final review: parked — Task 3: compose down before free-port check; stop not checked between camera IDs; writable shared array — Ruling: can wait; concurrent runs on one machine are rare, the app ignores callbacks after stop, and every consumer only reads the frame.
- Final review: parked — Task 4: `# type: ignore[attr-defined]` in the contract test server — Ruling: can wait; test-only style.
- Final review: parked — Task 5: drain swallows non-cancellation exceptions; cancel-before-first-step in-flight bit; docstring overstatement — Ruling: can wait; only a bug reaches the swallowed path and asyncio still reports it at GC, and the bit case occurs only at shutdown.
- Final review: parked — Task 6: stop during a reconnect handshake can leave service `online`; no-retry-queue test via logs only; docstring scope — Ruling: can wait; the race needs a same-second broker return during stop, and `_publish_event` never hands paho a message while disconnected.
- Final review: parked — Task 7: drain-bound test ambiguity; loop-side stop check; duplicated test helpers — Ruling: can wait; a late evaluation is bounded by the 3 s drain.
- Final review: parked — Task 8: broker-restart "no state" half cannot fail; e2e selection by test name — Ruling: can wait; the integration test covers state replay with an exact list.
- Final fix wave: parked — image now depends on host file modes (COPY keeps modes; a 027/077 umask checkout makes source unreadable to uid 10001) (Dockerfile:6, :10-12) — Ruling: real, not load-bearing; default umask 022 works; recommend `COPY --chmod` or `chmod -R a+rX` before shipping.
- Final fix wave: parked — the literal secret survives in the chained cause behind `ConfigError` (ValidationError input_value; MarkedYAMLError snippet) (config.py:325-327, :333-335) — Ruling: real, not load-bearing; the CLI prints only the message; recommend `from None` on both raises before shipping.
- Final fix wave: parked — non-string `rtsp`, `password_env`, or `api_key_env` raises TypeError that escapes `ConfigError` (exit 1 instead of 2) (config.py:51-55, :92-93) — Ruling: can wait; operator gets a traceback instead of a clean message.
- Final fix wave: parked — compose.yaml:9-10 comment lists 3+3+2 s and omits the 5 s connect case — Ruling: can wait; not false.
- Ruling: keep this SDD workspace until the live gates pass — v0.1 is not done without them, and the workspace holds the exposed-key fingerprint used to verify rotation and the per-task reports — cost if wrong: a git-ignored directory lingers.
