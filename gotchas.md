# Gotchas

## LunaRoute Djev image requests

The local `test.sh` confirms `POST https://gw.lunaroute.com/v1/systemone` for text. Live probes recorded in `../mm-decisions/gotchas.md` on 2026-09-24/25 found that the gateway accepts an image data URL inside `questions.<id>.instructions.image`, rejects top-level `images`, and rejects requests near 49 KB because it counts base64 against model input tokens. Use a 45,000-byte serialized-body budget and recheck it with a live image request during implementation.

Doctor Biz approved keeping decoded frames at source resolution in memory and resizing only the transmitted JPEG when needed. Attributes must show the dimensions sent. Djev itself rejects images over 2048 pixels per side.

Do not overwrite sampled frames in a latest-only queue before change detection. The comparison baseline must advance through every sampled frame, even one that triggers no sensor. Schedule inference in the background after that comparison.

## Exposed key in local smoke script

`test.sh` is untracked and contains a literal LunaRoute bearer token. Never stage or copy that token. Replace it with `LUNAROUTE_API_KEY` before tracking the script, and rotate the exposed key before any live test.
