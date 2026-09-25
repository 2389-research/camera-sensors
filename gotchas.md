# Gotchas

## LunaRoute Djev image requests

The local `test.sh` confirms `POST https://gw.lunaroute.com/v1/systemone` for text. Live probes recorded in `../mm-decisions/gotchas.md` on 2026-09-24/25 found that the gateway accepts an image data URL inside `questions.<id>.instructions.image`, rejects top-level `images`, and rejects requests near 49 KB because it counts base64 against model input tokens. Use a 45,000-byte serialized-body budget and recheck it with a live image request during implementation.

Doctor Biz approved keeping decoded frames at source resolution in memory and resizing only the transmitted JPEG when needed. Attributes must show the dimensions sent. Djev itself rejects images over 2048 pixels per side.

Do not overwrite sampled frames in a latest-only queue before change detection. The comparison baseline must advance through every sampled frame, even one that triggers no sensor. Schedule inference in the background after that comparison.

## Exposed key in local smoke script

`test.sh` is untracked and contains a literal LunaRoute bearer token. Never stage or copy that token. Replace it with `LUNAROUTE_API_KEY` before tracking the script, and rotate the exposed key before any live test.

## ABOUTME lines can turn into encoding declarations

Python reads any comment in a file's first two lines that matches `coding[:=]` as a PEP 263 encoding declaration. An ABOUTME line saying "Shared RTSP decoding: one decoder..." failed to import with `SyntaxError: unknown encoding: one`. Never put "coding:" or "coding=" in an ABOUTME line.

## PyAV errors quote the RTSP URL, credentials included

`str(exc)` and `repr(exc)` of a PyAV error hold the full URL with its password (seen with `av.error.ConnectionRefusedError`, errno 61). Log only `type(exc).__name__` and `exc.errno`. FFmpeg's own log lines quote host and port; PyAV discards them by default, and `djev_sensors/camera.py` pins that with `av.logging.set_level(None)` before each open.

## Docker Desktop port probes in the integration stack

Docker Desktop accepts a TCP connection on a published port before the container listens, then closes it at once, so readiness means "connection held open", not "connect succeeded". After a run's stack is gone, macOS can refuse a plain `bind` on 18554 for a while though nothing listens; check that a port is free with a connect probe. Both live in `tests/integration/conftest.py`.

## MediaMTX ends readers when the publisher leaves

With the default `alwaysAvailable: false`, MediaMTX closes RTSP readers when their publisher disconnects, and PyAV's `decode()` then stops without an exception. The camera hub counts that as a disconnect.

## Probability band edges: write `p + t <= 1.0`, not `p <= 1.0 - t`

`1.0 - 0.8` is `0.19999999999999996`, so `0.2 <= 1.0 - 0.8` is False: the plan's `resolve_state` left a probability of exactly 0.2 inside the uncertainty band at the default threshold. Across the 5,000 distinct thresholds with up to four decimal places in (0.5, 1], each paired with its decimal `1 - t`, the subtraction missed the edge for 1,663 and `1.0 - p >= t` missed 414. `p + t <= 1.0` missed none: when two decimals add up to one, their rounding errors cannot push the sum above 1.0. `djev_sensors/state.py` uses the sum. (Verified 2026-09-25.)
