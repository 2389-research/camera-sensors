# Gotchas

## LunaRoute Djev image requests

The local `test.sh` confirms `POST https://gw.lunaroute.com/v1/systemone` for text. Live probes recorded in `../mm-decisions/gotchas.md` on 2026-09-24/25 found that the gateway accepts an image data URL inside `questions.<id>.instructions.image`, rejects top-level `images`, and rejects requests at about 48 KB because it counts base64 against model input tokens. Use a 45,000-byte serialized-body budget and recheck it with a live image request during implementation.

Doctor Biz approved keeping decoded frames at source resolution in memory and resizing only the transmitted JPEG when needed. Attributes must show the dimensions sent. Djev itself rejects images over 2048 pixels per side.

Do not overwrite sampled frames in a latest-only queue before change detection. The comparison baseline must advance through every sampled frame, even one that triggers no sensor. Schedule inference in the background after that comparison.

## LunaRoute key in the local smoke script

`test.sh` once held a literal LunaRoute bearer token. Commit 61ee4d9 began tracking it only after the token gave way to `LUNAROUTE_API_KEY`, and the only other commit to touch the file, b14cacd, just added `pipefail`, so neither the file nor history holds the token. Doctor Biz ruled on 2026-09-25 that this key is not exposed, so it needs no rotation before live tests. The build had treated it as compromised and parked every live gate for hours. Keep keys out of commits and logs, but don't call a key exposed, or block work on rotating it, unless it was committed, pushed, or pasted somewhere shared; when unsure, ask. Never stage or copy a key.

## ABOUTME lines can turn into encoding declarations

Python reads any comment in a file's first two lines that matches `coding[:=]` as a PEP 263 encoding declaration. An ABOUTME line saying "Shared RTSP decoding: one decoder..." failed to import with `SyntaxError: unknown encoding: one`. Never put "coding:" or "coding=" in an ABOUTME line.

## PyAV errors quote the RTSP URL, credentials included

`str(exc)` and `repr(exc)` of a PyAV error hold the full URL with its password (seen with `av.error.ConnectionRefusedError`, errno 61). Log only `type(exc).__name__` and `exc.errno`. FFmpeg's own log lines quote host and port; PyAV discards them by default, and `djev_sensors/camera.py` pins that with `av.logging.set_level(None)` before each open.

## Docker port-forward probes in the test stacks

Under Docker Desktop, a published port accepted a TCP connection before the container listened, then closed it at once, so readiness means "connection held open", not "connect succeeded". No one has recorded whether Colima's forwarding does the same. After a run's stack is gone, macOS can refuse a plain `bind` on 18554 for a while though nothing listens; check that a port is free with a connect probe. Both live in `tests/local_ports.py`, which the integration and end-to-end stacks share.

## MediaMTX ends readers when the publisher leaves

With the default `alwaysAvailable: false`, MediaMTX closes RTSP readers when their publisher disconnects, and PyAV's `decode()` then stops without an exception. The camera hub counts that as a disconnect.

## Probability band edges: write `p + t <= 1.0`, not `p <= 1.0 - t`

`1.0 - 0.8` is `0.19999999999999996`, so `0.2 <= 1.0 - 0.8` is False: the plan's `resolve_state` left a probability of exactly 0.2 inside the uncertainty band at the default threshold. Across the 5,000 distinct thresholds with up to four decimal places in (0.5, 1], each paired with its decimal `1 - t`, the subtraction missed the edge for 1,663 and `1.0 - p >= t` missed 414. `p + t <= 1.0` missed none: when two decimals add up to one, their rounding errors cannot push the sum above 1.0. `djev_sensors/state.py` uses the sum. (Verified 2026-09-25.)

## paho-mqtt 2.1: resend order, locks, and sockets

Read in paho 2.1.0's `client.py`. After a reconnect, `_handle_connack` calls `on_connect` first and then resends the lost connection's unacknowledged QoS 1 messages, so an older retained availability still in flight can land after the replay `on_connect` publishes, until the next change; `MqttPublisher` therefore publishes per-sensor availability at QoS 0, which paho never resends, and the replay covers a lost one. The network thread can hold paho's `_out_message_mutex` while it runs `on_disconnect` (from that resend loop) or `on_publish`, and a QoS 1 `publish()` takes the same mutex: never call `publish()` while holding a lock your callbacks take. Only `Client.__del__` closes the socket pair that wakes the network thread (`reinitialise()` passes its arguments into the new `callback_api_version` slot and raises), so `MqttPublisher.stop()` clears its bound-method callbacks after `loop_stop()` rather than leave the client in a reference cycle.

## Mosquitto publishes the Last Will on a client-ID takeover

A second connection with the publisher's client ID made eclipse-mosquitto:2 drop the publisher and publish its Last Will (`offline`) before the publisher reconnected and replayed `online`. Observed once on 2026-09-25 in a debug run of the reconnect test; the man pages do not say, and `tests/integration/test_mqtt_publishing.py` accepts either behavior.

## macOS prints Objective-C duplicate-class notices when PyAV and OpenCV load together

PyAV and opencv-python-headless each bundle FFmpeg's libavdevice, so on macOS any process that imports both (`python -m djev_sensors`, the test suite) prints `objc[<pid>]: Class AVFFrameReceiver is implemented in both ...` and the same for `AVFAudioReceiver` on stderr at import time. Tests that assert on a child process's stderr must drop lines starting with `objc[`, as `tests/integration/test_cli.py` does. The classes belong to libavdevice's AVFoundation capture device, which the service never opens (it opens only RTSP URLs), and the notice comes from Apple's Objective-C runtime, so expect it on macOS only. (Seen 2026-09-25 with av 18.1.0 and opencv-python-headless 5.0.0.93.)

## asyncio.run waits up to 300 s for the default executor at exit

On Python 3.12, `asyncio.run` ends with `loop.shutdown_default_executor(constants.THREAD_JOIN_TIMEOUT)`, and that constant is 300 (read in the installed 3.12.12 `asyncio/runners.py` and `asyncio/constants.py`). Work that can block for long, such as the camera hub's stalled RTSP open, must not go through `asyncio.to_thread` or `run_in_executor(None, ...)`, or a bounded shutdown turns into a five-minute exit. `djev_sensors/app.py` runs the hub on its own daemon thread instead.

## The Docker engine here is Colima, which shares only $HOME with containers

On 2026-09-25 the docker CLI's context was `colima` (Colima 0.10.3: 2 CPUs, 2 GiB, no swap, virtiofs, SSH port forwarding), and Docker Desktop was not running. Colima's own `colima.yaml` documents that it mounts only `$HOME` by default, so pytest's `tmp_path` (under `/private/var/folders`) and `/private/tmp` are not shared with containers. `tests/e2e` writes the service's config under `tests/e2e/.run/`, which git and Docker both ignore, and mounts it from there. The full e2e stack (Home Assistant, MediaMTX, Mosquitto, the service) fit in the VM's 2 GiB, which had about 1.2 GB free before it started.

## Colima let a container user read an owner-only file

On 2026-09-26 a file under the repo written with mode 600 (umask 077) and bind-mounted into a `python:3.12-slim` container showed there as `-rw------- 1 0 0`, yet `cat` as UID 10001 read it. The e2e unprivileged-user test likewise passed under umask 077 before its harness made the service's config 644. So a file-mode bug that would lock UID 10001 out on a Linux Docker host goes unnoticed here; check modes on the host side, as `tests/unit/test_e2e_cleanup.py` does for the e2e config.

## An RTSP open takes seconds to reach its first frame

In one throwaway check on 2026-09-25, PyAV's `av.open` of a MediaMTX stream (TCP, H.264 with a keyframe every second) decoded its first frame about 6 s after the open began. The service opens cameras the same way, so `camera.connected` comes seconds after each (re)connect starts. Timing-sensitive tests must allow for it: the e2e black-to-white clip turns white 30 s after its publisher starts, and the model test fails with a clear message if the camera connected after the switch.

## uv run needs a writable cache, even with --no-sync

In the service image, `uv run --no-sync` as a system user without a home directory stopped at once with `failed to create directory /home/djev/.cache/uv: Permission denied (os error 13)` (uv 0.9.25, 2026-09-25), so the service never started. The image now runs the service with the virtualenv's own python (`/app/.venv/bin` leads `PATH`), so uv runs only in the Dockerfile's build stage, which the runtime image leaves behind, and the unprivileged user has no home directory. Anything that brings uv back into the runtime image and runs it as that user needs a writable cache first, such as one named by `UV_CACHE_DIR`. `tests/e2e/test_home_assistant.py` checks the container's UID after the service has published discovery.

## Don't judge the uv cache mount by a --no-cache build

On 2026-09-26, `docker compose build --no-cache` downloaded every large wheel again although `docker buildx du` showed the Dockerfile's `/root/.cache/uv` cache mount attached and holding 260 MB from the build before. Right after, a plain `docker build` whose layers through `COPY pyproject.toml uv.lock` came from the cache reran `uv sync`, and it installed all 16 packages from that mount with no download. Observed once each with Colima's BuildKit 0.30.0; the cause is unverified. A `--no-cache` build says nothing about whether the mount works.

## paho-mqtt 2.1: which callback sees a failed connect

Read in paho 2.1.0's `client.py`. `on_connect_fail` fires only when `reconnect()` raises `OSError`, meaning the TCP connect itself failed. A connection the peer closes before any CONNACK reaches only `on_disconnect`, with reason 128 "Unspecified error" (`_loop_rc_handle` after `MQTT_ERR_CONN_LOST`), and one that gets no CONNACK within the 60 s keepalive gets 141 "Keep alive timeout". A refused CONNACK calls `on_connect` with the refusal, then `on_disconnect` with 128. The service's own `disconnect()` reports reason 0, which is not a failure. `MqttPublisher` logs `mqtt.connect_failed` once for each of these paths.

## math.isfinite raises OverflowError for a huge int

`math.isfinite(10**400)` raises `OverflowError: int too large to convert to float`, and Python's `json` module turns a 401-digit number into exactly such an int. `_extract_noul` checks finiteness only for floats and lets the `0 <= value <= 1` comparison, which is exact for ints, reject the rest. (Verified 2026-09-25.)

## docker-host needed its real CPU passed through

docker-host (192.168.200.8, SSH as harper, login shell fish) is a KVM guest. Until 2026-09-26 it used the generic "Common KVM processor" model, which lacks the x86-64-v2 instructions (SSSE3, SSE4.1, SSE4.2, POPCNT). NumPy 2.5 requires them and died at import with "NumPy was built with baseline optimizations: (X86_V2) but your machine doesn't support: (X86_V2)". Doctor Biz switched the VM's CPU type to host passthrough (an Intel Core i7-8809G) and the service now runs there. When reading `/proc/cpuinfo`, SSE3 appears as `pni`, not `sse3`; grepping for `sse3` reports it missing on every CPU, which once led to the false claim that no older NumPy could run on the generic model. Watchtower there runs with WATCHTOWER_LABEL_ENABLE=true, so it leaves unlabeled local builds alone.

## LunaRoute latency can blow past the model timeout

On 2026-09-25 around 04:17-04:21 UTC, LunaRoute answers went from about 0.7 s to 7-14 s, a text-only smoke request took 47 s, and requests at the default 15 s `timeout_seconds` failed with ReadTimeout. Each timeout marks the sensor unavailable until the next successful look (spec section 19), so a slow gateway looks like a flapping sensor in Home Assistant. The couch deployment uses `timeout_seconds: 60`.

## The office cameras run on aibox03, not officetools

The office UniFi Protect streams (NVR at 192.168.23.1) are mostly 4K, and decoding one costs about half a core (measured 0.53 on officetools, 2026-09-26). officetools (192.168.23.123) has only 4 virtual cores, a load near 2.3, and about 2 GB of free disk, so the seven office cameras went to aibox03 (192.168.23.86): a bare-metal i9-13900K with 32 threads and 62 GB, where they use about 2.75 cores and 1.2 GB. aibox03 runs them from ~/camera-sensors and publishes to the office Mosquitto on officetools (192.168.23.123:1883, anonymous). 4K buys nothing here: change detection works at 320 pixels wide and the request budget shrinks what Djev sees to about 768x432, so a camera's Medium (720p) RTSP channel would do the same job for about a ninth of the decode cost.
