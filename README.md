# djev-sensors

djev-sensors turns RTSP camera streams into Home Assistant binary sensors. Each
sensor asks one yes/no question about what a camera sees, such as "Is a car
parked inside the garage?". The Djev model answers it through LunaRoute, and
the answer reaches Home Assistant over MQTT Discovery, with no Home Assistant
YAML.

## How it works

1. The service decodes each distinct RTSP URL once, however many sensors
   watch it, and samples each camera at its configured frame rate.
2. It scores each sample against the one before it: the percent of pixels
   that changed in a small grayscale copy.
3. A sensor triggers when that score reaches its `change_threshold_pct`, its
   cooldown has passed, and it has no request in flight. After that movement
   it rechecks the newest frame a few more times, slowly, so a scene that
   settles still gets judged.
4. The service sends the full-resolution frame and the sensor's question to
   Djev through LunaRoute and gets back the probability that the answer is yes.
5. A high probability publishes `ON`, a low one `OFF`, and one in between
   keeps the current state.

A camera that never changes sends nothing to the model, and the service makes
no request at startup. Each burst of movement costs at most one request plus
`recheck_count` rechecks per sensor, spaced by the cooldown or the recheck
interval, whichever is longer.

## Run it with Docker Compose

1. Copy the example config and edit it for your broker, cameras, and sensors:

   ```sh
   cp config.example.yaml config.yaml
   ```

2. Put the secrets in a `.env` file beside `compose.yaml`. Compose reads it
   for the variables `compose.yaml` names, and your shell's environment
   overrides it. Git and Docker both ignore `.env`. The file holds the key,
   the broker password, and camera passwords, so keep it at mode 600 with
   `chmod 600 .env`:

   ```
   LUNAROUTE_API_KEY=lr_...
   MQTT_PASSWORD=...
   GARAGE_RTSP_URL=rtsp://viewer:secret@192.0.2.10:554/stream1
   BACKYARD_RTSP_URL=rtsp://viewer:secret@192.0.2.11:554/stream1
   ```

   If your config names other variables, add them under `environment:` in
   `compose.yaml` as well, or the container never sees them. Compose warns
   about each of the four that is unset and passes it empty; the service fails
   only when a variable its config names is empty.

3. Build and start the container:

   ```sh
   docker compose up -d --build
   docker compose logs -f djev-sensors
   ```

The image is Python 3.12 with dependencies installed by uv from `uv.lock`. The
container mounts `config.yaml` read-only at `/app/config.yaml` and restarts
unless you stop it. It needs no volume: frames and state stay in memory. It
runs as an unprivileged user, UID 10001, which must be able to read
`config.yaml`; the file holds no secrets, so mode 644 is fine.
`docker compose stop` sends SIGTERM and allows 15 seconds, which covers the
service's slowest shutdown.

In Home Assistant, add the MQTT integration for your broker and leave
discovery on. Each sensor appears as `binary_sensor.<sensor_id>` on one device,
"Djev Vision Sensors".

## Run it without Docker

```sh
uv sync
export LUNAROUTE_API_KEY=lr_... MQTT_PASSWORD=... GARAGE_RTSP_URL=... BACKYARD_RTSP_URL=...
uv run python -m djev_sensors --config config.yaml
```

The service exits with status 0 after SIGINT or SIGTERM, 2 when the config is
invalid (the reason goes to stderr), and 1 when anything else stops it.

## Configuration

### Example: two cameras

This is `config.example.yaml`:

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

### Reference

The service rejects unknown keys. Camera and sensor IDs use lowercase
letters, digits, and underscores. The config needs at least one sensor, and
the sensor ID `service` is reserved for the service's own availability topic.

`system.mqtt`

| Key | Default | Meaning |
|---|---|---|
| `host` | required | Broker host name or address. |
| `port` | `1883` | Broker port. |
| `username` | none | Broker user name. |
| `password_env` | none | Name of the environment variable that holds the broker password. Requires `username`. |
| `discovery_prefix` | `homeassistant` | Must match the discovery prefix of Home Assistant's MQTT integration. Not empty; no `+` or `#`. |
| `topic_prefix` | `djev-sensors` | Start of every state, attribute, and availability topic. Not empty; no `+` or `#`. |
| `client_id` | `djev-sensors` | MQTT client ID. Two clients with one ID disconnect each other over and over. |

`system.model`

| Key | Default | Meaning |
|---|---|---|
| `provider` | required | `lunaroute`, the only provider. |
| `model` | required | `djev`, the only model. |
| `api_key_env` | required | Name of the environment variable that holds the LunaRoute key. |
| `timeout_seconds` | `15` | Time limit for each model request. |
| `prompt_wrapper` | the three lines in the example | Text sent before every sensor prompt, a blank line between. An empty wrapper sends the prompt alone. |

`system.inference`

| Key | Default | Meaning |
|---|---|---|
| `max_concurrent_requests` | `4` | Most model requests in flight at once, across all sensors. |

`system.change_detection`

| Key | Default | Meaning |
|---|---|---|
| `type` | `frame_difference` | The only change detector. |
| `width` | `320` | Width in pixels of the grayscale copy that gets compared. |
| `pixel_delta_threshold` | `20` | How far, from 0 to 255, a pixel must move to count as changed. |
| `blur` | `5` | Gaussian blur size applied before comparing: 0 for none, or an odd number. |

`cameras.<camera_id>`

| Key | Default | Meaning |
|---|---|---|
| `rtsp` | required | Stream URL. `${VAR}` tokens expand from the environment. |
| `fps` | `1` | Samples per second. |

`sensors.<sensor_id>`

| Key | Default | Meaning |
|---|---|---|
| `name` | required | Name shown in Home Assistant. |
| `camera` | required | A camera ID from `cameras`. |
| `prompt` | required | The yes/no question about the frame. |
| `device_class` | none | A Home Assistant binary sensor device class, such as `occupancy` or `opening`. |
| `true_threshold` | `0.80` | Probability for `ON`; `1 - true_threshold` or less means `OFF`. Above 0.5, at most 1. |
| `change_threshold_pct` | `2.5` | Percent of compared pixels that must change before the sensor asks Djev. Above 0, at most 100. |
| `cooldown_seconds` | `10` | Least time between the starts of two requests for this sensor. |
| `recheck_count` | `3` | Looks at the newest frame after each movement-triggered look, so a scene that settles still gets judged. New movement restores the count; `0` turns rechecks off. |
| `recheck_interval_seconds` | `10` | Time from the start of one request to the next recheck, never shorter than the cooldown. Above 0. |

### Secrets and camera URLs

Keys and passwords never go in `config.yaml`. The file names environment
variables instead: `api_key_env`, `password_env`, and `${VAR}` tokens in camera
URLs. A camera URL can come whole from a variable (`rtsp: ${GARAGE_RTSP_URL}`),
or keep its password in one: `rtsp: rtsp://viewer:${GARAGE_PASSWORD}@192.0.2.10:554/stream1`.
The service refuses a literal `password` under `mqtt`, a literal `api_key`
under `model`, and a literal password in a camera URL. It exits with status 2
when a variable its config names is missing or empty.

## What Home Assistant sees

### Topics

With the default prefixes:

| Topic | Payload | Retained |
|---|---|---|
| `homeassistant/binary_sensor/djev_sensors/<sensor_id>/config` | discovery config (JSON) | yes |
| `djev-sensors/<sensor_id>/state` | `ON` or `OFF` | no |
| `djev-sensors/<sensor_id>/attributes` | attributes (JSON) | no |
| `djev-sensors/<sensor_id>/availability` | `online` or `offline` | yes |
| `djev-sensors/service/availability` | `online` or `offline`; the Last Will | yes |

On every MQTT connect, the first or a reconnect, the service publishes its
service topic `online`, every discovery config, and each sensor's current
availability. It never publishes an old state.

### State

- A probability at or above `true_threshold` means `ON`, one at or below
  `1 - true_threshold` means `OFF`, and one between them keeps the current
  state. With the default 0.80, that is `ON` from 0.80, `OFF` up to 0.20.
- One confident judgment changes the state; there is no confirmation round.
- The service publishes a state only when it changes. A malformed answer from
  Djev publishes `OFF` every time, with `parse_error: true` in the attributes.
- State lives in memory. After a restart, every sensor's state is unknown,
  and the service publishes none until the sensor's first confident judgment
  or malformed answer. A malformed answer publishes `OFF` from unknown too.
- Home Assistant keeps the last state it received. When the service restarts,
  the entity shows unavailable until its camera connects, then that last state
  until a new judgment. After Home Assistant itself restarts, the entity shows
  unknown until the next state message, because state is not retained.
- A sensor whose camera shows no change for hours keeps its state for hours.

### Availability

An entity is available only while all of these hold:

- The service is connected. A clean shutdown marks every sensor `offline`,
  then publishes the retained service `offline`. When the connection drops
  without a clean MQTT disconnect, as after a crash or `docker kill`, the
  broker publishes the service `offline` as the Last Will.
- Its camera streams. When the stream fails or ends, every sensor on that
  camera goes unavailable, and the service reconnects after 1, 2, 4, 8, 16,
  and then every 30 seconds. The first frame after a reconnect restores it.
- Its last model request did not fail. A timeout, network error, HTTP error,
  or frame that cannot fit the request makes the sensor unavailable and keeps
  its state. The next request that succeeds restores it, and that request
  needs a new qualifying change.

Home Assistant also marks MQTT entities unavailable while its own broker
connection is down.

### Attributes

After each judgment the service publishes:

| Attribute | Meaning |
|---|---|
| `true_probability` | Djev's probability that the answer is yes. |
| `true_threshold` | The sensor's threshold. |
| `change_pct` | The judged frame's change score; small for a recheck. |
| `camera` | The camera ID. |
| `model` | `djev`. |
| `evaluated_at` | UTC time of the judgment. |
| `latency_ms` | The request's round trip. |
| `sent_width`, `sent_height` | Size of the image Djev received. |
| `parse_error` | `true` after a malformed answer, which carries no probability, latency, or size. |
| `trigger` | `change` for a look triggered by movement, `recheck` for a follow-up look at the newest frame. |

## Images sent to Djev

Frames stay in memory at the camera's resolution; the service writes none to
disk. Change detection works on the small grayscale copy, but Djev receives
the full-resolution frame as a JPEG inside a JSON request.

LunaRoute counts the base64 image against Djev's 32,768 input tokens, at
about 1.5 bytes per token. In live probes on 2026-09-24 and 25, a request of
48,149 bytes passed and one of 48,957 bytes failed, so the limit sits near
48 KB. The service keeps every request body at or under 45,000 bytes:

1. It tries the frame at its own size, capped at 2048 pixels on the longest
   side (Djev's limit), at JPEG quality 85, then 75, 65, and 55.
2. If none fits, it shrinks the longest side to 1600, 1280, 1024, 896, 768,
   640, 512, 448, 384, 320, 256, and then 192 pixels, keeping the aspect ratio,
   and tries the same qualities at each size.
3. It sends the first body that fits, which is the largest fitted JPEG. When
   nothing fits, the request fails like any model failure.

`sent_width` and `sent_height` show what Djev got. The limit was measured,
not published, and `scripts/check --live` sends only small images, so it
does not recheck the limit.

## Logs and metrics

The service writes one JSON object per line on stdout, event name first. Lines
carry no timestamp; `docker compose logs --timestamps` adds Docker's. Logs
never hold images, request bodies, keys, or camera URLs, and camera errors show
only the exception class and errno.

| Event | Fields | When |
|---|---|---|
| `service.started` / `service.stopped` | `cameras`, `sensors` / `error` if it failed | Startup and shutdown. |
| `camera.connected` | `camera` | First frame after a (re)connect. |
| `camera.disconnected` | `camera`, `error`, `errno` | A live stream failed or ended. |
| `camera.reconnecting` | `camera`, `attempt`, `delay`, `error`, `errno` | Before each retry. |
| `frame.change` | `camera`, `change_pct` | A score reached the lowest threshold among the camera's sensors. |
| `sensor.triggered` | `sensor`, `camera`, `change_pct` | An evaluation starts. |
| `sensor.cooldown_skipped` | `sensor`, `camera`, `change_pct`, `reason` | A change arrived during cooldown or while a request was in flight. |
| `sensor.rechecking` | `sensor`, `camera`, `change_pct`, `remaining` | A follow-up look at the newest frame starts, with `remaining` rechecks still owed. |
| `inference.started` | `sensor`, `camera` | A model request goes out. |
| `inference.completed` | `sensor`, `camera`, `true_probability`, `change_pct`, `latency_ms` | Djev answered. |
| `inference.failed` | `sensor`, `camera`, `error`, and `message` for gateway errors | The request failed. |
| `inference.invalid_response` | `sensor`, `camera` | Djev's answer did not parse. |
| `inference.discarded` | `sensor`, `camera` | The camera dropped while the request waited or ran. |
| `sensor.state_changed` | `sensor`, `camera`, `state` | `ON` or `OFF` changed. |
| `sensor.availability_changed` | `sensor`, `camera`, `available`, `camera_available`, `model_available` | Availability changed. |
| `mqtt.connected` / `mqtt.disconnected` | `host`, `port` / `reason_code`, `reason` | Broker connection. |
| `mqtt.connect_failed` | `host`, `port`, and `reason_code`, `reason` unless the TCP connection itself failed | A connect attempt failed. |
| `mqtt.discovery_published` | `sensors` | After each connect. |
| `mqtt.publish_failed` | `topic`, `error`, `rc` | A publish was dropped. |
| `camera.callback_failed` | `camera`, `callback`, `error`, `traceback` | A bug; the service stops. |

Count events to get the spec's metrics:

| Metric | From |
|---|---|
| `camera_reconnects_total` | `camera.reconnecting` |
| `change_events_total` | `frame.change` |
| `inference_requests_total` | `inference.started` |
| `inference_failures_total` | `inference.failed` (add `inference.invalid_response` to count bad answers) |
| `inference_latency_seconds` | `latency_ms` of `inference.completed`, divided by 1000 |
| `sensor_transitions_total` | `sensor.state_changed` |
| `mqtt_publish_failures_total` | `mqtt.publish_failed` |

`camera_frames_decoded_total` and `camera_frames_sampled_total` have no event,
so logs cannot supply them.

## Troubleshooting

### RTSP

- `camera.reconnecting` repeats with `ConnectionRefusedError` or a timeout:
  the container cannot connect to the camera. Check the host, port, and path,
  and test the URL from the Docker host with
  `ffprobe -rtsp_transport tcp '<url>'`.
- The service reads RTSP over TCP only. Enable TCP on cameras that default to
  UDP.
- `error: end_of_stream` means the camera or relay closed the stream.
- Many cameras allow only a few viewers. The service opens one connection
  per distinct URL, however many sensors use it, but other viewers count too.

### Model

- `inference.failed` with `LunaRoute returned HTTP 401`: the key is wrong,
  revoked, or empty. See [Rotating the LunaRoute key](#rotating-the-lunaroute-key).
- `LunaRoute request failed: ConnectTimeout` or `ReadTimeout`: no route to
  `gw.lunaroute.com`, or `timeout_seconds` is too short.
- Every request fails with `LunaRoute returned HTTP 400` and a `message`
  that includes "the request exceeds this model's max_input_tokens of
  32768": the gateway's limit has moved below the service's request budget.
  See [Images sent to Djev](#images-sent-to-djev).
- A sensor stays unavailable after a failure until a new change triggers a
  request that succeeds.
- `inference.invalid_response`: Djev answered without a usable probability.
  The sensor goes `OFF` with `parse_error: true`.

### MQTT

- `mqtt.connect_failed` with a reason such as "Not authorized": the broker
  refused the service. Check `username` and the variable `password_env` names.
- `mqtt.connect_failed` with the reason "Unspecified error": the connection
  closed before the broker accepted it, as when `port` is a TLS listener or a
  service that does not speak MQTT. "Keep alive timeout" means nothing
  answered for 60 seconds.
- `mqtt.connect_failed` with no reason: the service could not open a TCP
  connection to `host` and `port`.
- No entities in Home Assistant: its MQTT integration needs discovery on and
  the same `discovery_prefix`. Look for the retained configs with
  `mosquitto_sub -h <broker> -t 'homeassistant/binary_sensor/djev_sensors/#' -v`.
- Entities stay unavailable: read the two availability topics above to see
  whether the service or the camera and model are down.
- Two instances on one broker need different `client_id` and `topic_prefix`
  values, and different sensor IDs. The discovery node ID, `djev_sensors`, is
  fixed, so both publish discovery configs under
  `<discovery_prefix>/binary_sensor/djev_sensors/`, and one sensor ID in both
  would share a config topic and a unique ID. Their sensors also share one
  device, "Djev Vision Sensors".

### Startup

- The container restarts over and over: `docker compose logs djev-sensors`
  shows `python -m djev_sensors: error: ...` with the config problem, such as a
  missing variable. Fix it and run `docker compose up -d`.
- `could not read config file /app/config.yaml: ... Is a directory`: the
  container started before `config.yaml` existed, so Docker created a
  directory in its place. Remove the directory, create the file, and run
  `docker compose up -d`.
- `could not read config file /app/config.yaml: [Errno 13] Permission denied`:
  the container's user, UID 10001, cannot read the file. Run
  `chmod 644 config.yaml`.

## Removing or renaming a sensor

Removing a sensor from `config.yaml`, or changing its ID, leaves its old
entity in Home Assistant, because the broker keeps the retained discovery
config. The service's clean stop marks every configured sensor `offline`, so
after you remove one and restart the service, its old entity shows
unavailable. After a crash or `docker kill`, the old sensor's availability
keeps its last value, so its entity can still show available.

To delete the old entity, clear its two retained topics on the broker:

```sh
mosquitto_pub -h <broker> -r -n -t '<discovery_prefix>/binary_sensor/djev_sensors/<sensor_id>/config'
mosquitto_pub -h <broker> -r -n -t '<topic_prefix>/<sensor_id>/availability'
```

The prefixes default to `homeassistant` and `djev-sensors`; add `-u` and `-P`
if the broker needs a login. `-r -n` publishes an empty retained message,
which clears the retained one, and Home Assistant deletes an entity whose
discovery topic receives an empty payload.

## Rotating the LunaRoute key

The key lives only in the environment variable that `api_key_env` names,
`LUNAROUTE_API_KEY` by default: never in `config.yaml`, the image, or the
logs. To rotate it:

1. Create a new key in LunaRoute.
2. Update every place that holds the key: the `.env` file or secret store that
   feeds `docker compose`, every other deployment, and the shell you run
   `scripts/check --live` from.
3. Recreate the container with `docker compose up -d`. `docker compose restart`
   keeps the old environment.
4. Watch for `inference.completed`, or run `scripts/check --live`.
5. Revoke the old key.

Never commit `.env` or paste a key into a script or config file. If a key
leaks, rotate it before any live request.

## Checks

`scripts/check` runs `ruff check`, `ruff format --check`, strict mypy over
`djev_sensors`, and the unit and integration tests. `scripts/check --live`
runs all of that, then the end-to-end tests in `tests/e2e`.

Prerequisites for both: uv, Docker running (the integration tests start
MediaMTX and Mosquitto on 127.0.0.1:18554 and 18883, which must be free), the
repository in a directory the Docker engine shares with containers (the test
stacks mount files from it, and Colima shares only your home directory by
default), and FFmpeg on `PATH` (the RTSP tests publish with it). `--live`
also needs:

- a LunaRoute API key in `LUNAROUTE_API_KEY`; the live tests fail, not
  skip, without one;
- internet access to `gw.lunaroute.com`, to pull
  `ghcr.io/home-assistant/home-assistant:stable`, and to pull the service
  image's bases, `python:3.12-slim` and `ghcr.io/astral-sh/uv:0.9.25`;
- 127.0.0.1:18123, 18555, and 18884 free, for Home Assistant, RTSP, and MQTT.

The end-to-end tests build the service image from the `Dockerfile`, onboard a
fresh Home Assistant through its HTTP API, add its MQTT integration with the
config flow, and check entities through Home Assistant's REST API. They send
LunaRoute only synthetic pictures: a white square and a clip that turns from
black to white. Another test builds the image from a copy of the sources
that only their owner can read, the way a checkout under a strict umask
leaves them, and imports the service as the image's unprivileged user. They
remove everything they start, including the images. To run the ones that
need no key:

```sh
uv run pytest tests/e2e -q -k "not djev and not lunaroute"
```

The script prints `==> <step>` before each step. It exits 0 after printing
`All checks passed.`, stops with status 1 at the first failing step after
printing `FAILED: <step> (command: ...)`, and exits 2 with a usage line for
any other argument.
