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

## Resolution (2026-09-26, first half: everything but README)

Covers implementation plan items 1, 3, 4, 5, and the non-README rows of "False claims" and all of "Spec drift". README fixes (plan item 2) are a separate pass; its rows are not covered here.

### 1. Scheduler test pinning recheck recovery

Added `test_a_recheck_restores_availability_after_a_model_failure` to `tests/unit/test_scheduler.py`: a `ModelError` on the first, movement-triggered look, then a valid judgment on a later below-threshold sample (an owed recheck). Asserts availability publishes `False` then `True`, and that the recheck's `attributes` publish has `trigger: "recheck"`. It passed on the first run with no production-code change, confirming recovery-by-recheck already matches spec section 19 ("the sensor becomes available again after the next successful Djev evaluation").

### 3. gotchas.md

- Line 13 (test.sh key): now names `b14cacd` as the only other commit to touch the file (it added `pipefail`), instead of saying no other commit touches it.
- Line 5 (gateway limit): "rejects requests near 49 KB" -> "rejects requests at about 48 KB", the same figure README and spec.md use, backed by the measured 48,149-byte pass and 48,957-byte fail.
- Read the rest of the file against `camera.py`, `mqtt/client.py`, `lunaroute_djev.py`, `state.py`, and the `Dockerfile`; found no further contradictions.

### 4. docs/spec.md — false claims

| Section | Change |
|---|---|
| 25 | Added `default_entity_id` to the example discovery payload, with a sentence tying it to section 40's `binary_sensor.someone_at_door`. |
| 35 | Replaced the suggested project layout with the real one: `events.py`, `frames.py`, `scripts/check`, and tests split into `tests/unit`, `tests/integration`, `tests/e2e`. |
| 26 | Moved `parse_error` into the main attributes example (it is always sent); `model_request_id` and `frame_age_ms` are now marked "Not implemented in v0.1". |
| 8 | Reordered the pipeline diagram to grayscale, then resize, then blur, matching `frame_difference.py`. |
| 7 | Reworded: the transmitted image is always JPEG-encoded; only whether it's resized first depends on whether it fits at full size. |

### 4. docs/spec.md — spec drift

| Item | Change |
|---|---|
| Sections 17, 31 | Added a note in each that implementations should compare `p + t <= 1.0`, not `p <= 1 - t`, pointing to `djev_sensors/state.py`. |
| Section 5 | Added `recheck_count` / `recheck_interval_seconds` to both example sensors. |
| Section 36 | Added `build: .`, `init: true`, and `stop_grace_period: 15s` to the compose example, matching `compose.yaml`. |
| Section 33 | Listed all 22 events the code emits (confirmed by parsing every `log_event(` call site with `ast`), up from 16; added `service.started`, `service.stopped`, `camera.callback_failed`, `inference.discarded`, `mqtt.connect_failed`, `mqtt.publish_failed`. |
| Section 28 | Rewrote the suggested runtime object to match `djev_sensors/state.py`'s actual `SensorRuntime` dataclass (`camera_available`/`model_available` instead of a single flag, `cooldown_started_at`, no `pending_frame`) and its `available` property. |
| Section 1 | Changed its heading from `##` to `#`, matching every other section. |

### 5. Code comments

- `compose.yaml`: the shutdown-budget comment now says 3 s (hub) + 3 s (drain) + up to 5 s more (paho's connect timeout) ≈ 11 s, under the 15 s grace period — matching `app.py`'s own comment instead of the stale 3+3+2 figure.
- `djev_sensors/models/lunaroute_djev.py` (class docstring, formerly lines 48-49): now says "the next qualifying change, or an owed recheck, retries naturally", not only the next qualifying change.

### Not addressed in this pass (out of the assigned scope)

**Undocumented behavior** (6 items) — none of these were in the assigned spec.md section list, so `docs/spec.md` does not yet document:

1. Cameras don't start until the first MQTT connect.
2. RTSP opens and reads time out after 10 s.
3. `scripts/check` unconditionally pulls `bluenviron/mediamtx:1` and `eclipse-mosquitto:2`.
4. `sensor.cooldown_skipped`'s reason values (`cooldown`, `in_flight`) aren't listed.
5. Some log fields are optional or typed differently than the section 33 table suggests.
6. MQTT reconnects back off from 1 s, doubling to 120 s.

**Human review queue** (7 items) — six name a README line and belong to the README pass; the seventh ("gotchas.md deployment facts that cannot be checked from the repo") is not a documentation error to fix, just a standing caveat about facts this repo can't self-verify. None required a `spec.md` or `gotchas.md` change beyond section 4 and 3 above.

**Verified true (highlights)** — no changes; nothing here was found false.

### 5 (workflow). Docker image for people to use

Added `.github/workflows/docker-image.yml` (new file, not present before). Builds the existing `Dockerfile` for `linux/amd64` and `linux/arm64` via Buildx + QEMU and publishes to `ghcr.io/2389-research/camera-sensors`, using the GitHub Actions cache (`type=gha`) and `docker/metadata-action` for tags and OCI labels (including the default `org.opencontainers.image.source` label that links the package back to this repo). `compose.yaml` is untouched and still builds from source (`build: .`), so the couch and office (aibox03) deployments are unaffected.

- Triggers: `push` to `main`, `push` of `v*` tags, `pull_request`, and `workflow_dispatch`.
- Tags: `latest` and `sha-<short-sha>` on push to `main` only (`enable={{is_default_branch}}`); `major.minor.patch`, `major.minor`, and `major` semver tags on `v*` tags only (metadata-action's `type=semver` is inherently tag-scoped).
- Push happens for every event except `pull_request` (`push: ${{ github.event_name != 'pull_request' }}`), so PRs build both platforms without publishing; `workflow_dispatch` does push, since no instruction said otherwise and a manual run that can never publish seemed of little use — flagged as a judgment call.
- Permissions: job-level `contents: read`, `packages: write`, using the workflow's own `GITHUB_TOKEN` (no PAT).
- Actions and pinned majors, looked up with `gh api repos/<owner>/<repo>/releases/latest --jq .tag_name` on 2026-09-26 rather than guessed: `actions/checkout@v7` (v7.0.1), `docker/setup-qemu-action@v4` (v4.4.0), `docker/setup-buildx-action@v4` (v4.4.1), `docker/login-action@v4` (v4.6.0), `docker/metadata-action@v6` (v6.2.0), `docker/build-push-action@v7` (v7.4.0).
- Validated with `docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:latest -color`: exit 0, no findings.
- Not pushed, tagged, or opened as a PR; visibility on GitHub is unchanged.
