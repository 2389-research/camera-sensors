# Gotchas

## LunaRoute Djev image requests

The local `test.sh` confirms `POST https://gw.lunaroute.com/v1/systemone` for text. Live probes recorded in `../mm-decisions/gotchas.md` on 2026-09-24/25 found that the gateway accepts an image data URL inside `questions.<id>.instructions.image`, rejects top-level `images`, and rejects requests near 49 KB because it counts base64 against model input tokens. Use a 45,000-byte serialized-body budget and recheck it with a live image request during implementation.

Doctor Biz approved keeping decoded frames at source resolution in memory and resizing only the transmitted JPEG when needed. Attributes must show the dimensions sent. Djev itself rejects images over 2048 pixels per side.

Do not overwrite sampled frames in a latest-only queue before change detection. The comparison baseline must advance through every sampled frame, even one that triggers no sensor. Schedule inference in the background after that comparison.

## Exposed key in local smoke script

`test.sh` once held a literal LunaRoute bearer token. Commit 61ee4d9 began tracking it only after the token gave way to `LUNAROUTE_API_KEY`, and no other commit touches it, so neither the file nor history holds the token. The key it exposed must still be rotated before any live test. Never stage or copy a key.

## ABOUTME lines can turn into encoding declarations

Python reads any comment in a file's first two lines that matches `coding[:=]` as a PEP 263 encoding declaration. An ABOUTME line saying "Shared RTSP decoding: one decoder..." failed to import with `SyntaxError: unknown encoding: one`. Never put "coding:" or "coding=" in an ABOUTME line.

## PyAV errors quote the RTSP URL, credentials included

`str(exc)` and `repr(exc)` of a PyAV error hold the full URL with its password (seen with `av.error.ConnectionRefusedError`, errno 61). Log only `type(exc).__name__` and `exc.errno`. FFmpeg's own log lines quote host and port; PyAV discards them by default, and `djev_sensors/camera.py` pins that with `av.logging.set_level(None)` before each open.

## Docker Desktop port probes in the integration stack

Docker Desktop accepts a TCP connection on a published port before the container listens, then closes it at once, so readiness means "connection held open", not "connect succeeded". After a run's stack is gone, macOS can refuse a plain `bind` on 18554 for a while though nothing listens; check that a port is free with a connect probe. Both live in `tests/local_ports.py`, which the integration and end-to-end stacks share.

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

## An RTSP open takes seconds to reach its first frame

In one throwaway check on 2026-09-25, PyAV's `av.open` of a MediaMTX stream (TCP, H.264 with a keyframe every second) decoded its first frame about 6 s after the open began. The service opens cameras the same way, so `camera.connected` comes seconds after each (re)connect starts. Timing-sensitive tests must allow for it: the e2e black-to-white clip turns white 30 s after its publisher starts, and the model test fails with a clear message if the camera connected after the switch.
