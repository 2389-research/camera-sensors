# djev-sensors

djev-sensors turns RTSP camera streams into Home Assistant binary sensors.
Each sensor asks Djev one yes-or-no question about what a camera sees, such
as "Is a car parked inside the garage?". The Djev model answers it through
LunaRoute, and the answer reaches Home Assistant over MQTT Discovery: no
Home Assistant YAML to write.

A camera that never changes costs nothing: the service makes no model
request until something moves in its frame, and even then it asks Djev at
most once per sensor per cooldown, plus a few slow rechecks while the scene
settles. See [How it works](#how-it-works) for the exact cost.

## Contents

- [How it works](#how-it-works)
- [Quick start with the prebuilt image](#quick-start-with-the-prebuilt-image)
- [Running from a clone and building from source](#running-from-a-clone-and-building-from-source)
- [Configuration reference](#configuration-reference)
- [How sensors decide](#how-sensors-decide)
- [Home Assistant](#home-assistant)
- [Deploying, updating, and changing sensors](#deploying-updating-and-changing-sensors)
- [Operating](#operating)
- [Development](#development)

## How it works

1. The service decodes each distinct RTSP URL once, however many sensors
   watch it, and samples each camera at its configured frame rate.
2. It scores each sample against the one before it: the percent of pixels
   that changed in a small grayscale copy.
3. A sensor triggers when that score reaches its `change_threshold_pct`, its
   cooldown has passed, and it has no request in flight. After that movement
   it rechecks the newest frame a few more times, slowly, so a scene that
   settles still gets judged.
4. The service sends the sensor's question and a JPEG of the frame to Djev
   through LunaRoute, and gets back the probability that the answer is yes.
   The JPEG is the frame at full resolution when that fits LunaRoute's byte
   budget, and shrunk when it does not, which is usual for HD and 4K
   cameras. See [How sensors decide](#how-sensors-decide) for the details.
5. A high probability publishes `ON`, a low one `OFF`, and one in between
   keeps the current state.

A camera that never changes sends nothing to the model, and the service makes
no request at startup. Movement that settles within the cooldown costs at most
one request plus `recheck_count` rechecks per sensor, spaced by the cooldown
or the recheck interval, whichever is longer. Movement that lasts longer
starts another request each time the cooldown expires.

## Quick start with the prebuilt image

Every push to `main` publishes `ghcr.io/2389-research/camera-sensors:latest`
for `linux/amd64` and `linux/arm64`. This is the fastest way to run the
service without cloning the repository or building anything.

You need:

- Docker with Compose (Docker Desktop, or the Docker Engine plus the
  `docker compose` plugin);
- an MQTT broker reachable from wherever the container runs, such as the
  Mosquitto add-on in Home Assistant or a standalone Mosquitto;
- a LunaRoute API key (looks like `lr_...`);
- the RTSP URL of each camera you want to watch.

### 1. Pull the image

The image is public, so no login is needed:

```sh
docker pull ghcr.io/2389-research/camera-sensors:latest
```

| Tag | Points at |
|---|---|
| `latest` | The newest commit on `main`. It moves with every merge. |
| `sha-<commit>` | One exact commit, such as `sha-8afa8f5`. Use it to pin a deployment you intend to keep running. |
| `<version>`, `<major>.<minor>`, `<major>` | A release, such as `0.1.0`, `0.1`, and `0`. These appear only when a `v*` git tag is pushed; none exist yet. |

Every tag holds builds for `linux/amd64` and `linux/arm64`, and Docker pulls
the one that matches your machine. The package page on GitHub lists every
published tag.

What the image expects from you:

- It runs `python -m djev_sensors --config /app/config.yaml` as an
  unprivileged user, UID 10001, and exposes no ports.
- Mount your config file read-only at `/app/config.yaml`. The container's
  user must be able to read it, so give it mode 644, not 600.
- Pass the LunaRoute key, the camera URLs, and any MQTT password as
  environment variables, under the names your config file gives.

### 2. Create a folder with three files

```sh
mkdir djev-sensors && cd djev-sensors
```

`compose.yaml`, using the published image. It keeps the same restart
policy, init, shutdown grace period, and read-only config mount as the
repository's own `compose.yaml`:

```yaml
services:
  djev-sensors:
    image: ghcr.io/2389-research/camera-sensors:latest
    restart: unless-stopped
    init: true
    stop_grace_period: 15s
    env_file: .env
    volumes:
      - ./config.yaml:/app/config.yaml:ro
```

`.env`, holding the key, the broker password, and the camera URL. Create it
at mode 600, since it holds secrets:

```sh
touch .env
chmod 600 .env
```

```
LUNAROUTE_API_KEY=lr_...
MQTT_PASSWORD=...
GARAGE_RTSP_URL=rtsp://viewer:secret@camera.example/stream1
```

`config.yaml`, with one camera and one sensor. See
[Configuration reference](#configuration-reference) for every key:

```yaml
system:
  mqtt:
    host: mqtt.example
    username: homeassistant
    password_env: MQTT_PASSWORD

  model:
    provider: lunaroute
    model: djev
    api_key_env: LUNAROUTE_API_KEY

cameras:
  garage:
    rtsp: ${GARAGE_RTSP_URL}
    fps: 1

sensors:
  car_in_garage:
    name: Car in Garage
    camera: garage
    prompt: |
      Is a car currently parked inside the garage?
    device_class: occupancy
```

### 3. Start it

```sh
docker compose up -d
docker compose logs -f djev-sensors
```

### Confirm it works

Watch the logs. In a working startup, these lines appear in this order:

1. `service.started`, right away.
2. `mqtt.connected`, once the broker accepts the connection.
3. `mqtt.discovery_published`, with `sensors` equal to the number of
   sensors in `config.yaml`.
4. `camera.connected`, once per camera, as each one's stream connects (an
   RTSP open can take several seconds).
5. `sensor.availability_changed`, for each sensor on that camera, with
   `available: true`.

If the broker is unreachable, only `service.started` and repeated
`mqtt.connect_failed` lines appear; see
[Operating](#operating) for what that and other failures look like.

Check the retained MQTT topics. This example's broker needs a login, so pass
`-u` and the same password as `MQTT_PASSWORD` in `.env`:

```sh
mosquitto_sub -h mqtt.example -u homeassistant -P '<password>' -t 'homeassistant/binary_sensor/djev_sensors/#' -v
mosquitto_sub -h mqtt.example -u homeassistant -P '<password>' -t 'djev-sensors/#' -v
```

The first prints one retained discovery payload per sensor. The second
prints `online` for `djev-sensors/service/availability`, and, once the
camera has connected, `online` for `djev-sensors/car_in_garage/availability`.
Both stay open after printing the retained messages; press Ctrl-C once
you have seen them.

In Home Assistant, with the MQTT integration configured and discovery on,
open Settings > Devices & services > MQTT and find the "Djev Vision
Sensors" device. The sensor appears as `binary_sensor.car_in_garage`, shown
as unavailable until its camera connects, then unknown until Djev's first
confident judgment.

## Running from a clone and building from source

### Build and run with Docker Compose

1. Clone the repository and copy the example config:

   ```sh
   cp config.example.yaml config.yaml
   ```

   Edit `config.yaml` for your broker, cameras, and sensors.

2. Put the secrets and camera URLs in a `.env` file beside `compose.yaml`.
   Git and Docker both ignore `.env`. The file holds the key, the broker
   password, and camera passwords, so keep it at mode 600 with
   `chmod 600 .env`:

   ```
   LUNAROUTE_API_KEY=lr_...
   MQTT_PASSWORD=...
   GARAGE_RTSP_URL=rtsp://viewer:secret@camera.example/stream1
   BACKYARD_RTSP_URL=rtsp://viewer:secret@backyard-camera.example/stream1
   ```

   Compose reads `compose.override.yaml` along with `compose.yaml`, and the
   override passes every variable in `.env` into the container, so a new
   camera needs only a line here and an entry in `config.yaml`. `.env` is
   not optional: `compose.override.yaml` sets `env_file: .env`, and Compose
   refuses to start if that file is missing. The four variables above also
   appear under `environment:` in `compose.yaml`; for those four alone, a
   variable exported in your shell overrides `.env`. Compose warns about
   each of the four that is unset and passes it empty; the service fails
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
discovery on. Each sensor appears as `binary_sensor.<sensor_id>` on one
device, "Djev Vision Sensors". See [Home Assistant](#home-assistant).

### Run without Docker

```sh
uv sync
export LUNAROUTE_API_KEY=lr_... MQTT_PASSWORD=... GARAGE_RTSP_URL=... BACKYARD_RTSP_URL=...
uv run python -m djev_sensors --config config.yaml
```

The service exits with status 0 after SIGINT or SIGTERM, 2 when the config is
invalid (the reason goes to stderr), and 1 when anything else stops it.

## Configuration reference

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
    recheck_count: 3
    recheck_interval_seconds: 10

  gate_open:
    name: Gate Open
    camera: backyard

    prompt: |
      Is the backyard gate visibly open?

    device_class: opening

    true_threshold: 0.85
    change_threshold_pct: 1.5
    cooldown_seconds: 5
    recheck_count: 3
    recheck_interval_seconds: 10
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
| `api_key_env` | required | Name of the environment variable that holds the LunaRoute key. No default; the example config names it `LUNAROUTE_API_KEY`, but any name works. |
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
| `name` | required | The sensor's name. Home Assistant shows it in the friendly name, after the device name: "Djev Vision Sensors `<name>`". The entity ID instead comes from the sensor ID; see [Home Assistant](#home-assistant). |
| `camera` | required | A camera ID from `cameras`. |
| `prompt` | required | The yes/no question about the frame. See [Writing good prompts](#writing-good-prompts). |
| `device_class` | none | A Home Assistant binary sensor device class, such as `occupancy` or `opening`. |
| `true_threshold` | `0.80` | Probability for `ON`; `1 - true_threshold` or less means `OFF`. Above 0.5, at most 1. |
| `change_threshold_pct` | `2.5` | Percent of compared pixels that must change before the sensor asks Djev. Above 0, at most 100. |
| `cooldown_seconds` | `10` | Least time between the starts of two requests for this sensor. |
| `recheck_count` | `3` | Looks at the newest frame after each movement-triggered look, so a scene that settles still gets judged. New movement restores the count; `0` turns rechecks off. |
| `recheck_interval_seconds` | `10` | Time from the start of one request to the next recheck, never shorter than the cooldown. Above 0. |

### Secrets and camera URLs

Keys and passwords never go in `config.yaml`: the file names environment
variables instead, through `api_key_env`, `password_env`, and `${VAR}`
tokens inside camera URLs. The service enforces this by refusing a literal
`password` under `mqtt`, a literal `api_key` under `model`, or a literal
password inside a camera URL, and exits with status 2 naming the problem.
Keeping secrets out of the file means a `config.yaml` copied into a bug
report, committed by accident, or baked into a backup carries no live
credential; the environment is the one place a secret has to be handled on
purpose.

A camera URL can come whole from a variable (`rtsp: ${GARAGE_RTSP_URL}`),
or keep its password in one while the rest is written out:
`rtsp: rtsp://viewer:${GARAGE_PASSWORD}@camera.example:554/stream1`. The
service exits with status 2 when a variable its config names is missing or
empty.

### Writing good prompts

A few practices make sensors more reliable:

- Ask one yes-or-no question per sensor. Two conditions in one prompt blur
  the line between `ON` and `OFF`.
- Name a visible landmark to point Djev at the right part of the frame,
  such as "the long wooden desk with monitors", rather than a vague area
  like "the office".
- Ask for a coarse threshold, such as "are three or more people visible",
  rather than an exact count.
- Give a sensor whose answer changes slowly, such as occupancy over a whole
  afternoon, a longer `cooldown_seconds` and a smaller `recheck_count`, so
  it is not re-asked far more often than its answer could plausibly change.
- Watch a new sensor's `true_probability` and state for its first day, and
  reword its prompt if borderline frames keep landing near the threshold.

## How sensors decide

### Sampling and the change score

Each camera decodes continuously but is only sampled at its configured
`fps` (once a second by default). Every sampled frame is reduced to a small
grayscale copy: converted to grayscale, resized to `change_detection.width`
pixels wide (aspect ratio kept), and, unless `blur` is 0, smoothed with a
Gaussian blur. That copy is compared, pixel by pixel, against the same
reduction of the previous sampled frame, whether or not that previous frame
triggered a sensor: a pixel counts as changed when it moves by at least
`pixel_delta_threshold`, and the change score is the percentage of pixels
that changed. The first sample after startup or a reconnect has nothing to
compare against, so it only becomes the new baseline.

### Triggering a look

A sensor triggers a look at the source-resolution frame when all of these
hold:

- the change score reaches its `change_threshold_pct`,
- its `cooldown_seconds` have passed since its last request started, and
- it has no request in flight.

A qualifying change that arrives during the cooldown or while a request is
running is logged as `sensor.cooldown_skipped` (`reason: cooldown` or
`reason: in_flight`) and otherwise ignored, except that it still resets the
sensor's rechecks.

### Rechecks after movement

Any qualifying change, whether or not it triggers a look, sets the sensor's
remaining rechecks to `recheck_count`. After that, each later sample that
does not itself qualify as a change is offered as a recheck: once at least
`max(recheck_interval_seconds, cooldown_seconds)` seconds have passed since
the last request started, and no request is in flight, the service looks at
that newest frame, logs `sensor.rechecking`, and counts down one recheck.
This is how a scene that changes once and then holds still still gets
judged again a few seconds later, without waiting for more movement. New
movement restores the count to `recheck_count`; a camera outage clears any
rechecks still owed.

### What Djev sees

Frames stay in memory at the camera's resolution; the service writes none
to disk. Djev is sent a JPEG of the source-resolution frame, inside a JSON
request, shrunk when it needs to be to fit LunaRoute's byte budget.

LunaRoute counts the base64 image against Djev's 32,768 input tokens, at
about 1.5 bytes per token. In live probes on 2026-09-24 and 25, a request of
48,149 bytes passed and one of 48,957 bytes failed, so the limit sits near
48 KB. The service keeps every request body at or under 45,000 bytes:

1. It tries the frame at its own size, capped at 2048 pixels on the longest
   side (Djev's limit), at JPEG quality 85, then 75, 65, and 55.
2. If none fits, it shrinks the longest side to 1600, 1280, 1024, 896, 768,
   640, 512, 448, 384, 320, 256, and then 192 pixels, keeping the aspect
   ratio, and tries the same qualities at each size.
3. It sends the first body that fits, which is the largest fitted JPEG.
   When nothing fits, the request fails like any model failure.

A 1080p or 4K frame is rarely small enough at full size; in practice the
fitted image usually lands around 768x432. `sent_width` and `sent_height`,
published in the sensor's attributes, show what Djev actually got. The
limit was measured, not published, and `scripts/check --live` sends only
small images, so it does not recheck the limit.

### From a probability to a state

Djev returns a probability that the answer is yes. The service turns that
into a state with a symmetric band around `true_threshold`:

- at or above `true_threshold`, the sensor publishes `ON`;
- at or below `1 - true_threshold`, it publishes `OFF`;
- anything between them, the uncertainty band, keeps the sensor's current
  state, so a middling answer never flips it.

With the default 0.80, that is `ON` from 0.80 and `OFF` at 0.20 or below;
between them, nothing changes. One confident judgment is enough to change
the state; there is no confirmation round. Because the band is symmetric, a
first judgment that lands inside it leaves the sensor's state unknown until
a later judgment falls outside the band.

### A malformed answer or a model failure

Two different things can go wrong with a look, and the service treats them
differently:

- **A malformed answer.** Djev's response parses as JSON but does not carry
  a usable probability (missing, non-numeric, out of `[0, 1]`, or not
  finite). The service logs `inference.invalid_response` and publishes
  `OFF` every time, with `parse_error: true` in the attributes and no
  `true_probability`, `latency_ms`, `sent_width`, or `sent_height`. The
  sensor stays available: a malformed answer is still a completed request.
- **A model failure.** The request times out, fails at the transport or
  HTTP level, or the frame cannot fit the byte budget. The service logs
  `inference.failed` and does not publish a state at all, so the sensor
  keeps whatever `ON`/`OFF` it last had. Instead, it marks the sensor
  unavailable (see [Availability](#availability)). That unavailability
  lifts at the next look that completes successfully, which can be a fresh
  qualifying change or one of the sensor's already-owed rechecks; a failure
  does not cancel the rechecks a sensor still owes.

## Home Assistant

### Discovery and entity IDs

On every MQTT connect, the first or a reconnect, the service publishes its
service topic `online`, every discovery config, and each sensor's current
availability. It never publishes an old state.

| Topic | Payload | Retained |
|---|---|---|
| `<discovery_prefix>/binary_sensor/djev_sensors/<sensor_id>/config` | discovery config (JSON) | yes |
| `<topic_prefix>/<sensor_id>/state` | `ON` or `OFF` | no |
| `<topic_prefix>/<sensor_id>/attributes` | attributes (JSON) | no |
| `<topic_prefix>/<sensor_id>/availability` | `online` or `offline` | yes |
| `<topic_prefix>/service/availability` | `online` or `offline`; the Last Will | yes |

The prefixes default to `homeassistant` and `djev-sensors`. Every sensor's
discovery config sets `default_entity_id` to `binary_sensor.<sensor_id>`,
so that is the entity ID Home Assistant assigns. The friendly name is
different: Home Assistant prefixes the sensor's configured `name` with the
device name, "Djev Vision Sensors", for example "Djev Vision Sensors Car in
Garage" for a sensor named "Car in Garage", even though the entity ID
itself carries no such prefix. This was tested on Home Assistant 2026.9.3;
neither this project nor Home Assistant's own documentation states a
minimum version `default_entity_id` needs.

All of a service's sensors appear on one device, "Djev Vision Sensors", made
by "2389 Research".

### Attributes

After each judgment the service publishes:

| Attribute | Meaning |
|---|---|
| `true_probability` | Djev's probability that the answer is yes. Absent on a malformed answer. |
| `true_threshold` | The sensor's threshold. |
| `change_pct` | The judged frame's change score; small for a recheck. |
| `camera` | The camera ID. |
| `model` | `djev`. |
| `evaluated_at` | UTC time of the judgment. |
| `latency_ms` | The request's round trip. Absent on a malformed answer. |
| `sent_width`, `sent_height` | Size of the image Djev received. Absent on a malformed answer. |
| `parse_error` | `true` after a malformed answer, `false` after a valid judgment. Always present. |
| `trigger` | `change` for a look triggered by movement, `recheck` for a follow-up look at the newest frame. |

### Availability

An entity is available only while all of these hold:

- The service is connected. A clean shutdown marks every sensor `offline`,
  then publishes the retained service `offline`. When the connection drops
  without a clean MQTT disconnect, as after a crash or `docker kill`, the
  broker publishes the service `offline` as the Last Will.
- Its camera streams. When the stream fails or ends, every sensor on that
  camera goes unavailable, and the service reconnects after 1, 2, 4, 8, 16,
  and then every 30 seconds. The first frame after a reconnect restores it.
- Its last look did not fail. A model failure (see
  [How sensors decide](#how-sensors-decide)) makes the sensor unavailable
  and keeps its state. It becomes available again at the next look that
  completes successfully, whether that is a fresh qualifying change or one
  of the sensor's already-owed rechecks. A malformed answer is not a
  failure and does not affect availability.

Home Assistant also marks MQTT entities unavailable while its own broker
connection is down, regardless of what this service reports.

### State after a restart

- State lives in memory. After the service restarts, every sensor's state
  is unknown, and the service publishes none until the sensor's first
  confident judgment or malformed answer. A malformed answer publishes
  `OFF` from unknown too.
- Home Assistant keeps the last state it received, because the state topic
  is not retained. So after the service restarts, the entity shows
  unavailable until its camera reconnects, then that last state, until a
  new judgment overwrites it.
- After Home Assistant itself restarts, the entity shows unknown until the
  next state message, because discovery and availability come back at
  once (both retained), but state does not.
- A sensor whose camera shows no change for hours keeps its state for
  hours; nothing times it out.

## Deploying, updating, and changing sensors

### Updating a running deployment

- Prebuilt image on `latest`: `docker compose pull && docker compose up -d`.
- Prebuilt image pinned to a `sha-` tag: change the tag in `compose.yaml`,
  then run the same two commands.
- Built from source: `git pull`, then `docker compose up -d --build`.

`docker compose restart` restarts the existing container with its existing
environment. It will not pick up a changed `.env` file or a new image; use
`docker compose up -d` for either.

### Adding, renaming, and removing sensors

Add a sensor by adding a `sensors.<sensor_id>` block to `config.yaml` (and a
camera and a `.env` line, if it needs a new camera), then
`docker compose up -d`.

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

Renaming a sensor is removing the old ID and adding a new one: after
restarting the service with the new ID in `config.yaml`, clear the old ID's
two retained topics the same way.

### Rotating the LunaRoute key

The key lives only in the environment variable that `api_key_env` names
(`LUNAROUTE_API_KEY` in the example config): never in `config.yaml`, the
image, or the logs. To rotate it:

1. Create a new key in LunaRoute.
2. Update every place that holds the key: the `.env` file that feeds
   `docker compose` (mandatory for this repository's tracked
   `compose.override.yaml`), every other deployment, and the shell you run
   `scripts/check --live` from.
3. Recreate the container with `docker compose up -d`. `docker compose
   restart` keeps the old environment.
4. Watch for `inference.completed`, or run `scripts/check --live`.
5. Revoke the old key.

Never commit `.env` or paste a key into a script or config file. If a key
leaks, rotate it before any live request.

## Operating

### Logs

The service writes one JSON object per line on stdout, event name first.
Lines carry no timestamp; `docker compose logs --timestamps` adds Docker's.
Logs never hold images, request bodies, keys, or camera URLs, and camera
errors show only the exception class and errno. A gateway error's `message`
is whatever text the gateway returned, cut to 200 characters; it names the
problem (as in "the request exceeds this model's max_input_tokens of
32768"), but since it comes from the gateway's own response text, it could
in principle echo something back from what was sent. Warnings a library
prints on its own, such as a `DeprecationWarning`, appear as plain text on
stderr, not as JSON.

| Event | Fields | When |
|---|---|---|
| `service.started` / `service.stopped` | `cameras`, `sensors` (lists) / `error` if it failed | Startup and shutdown. |
| `camera.connected` | `camera` | First frame after a (re)connect. |
| `camera.disconnected` | `camera`, `error`, `errno` | A live stream failed or ended. |
| `camera.reconnecting` | `camera`, `attempt`, `delay`, `error`, `errno` | Before each retry. |
| `frame.change` | `camera`, `change_pct` | A score reached the lowest threshold among the camera's sensors. |
| `sensor.triggered` | `sensor`, `camera`, `change_pct` | Movement starts an evaluation. |
| `sensor.cooldown_skipped` | `sensor`, `camera`, `change_pct`, `reason` (`cooldown` or `in_flight`) | A change arrived during cooldown or while a request was in flight. |
| `sensor.rechecking` | `sensor`, `camera`, `change_pct`, `remaining` | A follow-up look at the newest frame starts, with `remaining` rechecks still owed. |
| `inference.started` | `sensor`, `camera` | A model request goes out. |
| `inference.completed` | `sensor`, `camera`, `true_probability`, `change_pct`, `latency_ms` | Djev answered. |
| `inference.failed` | `sensor`, `camera`, `error`, `message` | The request failed. `message` comes with every expected failure, a bad HTTP status, a transport error, or a frame that could not fit; it is missing only for an unexpected exception. |
| `inference.invalid_response` | `sensor`, `camera` | Djev's answer did not parse. |
| `inference.discarded` | `sensor`, `camera` | The camera dropped while the request waited or ran. |
| `sensor.state_changed` | `sensor`, `camera`, `state` | `ON` or `OFF` changed. |
| `sensor.availability_changed` | `sensor`, `camera`, `available`, `camera_available`, `model_available` | Availability changed. |
| `mqtt.connected` / `mqtt.disconnected` | `host`, `port` / `reason_code`, `reason` | Broker connection. |
| `mqtt.connect_failed` | `host`, `port`, and `reason_code`, `reason` unless the TCP connection itself failed | A connect attempt failed. |
| `mqtt.discovery_published` | `sensors` (a count, not a list) | After each connect. |
| `mqtt.publish_failed` | `topic`, `error`, `rc` (only for a paho error code, not an exception) | A publish was dropped. |
| `camera.callback_failed` | `camera`, `callback`, `error`, `traceback` | A bug; the service stops. |

`errno` appears only when the failing exception carries an integer one.

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

### Troubleshooting

#### RTSP

- `camera.reconnecting` repeats with `ConnectionRefusedError`, `ExitError`
  (the open or a read ran past its 10-second timeout), or another exception
  naming the failure: the container cannot connect to the camera, or the
  camera stopped answering. Check the host, port, and path, and test the
  URL from the Docker host with `ffprobe -rtsp_transport tcp '<url>'`.
- The service reads RTSP over TCP only. Enable TCP on cameras that default to
  UDP.
- `error: end_of_stream` means the camera or relay closed the stream.
- Many cameras allow only a few viewers. The service opens one connection
  per distinct URL, however many sensors use it, but other viewers count too.

#### Model

- `inference.failed` with `LunaRoute returned HTTP 401`: the key is wrong,
  revoked, or empty. See
  [Rotating the LunaRoute key](#rotating-the-lunaroute-key).
- `LunaRoute request failed: ConnectTimeout` or `ReadTimeout`: no route to
  `gw.lunaroute.com`, or `timeout_seconds` is too short.
- Every request fails with `LunaRoute returned HTTP 400` and a `message`
  that includes "the request exceeds this model's max_input_tokens of
  32768": the gateway's limit has moved below the service's request budget.
  See [What Djev sees](#what-djev-sees).
- A sensor stays unavailable after a model failure until the next look that
  succeeds, a fresh qualifying change or an owed recheck; see
  [Availability](#availability).
- `inference.invalid_response`: Djev answered without a usable probability.
  The sensor goes `OFF` with `parse_error: true`.

#### MQTT

- `mqtt.connect_failed` with a reason such as "Not authorized": the broker
  refused the service. Check `username` and the variable `password_env` names.
- `mqtt.connect_failed` with the reason "Unspecified error": the connection
  closed before the broker accepted it, as when `port` is a TLS listener or a
  service that does not speak MQTT. "Keep alive timeout" means nothing
  answered for 60 seconds.
- `mqtt.connect_failed` with no reason: the service could not open a TCP
  connection to `host` and `port`.
- The service does not open any camera or send any model request until its
  first successful MQTT connect. While the broker is unreachable, the logs
  show only `service.started` followed by repeated `mqtt.connect_failed`
  lines, nothing about cameras at all. Paho retries on its own, waiting 1
  second after the first failed attempt and doubling, up to 120 seconds,
  between later ones; there is nothing to restart, only the broker to fix.
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

#### Container startup

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

## Development

`scripts/check` runs `ruff check`, `ruff format --check`, strict mypy over
`djev_sensors`, and the unit and integration tests. `scripts/check --live`
runs all of that, then the end-to-end tests in `tests/e2e`.

Prerequisites for both: uv, Docker running, and FFmpeg on `PATH` (the RTSP
tests publish with it). The integration tests bring up MediaMTX and
Mosquitto with `docker compose` on 127.0.0.1:18554 and 18883, which must be
free, pulling `bluenviron/mediamtx:1` and `eclipse-mosquitto:2` the first
time they run; a plain `scripts/check` needs registry access for that, not
just `--live`. The repository also needs to sit in a directory the Docker
engine shares with containers, since the test stacks mount files from it;
Colima, for example, shares only your home directory by default. `--live`
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
leaves them, and imports the service as the image's unprivileged user. Both
remove only the images they built (`docker compose down --rmi local`, or
`docker image rm` for the standalone one); pulled images and the build
cache stay. To run the ones that need no key:

```sh
uv run pytest tests/e2e -q -k "not djev and not lunaroute"
```

The script prints `==> <step>` before each step. It exits 0 after printing
`All checks passed.`, stops with status 1 at the first failing step after
printing `FAILED: <step> (command: ...)`, and exits 2 with a usage line for
any other argument.
