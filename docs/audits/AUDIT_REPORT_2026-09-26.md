# Documentation Audit Report

Generated 2026-09-26 against commit 593a1a2 by the documentation-audit skill. Scope: README.md, docs/spec.md, and gotchas.md, checked against the code. The plan under docs/superpowers/plans/ is a historical record and was left out.

## Summary

| Metric | Count |
|---|---|
| Documents scanned | 3 |
| Claims verified | about 330 |
| Verified true | about 300 (91%) |
| Verified false | 17: README 10, gotchas 2, spec 5 |
| Spec drift (informational) | 6 |
| Human review queue | 7 |
| Undocumented behavior | 6 |

## False claims

### README.md

| Line | Claim | Reality | Fix |
|---|---|---|---|
| 289-292 | The next successful request "needs a new qualifying change" | A model failure does not cancel the rechecks a sensor still owes (scheduler.py 135-144, 175-192; state.py 53-64). Reproduced: a failed look, then a 0% change sample, gave `recheck`, available, ON. The text predates rechecks. | An owed recheck, or once those run out a request after a new qualifying change, restores it |
| 406-407 | "stays unavailable ... until a new change triggers a request that succeeds" | Same | Same |
| 469-470 | "`LUNAROUTE_API_KEY` by default" | `api_key_env` has no default and is required (config.py 196-209) | "(`LUNAROUTE_API_KEY` in the example config)" |
| 19-20 | "sends the full-resolution frame ... to Djev" | Sends the largest JPEG of the frame that fits 45,000 bytes (lunaroute_djev.py 148-188); 4K frames shrink to about 768x432 | Say so |
| 317-318 | "Djev receives the full-resolution frame as a JPEG" | Contradicts lines 325-329 | A JPEG of the frame, shrunk when the full-size one does not fit, which is usual for HD and 4K |
| 356 | `inference.failed`: "`message` for gateway errors" | `message` comes with every `ModelError` (HTTP status, transport failure, image-fit failure); only unexpected exceptions omit it | Say `message` explains every expected failure |
| 511-512 | "They remove everything they start, including the images" | Only the images they build (`--rmi local`); pulled images and the build cache stay | Say so |
| 223 | `name`: "Name shown in Home Assistant" | Home Assistant prefixes the device name ("Djev Vision Sensors ...") | Mention the prefix |
| 311 | `parse_error`: "`true` after a malformed answer" | Valid judgments send `parse_error: false` too | Document both |
| 351 | `sensor.triggered`: "An evaluation starts" | Only movement-triggered looks; rechecks log `sensor.rechecking` | "Movement starts an evaluation" |

### gotchas.md

| Line | Claim | Reality | Fix |
|---|---|---|---|
| 13 | "no other commit touches it" (test.sh) | b14cacd changed `set -eu` to `set -euo pipefail`; history still holds no literal token | Name that commit |
| 5 | "rejects requests near 49 KB" | README and spec say about 48 KB; the source measured 48,149 bytes passing and 48,957 failing | Use one figure everywhere |

### docs/spec.md

| Section | Claim | Reality | Fix |
|---|---|---|---|
| 25 | Example discovery payload | Lacks `default_entity_id`, which section 40's `binary_sensor.someone_at_door` depends on | Add it with one sentence of explanation |
| 35 | Suggested project structure | Lists `tests/fixtures/`, flat `tests/test_*.py`, `test_model_response.py`; omits `events.py`, `frames.py`, `scripts/check` | Replace with the actual layout |
| 26 | `parse_error` optional; `model_request_id` and `frame_age_ms` listed | `parse_error` is always sent; the other two are not implemented | Move `parse_error` into the main example; mark the others as not in v0.1 |
| 8 | resize, then grayscale, then blur | Code does grayscale, resize, blur (frame_difference.py 27-37) | Reorder |
| 7 | JPEG-encodes "otherwise" | Always JPEG-encoded, even at full size | Reword |

### Code comments

- compose.yaml 12-13 gives the shutdown budget as 3 + 3 + 2 s; app.py 22-25 gives 3 + 3 + 5 s (paho's connect timeout), about 11 s. Make them agree.
- lunaroute_djev.py 48-49 says "the next qualifying change retries"; an owed recheck retries too.

## Spec drift (informational)

- Sections 17 and 31 write `p <= 1 - t`, the float-unsafe form gotchas.md warns against; state.py uses `p + t <= 1.0`.
- Section 5's example lacks the recheck keys.
- Section 36's example lacks `build`, `init`, and `stop_grace_period`.
- Section 33 lists 16 of the 22 events.
- Section 28 suggests fields that do not exist, such as `pending_frame`.
- Section 1 uses `##` where every other section uses `#`.

## Undocumented behavior

1. Cameras do not start until the first MQTT connect (app.py 115-121); with the broker down the logs show only `service.started` and `mqtt.connect_failed`.
2. RTSP opens and reads time out after 10 s (camera.py 87-89); a stalled stream counts as a failure.
3. The base `scripts/check` pulls `bluenviron/mediamtx:1` and `eclipse-mosquitto:2`, but the README mentions registry access only for `--live`.
4. The `sensor.cooldown_skipped` reason values (`cooldown`, `in_flight`) are not listed.
5. Some log fields are optional or typed differently than the table suggests: `errno` only when the exception has one; `rc` only for paho error codes; `mqtt.discovery_published.sensors` is a count while `service.started.sensors` is a list.
6. MQTT reconnects back off from 1 s, doubling up to 120 s (mqtt/client.py 62-65).

## Human review queue

- README 386: a PyAV timeout probably logs as `ExitError` (the interrupt callback returns 1); name it.
- README 272-275 and spec section 37: what Home Assistant shows after the service restarts.
- README 402-404: check the gateway's text fits the 200-character error excerpt.
- README 339-342: library warnings print as plain text, not JSON, and gateway error excerpts could echo request content.
- gotchas.md deployment facts that cannot be checked from the repo.
- README 474-475 says "or secret store", but the tracked compose.override.yaml's `env_file: .env` makes `.env` mandatory.
- Neither doc states a minimum Home Assistant version for `default_entity_id` (tested on 2026.9.3).

## Verified true (highlights)

All 26 config keys, defaults, and bounds match config.py, and the README's example equals `config.example.yaml`. The README's event table lists exactly the 22 events the code emits. Topics, retain flags, the RTSP reconnect backoff, exit codes, `scripts/check` behavior, test ports, and image tags match. The gotchas numbers reproduce. All 30 "spec section N" references point to the right section.

## Implementation plan

1. Pin recovery by recheck with a scheduler test (it should pass as-is), since the docs will now promise it.
2. Fix the README's user-facing claims, then its reference tables, then document the six gaps.
3. Fix gotchas.md lines 13 and 5.
4. Fix the spec (sections 25, 35, 26, 8, 7) and the drift items.
5. Align the two code comments.
6. Re-run the pattern searches ("new change", "full-resolution", "by default"), re-diff the README example against `config.example.yaml`, and run `scripts/check`.
