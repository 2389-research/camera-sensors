# Djev RTSP → Home Assistant Binary Sensors

## 1. Purpose

Build a small headless service that turns arbitrary RTSP camera streams into semantic Home Assistant binary sensors using Djev multimodal inference routed through LunaRoute.

Examples:

- `binary_sensor.gate_open`
- `binary_sensor.car_in_garage`
- `binary_sensor.people_at_table`
- `binary_sensor.people_on_couch`
- `binary_sensor.someone_at_door`

The service continuously watches configured RTSP streams cheaply.

It **does not continuously run inference**.

Instead:

```text
RTSP
  ↓
sample @ configured FPS
  ↓
cheap local frame-difference detector
  ↓
enough visual change?
  ├── no → continue
  └── yes
       ↓
   evaluate eligible sensors
       ↓
   Djev + largest frame that fits LunaRoute
       ↓
   probability
       ↓
   sensor state decision
       ↓
   MQTT
       ↓
   Home Assistant
```

The central design principle is:

> **Pixels locally decide when something might have changed. Djev decides what the change means.**

---

# 2. Goals

The system MUST:

1. Consume one or more RTSP streams.
2. Share a single RTSP connection when multiple sensors reference the same stream.
3. Sample each camera at a configurable FPS.
4. Perform cheap local visual-change detection.
5. Compute one camera-level change score for each sampled frame.
6. Allow each semantic sensor to have its own change threshold.
7. Trigger one independent Djev request per eligible sensor.
8. Keep the full-resolution camera frame locally and send the largest image that fits LunaRoute's request limit to Djev.
9. Convert Djev's Noul probability into Home Assistant binary state.
10. Publish using Home Assistant MQTT Discovery.
11. Publish useful inference metadata as entity attributes.
12. Mark entities unavailable when their RTSP stream or Djev evaluation path fails.
13. Recover automatically from camera/network/model failures.
14. Require no Home Assistant YAML configuration.

---

# 3. Non-goals

v0.1 does NOT need:

- object tracking
- bounding boxes
- face recognition
- persistent video recording
- clip analysis
- temporal model input
- multi-frame Djev inference
- ROIs
- batching several semantic sensors into one model request
- a web UI
- database persistence
- historical state reconstruction
- model-generated prose
- Home Assistant REST/WebSocket APIs

These can be added later without changing the fundamental sensor abstraction.

---

# 4. High-level architecture

```text
┌─────────────────────────────────────────────┐
│                 Application                 │
│                                             │
│  Config                                     │
│    ↓                                        │
│  Camera Registry                            │
│    ↓                                        │
│  RTSP Workers ──→ Frame Sampler             │
│                       ↓                     │
│                Change Detector              │
│                       ↓                     │
│                Sensor Scheduler             │
│                       ↓                     │
│                  Djev Client                │
│                       ↓                     │
│                State Resolver               │
│                       ↓                     │
│                 MQTT Publisher              │
└─────────────────────────────────────────────┘
                          ↓
                    MQTT Broker
                          ↓
                  Home Assistant
```

Recommended reference implementation:

```text
Python 3
PyAV / FFmpeg      RTSP decode
NumPy/OpenCV       frame comparison
httpx              model HTTP client
paho-mqtt          MQTT
Pydantic           configuration validation
PyYAML             configuration
asyncio            orchestration
```

The model integration MUST live behind a transport interface so LunaRoute-specific behavior does not leak into camera, state, or MQTT code.

---

# 5. Configuration

Configuration SHOULD be YAML.

Example:

```yaml
system:
  mqtt:
    host: mqtt.home
    port: 1883
    username: homeassistant
    password_env: MQTT_PASSWORD

    discovery_prefix: homeassistant
    topic_prefix: djev-sensors
    client_id: djev-sensors

  model:
    provider: lunaroute
    model: djev

    api_key_env: LUNAROUTE_API_KEY

    timeout_seconds: 15

    prompt_wrapper: |
      Evaluate only the visible contents of the supplied camera frame.
      Answer the requested binary condition based only on visible evidence.
      Do not assume facts that cannot be seen.

  inference:
    max_concurrent_requests: 4

  change_detection:
    type: frame_difference
    width: 320
    pixel_delta_threshold: 20
    blur: 5

cameras:
  garage:
    rtsp: ${GARAGE_RTSP_URL}
    fps: 1

  backyard:
    rtsp: ${BACKYARD_RTSP_URL}
    fps: 1

sensors:
  car_in_garage:
    name: Car in Garage
    camera: garage

    prompt: |
      Is a car currently parked inside the garage?

    device_class: occupancy

    true_threshold: 0.80
    change_threshold_pct: 2.5
    cooldown_seconds: 10

  gate_open:
    name: Gate Open
    camera: backyard

    prompt: |
      Is the backyard gate visibly open?

    device_class: opening

    true_threshold: 0.85
    change_threshold_pct: 1.5
    cooldown_seconds: 5
```

Secrets MUST be sourced from environment variables rather than committed configuration.

---

# 6. Camera model

A camera is identified by its configured camera ID.

Multiple sensors may reference the same camera.

Only **one RTSP connection** may exist for a camera.

Conceptually:

```python
Camera:
    id
    rtsp_url
    fps

    latest_full_frame
    previous_detection_frame
    change_score

    connection_state
```

If two camera entries resolve to exactly the same RTSP URL, implementations SHOULD deduplicate them into a shared connection.

---

# 7. Frame sampling

The RTSP worker continuously decodes the stream.

Change detection does **not** need to run against every decoded frame.

Instead it samples according to:

```text
camera.fps
```

Example:

```yaml
fps: 1
```

means approximately one frame comparison each second.

The full-resolution decoded frame MUST remain available until inference is dispatched. The transport uses it at full size when the request fits; otherwise it resizes and JPEG-encodes only the transmitted image.

A separate reduced frame is generated for change detection.

---

# 8. Change detection v0.1

The initial detector is deliberately simple.

For each sampled frame:

```text
full frame
    ↓
resize to configured detector resolution
    ↓
grayscale
    ↓
optional blur
    ↓
absolute difference vs immediately previous sampled frame
    ↓
threshold changed pixels
    ↓
changed pixels / total pixels
    ↓
change percentage
```

Pseudo-code:

```python
diff = abs(current - previous)

changed = diff >= pixel_delta_threshold

change_pct = (
    changed_pixel_count /
    total_pixel_count
) * 100
```

The comparison baseline MUST always be:

> **the immediately previous sampled frame**

—not the previous frame that triggered inference.

After calculating the score:

```python
previous_frame = current_frame
```

regardless of whether inference is triggered.

---

# 9. Pluggable change detectors

The detector MUST have a small interface such as:

```python
class ChangeDetector:
    def compare(
        self,
        previous_frame,
        current_frame,
    ) -> ChangeResult:
        ...
```

With:

```python
class ChangeResult:
    changed_pct: float
```

v0.1 implements:

```text
frame_difference
```

Future implementations could include:

```text
mog2
knn
optical_flow
scene_embedding
camera_native_motion_event
```

without modifying sensor logic.

---

# 10. Sensor triggering

Each camera produces **one shared change score**.

Every sensor attached to that camera compares that same score against its own threshold.

Example:

```text
camera change = 3.2%

gate_open threshold = 1%
car_present threshold = 5%
```

Result:

```text
gate_open     → evaluate
car_present   → skip
```

Eligibility:

```python
eligible = (
    change_pct >= sensor.change_threshold_pct
    and
    cooldown_expired(sensor)
)
```

There is no inference at application startup.

The first sampled frame establishes the comparison baseline.

Only a subsequent visual change can cause inference.

---

# 11. Cooldowns

Cooldowns are per sensor.

Example:

```yaml
cooldown_seconds: 10
```

A sensor cannot begin another model evaluation until its cooldown expires.

The cooldown starts when a model request is dispatched.

Default:

```text
10 seconds
```

---

# 12. In-flight inference

There MUST be at most one active Djev request for a given sensor.

A global concurrency limiter SHOULD also prevent excessive simultaneous model requests.

Default:

```yaml
max_concurrent_requests: 4
```

If another qualifying frame appears while a sensor evaluation is active:

```text
do not create another simultaneous request
```

The implementation MAY retain the newest qualifying frame as pending, replacing any older pending frame.

Older frames are never queued FIFO.

This matches Djev's documented live-frame pattern of keeping one frame request in flight and preferring the newest frame rather than building a stale queue.

---

# 13. Djev question type

Every Home Assistant binary sensor maps naturally to a Djev **Noul** judgment.

Djev documents Noul as a yes/no judgment returning:

```text
answers.<question_id>.noul
```

where the value is the probability of **yes**, between `0` and `1`. It does not expose a separate Noul confidence field.

Therefore internally use:

```text
true_probability
```

rather than inventing a separate model confidence number.

---

# 14. Logical Djev request

Conceptually the request is:

```json
{
  "model": "djev",
  "state": "",
  "questions": {
    "result": {
      "type": "noul",
      "instructions": {
        "text": "<wrapper>\n\n<sensor prompt>",
        "image": "data:image/jpeg;base64,..."
      }
    }
  }
}
```

The verified LunaRoute `/v1/systemone` route accepts an image object in the question instructions. It rejects a top-level `images` field. This differs from Djev's direct API, so the wire format belongs only in the LunaRoute adapter.

LunaRoute's measured request limit was about 48 KB on 2026-09-24/25, including base64 image text. The adapter MUST measure the serialized request and fit it under a conservative 45,000-byte budget. It SHOULD keep the source resolution when it fits, then reduce JPEG quality and dimensions until it does. It MUST record the sent width and height. If no usable image fits, it treats the evaluation as a model-path failure with a clear diagnostic. These limits may change and must be checked again during implementation.

The application sends **one Djev request per sensor**, even when several sensors trigger from the same frame.

---

# 15. LunaRoute transport

Inference MUST go through LunaRoute.

The core application MUST NOT depend directly on LunaRoute HTTP details.

Define:

```python
class ModelClient:
    async def evaluate_binary(
        image: FullResolutionFrame,
        instructions: str,
    ) -> BinaryJudgment:
        ...
```

`FullResolutionFrame` here means the decoded frame at the camera's native pixel dimensions. The adapter may resize the transmitted image under the request-budget rule above.

and implement:

```text
LunaRouteDjevClient
```

The working local `test.sh` establishes `POST https://gw.lunaroute.com/v1/systemone` for text questions. Prior live image probes in the neighboring `mm-decisions` project establish the image-object format and measured request limit above. The adapter MUST validate a fresh image response during implementation and keep these LunaRoute details out of camera, state, and MQTT code.

No direct `api.djev.dev` fallback should occur silently.

---

# 16. Prompt composition

Each sensor supplies:

```yaml
prompt: Is the backyard gate visibly open?
```

The system supplies an optional global wrapper.

Final instructions are:

```text
<system.model.prompt_wrapper>

<sensor.prompt>
```

The wrapper is configurable.

Default wrapper:

```text
Evaluate only the visible contents of the supplied camera frame.
Answer the requested binary condition based only on visible evidence.
Do not assume facts that cannot be seen.
```

The sensor prompt remains the semantic definition of the entity.

---

# 17. State resolution

For a valid Noul response:

```python
p = result.true_probability
t = sensor.true_threshold
```

Use a symmetric uncertainty band:

```text
p >= t
    → ON

p <= (1 - t)
    → OFF

otherwise
    → KEEP PREVIOUS STATE
```

Example:

```yaml
true_threshold: 0.80
```

produces:

```text
0.80 – 1.00    ON
0.20 – 0.80    unchanged
0.00 – 0.20    OFF
```

This preserves the explicit requirement that low-confidence / ambiguous observations must not overwrite an existing state.

There is no multi-observation debounce.

One sufficiently confident inference can immediately transition the binary sensor.

---

# 18. Malformed model output

If LunaRoute successfully returns a response but the expected Djev result cannot be parsed or validated:

```text
sensor → OFF
```

This is intentionally different from a transport/model failure.

Publish diagnostic attributes indicating the malformed response.

Do not attempt to infer meaning from arbitrary text.

---

# 19. Model failures

The following count as inference failures:

```text
network failure
timeout
HTTP service failure
authentication failure
provider failure
capacity failure
```

On model failure:

```text
sensor availability → offline
```

The previous binary state is retained internally but Home Assistant sees the entity as unavailable.

The sensor becomes available again after the next successful Djev evaluation.

---

# 20. RTSP failures

If a camera stream becomes unreadable:

```text
camera → offline
all sensors attached to camera → unavailable
```

The RTSP worker MUST automatically reconnect using exponential backoff.

Suggested sequence:

```text
1s
2s
4s
8s
16s
30s
30s
30s...
```

On successful camera recovery, attached sensors may become available again provided they are not independently unavailable due to a model failure.

---

# 21. Internal availability

Treat availability as two independent conditions:

```python
sensor_available = (
    camera_available
    and
    model_available_for_sensor
)
```

This avoids a recovered camera accidentally clearing an outstanding model error.

---

# 22. MQTT topology

Default topic prefix:

```text
djev-sensors
```

Per sensor:

```text
djev-sensors/<sensor_id>/state
djev-sensors/<sensor_id>/attributes
djev-sensors/<sensor_id>/availability
```

Example:

```text
djev-sensors/gate_open/state
djev-sensors/gate_open/attributes
djev-sensors/gate_open/availability
```

---

# 23. MQTT state

State payloads:

```text
ON
OFF
```

Publish state with:

```text
retain = false
```

This means Home Assistant may show the entity as unknown following a restart until another state message is received; that follows Home Assistant's MQTT binary sensor behavior for non-retained state.

MQTT reconnect MUST NOT cause all current states to be republished.

A new state is published only as the result of a subsequent evaluation.

---

# 24. MQTT availability

Availability payload:

```text
online
offline
```

Each sensor has its own availability topic. Discovery also references a shared service availability topic for MQTT Last Will. Both must be online for the entity to be available.

Home Assistant MQTT binary sensors natively support availability topics and display the entity as unavailable when the configured unavailable payload is received.

The application SHOULD use MQTT Last Will for process-level failure in addition to explicit camera/model failure messages.

---

# 25. Home Assistant MQTT Discovery

Discovery is always enabled.

Topic:

```text
homeassistant/binary_sensor/djev_sensors/<sensor_id>/config
```

Discovery configuration messages MUST be retained.

Home Assistant documents retained MQTT Discovery config so entities can be reconstructed after Home Assistant reconnects or restarts.

Example:

```json
{
  "name": "Gate Open",
  "unique_id": "djev_sensors_gate_open",
  "state_topic": "djev-sensors/gate_open/state",
  "json_attributes_topic": "djev-sensors/gate_open/attributes",
  "availability": [
    {"topic": "djev-sensors/gate_open/availability"},
    {"topic": "djev-sensors/service/availability"}
  ],
  "availability_mode": "all",
  "payload_on": "ON",
  "payload_off": "OFF",
  "payload_available": "online",
  "payload_not_available": "offline",
  "device_class": "opening",
  "device": {
    "identifiers": ["djev_sensors"],
    "name": "Djev Vision Sensors",
    "manufacturer": "2389 Research"
  }
}
```

Home Assistant MQTT binary sensors support `state_topic`, `json_attributes_topic`, an `availability` list with `availability_mode`, `device_class`, and `unique_id`.

---

# 26. Attributes

After every completed inference, publish:

```json
{
  "true_probability": 0.93,
  "true_threshold": 0.8,
  "change_pct": 4.71,
  "camera": "backyard",
  "model": "djev",
  "evaluated_at": "2026-09-25T19:32:17Z",
  "latency_ms": 241,
  "sent_width": 768,
  "sent_height": 432
}
```

Optional diagnostic fields:

```json
{
  "parse_error": false,
  "model_request_id": "...",
  "frame_age_ms": 84
}
```

Do not invent a Djev `confidence` value.

`true_probability` is the model-provided Noul probability.

---

# 27. State persistence

No persistent state database is required in v0.1.

Sensor state exists in memory.

On application restart:

```text
internal state = UNKNOWN
```

The application:

1. republishes retained discovery configuration
2. connects cameras
3. establishes frame baselines
4. waits for visual change
5. performs inference
6. publishes the first resulting state

It does NOT perform startup inference.

---

# 28. Sensor runtime state

Suggested runtime object:

```python
class SensorRuntime:
    config

    state: bool | None

    last_true_probability: float | None
    last_change_pct: float | None

    last_evaluation_at: datetime | None
    last_trigger_at: datetime | None

    model_available: bool

    inference_in_flight: bool
    pending_frame: Frame | None
```

---

# 29. Camera runtime state

```python
class CameraRuntime:
    config

    connected: bool

    previous_detection_frame
    latest_full_frame

    current_change_pct: float

    sensors: list[SensorRuntime]
```

---

# 30. Main camera loop

Conceptual pseudo-code:

```python
async def camera_loop(camera):

    while running:

        try:
            await camera.connect()

            mark_camera_online(camera)

            previous = None

            async for full_frame in camera.sample_frames():

                detection_frame = prepare_for_change_detection(
                    full_frame
                )

                if previous is None:
                    previous = detection_frame
                    continue

                change = detector.compare(
                    previous,
                    detection_frame,
                )

                previous = detection_frame

                for sensor in camera.sensors:

                    if (
                        change.changed_pct
                        < sensor.change_threshold_pct
                    ):
                        continue

                    if not cooldown_expired(sensor):
                        continue

                    schedule_evaluation(
                        sensor=sensor,
                        frame=full_frame,
                        change=change,
                    )

        except CameraError:

            mark_camera_offline(camera)

            await reconnect_with_exponential_backoff()
```

---

# 31. Sensor evaluation

Conceptually:

```python
async def evaluate(sensor, frame, change):

    sensor.last_trigger_at = now()

    try:
        result = await model.evaluate_binary(
            image=frame,
            instructions=compose_prompt(sensor),
        )

    except InvalidModelResponse:
        sensor.model_available = True
        publish_availability_if_camera_online(sensor)
        sensor.state = False
        publish_state(sensor, "OFF")
        publish_attributes(sensor, parse_error=True)
        return

    except ModelError:
        sensor.model_available = False
        publish_availability(sensor, "offline")
        return

    sensor.model_available = True
    publish_availability_if_camera_online(sensor)

    p = result.true_probability

    publish_attributes(
        sensor,
        true_probability=p,
        change_pct=change.changed_pct,
    )

    threshold = sensor.true_threshold

    if p >= threshold:
        new_state = True

    elif p <= 1 - threshold:
        new_state = False

    else:
        return

    if new_state != sensor.state:
        sensor.state = new_state

        publish_state(
            sensor,
            "ON" if new_state else "OFF",
        )
```

Publishing only on state change is the default.

---

# 32. Change detector defaults

Recommended starting defaults:

```yaml
change_detection:
  type: frame_difference
  width: 320
  pixel_delta_threshold: 20
  blur: 5
```

Sensor defaults:

```yaml
true_threshold: 0.80
change_threshold_pct: 2.5
cooldown_seconds: 10
```

Camera default:

```yaml
fps: 1
```

These are operational starting values, not calibrated universal thresholds.

---

# 33. Logging

Logs SHOULD be structured.

Important events:

```text
camera.connected
camera.disconnected
camera.reconnecting

frame.change

sensor.triggered
sensor.cooldown_skipped

inference.started
inference.completed
inference.failed
inference.invalid_response

sensor.state_changed
sensor.availability_changed

mqtt.connected
mqtt.disconnected
mqtt.discovery_published
```

Example:

```json
{
  "event": "inference.completed",
  "sensor": "gate_open",
  "camera": "backyard",
  "true_probability": 0.93,
  "change_pct": 4.71,
  "latency_ms": 241
}
```

Raw camera images MUST NOT be written to disk by default.

---

# 34. Metrics

Useful counters/gauges:

```text
camera_frames_decoded_total
camera_frames_sampled_total
camera_reconnects_total

change_events_total

inference_requests_total
inference_failures_total
inference_latency_seconds

sensor_transitions_total

mqtt_publish_failures_total
```

Metrics support can initially be log-derived rather than requiring Prometheus.

---

# 35. Suggested project structure

```text
djev-sensors/
├── README.md
├── pyproject.toml
├── Dockerfile
├── compose.yaml
├── config.example.yaml
│
├── djev_sensors/
│   ├── __main__.py
│   ├── config.py
│   ├── app.py
│   │
│   ├── camera.py
│   ├── scheduler.py
│   ├── state.py
│   │
│   ├── detectors/
│   │   ├── base.py
│   │   └── frame_difference.py
│   │
│   ├── models/
│   │   ├── base.py
│   │   └── lunaroute_djev.py
│   │
│   └── mqtt/
│       ├── client.py
│       └── discovery.py
│
└── tests/
    ├── fixtures/
    ├── test_change_detection.py
    ├── test_sensor_state.py
    ├── test_scheduler.py
    ├── test_model_response.py
    └── test_mqtt_discovery.py
```

---

# 36. Docker deployment

The application SHOULD run cleanly as a single container.

Example:

```yaml
services:
  djev-sensors:
    image: djev-sensors:latest

    restart: unless-stopped

    environment:
      LUNAROUTE_API_KEY: ${LUNAROUTE_API_KEY}
      MQTT_PASSWORD: ${MQTT_PASSWORD}
      GARAGE_RTSP_URL: ${GARAGE_RTSP_URL}
      BACKYARD_RTSP_URL: ${BACKYARD_RTSP_URL}

    volumes:
      - ./config.yaml:/app/config.yaml:ro
```

No persistent volume is required.

---

# 37. Failure semantics

| Situation | HA state behavior |
|---|---|
| No change detected | unchanged |
| Sensor cooldown active | unchanged |
| Djev result confidently true | `ON` |
| Djev result confidently false | `OFF` |
| Djev result uncertain | unchanged |
| Djev response malformed | `OFF` |
| Djev request fails | `unavailable` |
| RTSP fails | `unavailable` |
| RTSP recovers | availability may recover |
| Djev later succeeds | model availability recovers |
| Service restarts | `unavailable` until the camera connects, then the last state HA received, until a new judgment |
| Home Assistant restarts | unknown until the next state message, because state is not retained |
| MQTT reconnects | do not republish previous state |
| No evaluation for a long time | retain previous state |

---

# 38. Acceptance tests

The implementation is v0.1 complete when all of the following pass.

### Camera sharing

Given:

```text
sensor A → garage
sensor B → garage
sensor C → garage
```

there is exactly one RTSP decoder for `garage`.

### Startup

Given a running camera:

```text
service starts
```

no Djev call occurs until a sampled frame differs sufficiently from its predecessor.

### Camera-level change

Given:

```text
change = 3%

sensor A threshold = 2%
sensor B threshold = 5%
```

only sensor A triggers.

### Independent inference

If two sensors qualify from the same frame:

```text
2 independent Djev requests
```

are generated.

### Inference image

Change detection may use the reduced frame. Inference uses the corresponding full-resolution decoded frame as its source. Djev receives it at full size when the request fits LunaRoute's budget; otherwise it receives the largest fitted JPEG. Attributes report the sent dimensions.

### Confidence band

Given:

```text
true_threshold = .8
```

then:

```text
p=.91 → ON
p=.09 → OFF
p=.55 → unchanged
```

### Immediate transition

A single qualifying inference can transition state.

No repeated confirmation is required.

### Cooldown

A sensor receiving repeated changes cannot invoke inference more frequently than its configured cooldown.

### Bad response

A successful HTTP response containing an invalid Djev result publishes:

```text
OFF
```

### Model failure

A failed Djev request publishes:

```text
availability = offline
```

The next successful inference restores model availability.

### Camera failure

RTSP loss publishes:

```text
availability = offline
```

for all associated sensors and initiates exponential reconnect.

### MQTT Discovery

Every configured sensor automatically appears as a Home Assistant MQTT binary sensor.

Discovery messages are retained.

### MQTT state

State messages are not retained.

### MQTT reconnect

MQTT reconnection alone does not cause previous sensor state to be republished.

---

# 39. Core invariant

The implementation should remain understandable as this single pipeline:

```text
CAMERA
  │
  │ RTSP
  ▼
FRAME
  │
  │ sample
  ▼
CHANGE SCORE
  │
  ├── below threshold ──────────────┐
  │                                 │
  ▼                                 │
SENSOR                              │
  │                                 │
  │ cooldown OK                     │
  ▼                                 │
FULL FRAME + QUESTION               │
  │                                 │
  ▼                                 │
DJEV                                │
  │                                 │
  ▼                                 │
P(TRUE)                             │
  │                                 │
  ├── high ───────────────→ ON      │
  ├── low ────────────────→ OFF     │
  └── ambiguous ──────────→ HOLD    │
                                    │
                  ┌─────────────────┘
                  ▼
             NEXT FRAME
```

Everything else should remain infrastructure around that loop.

# 40. v0.1 definition of done

A user can add:

```yaml
sensors:
  someone_at_door:
    name: Someone at Door
    camera: front_door
    prompt: Is a person currently standing at the front door?
    device_class: occupancy
    true_threshold: .8
    change_threshold_pct: 2
    cooldown_seconds: 5
```

restart the service, and—without touching Home Assistant configuration—eventually get:

```text
binary_sensor.someone_at_door
```

whose state is semantically determined from the RTSP camera image by Djev and whose model probability and evaluation metadata are visible as Home Assistant attributes.

That is the product.
