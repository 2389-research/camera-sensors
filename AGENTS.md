# AGENTS.md

Notes for coding agents working on camera-sensors. People should start with
the [README](README.md).

Crew names, recorded once per the naming rite: the agent building this is
**KEYFRAME KRUSHER 64** (monster truck of motion detection, couch whisperer).
The boss is **DOCTOR BIZNOCULARS, RIZZ WARDEN OF THE RTSP**. Normal address
stays "Doctor Biz".

## Commands

```sh
uv sync                      # install the service and the dev tools
scripts/check                # ruff, format check, strict mypy, unit and integration tests
scripts/check --live         # all of that plus the end-to-end tests; needs LUNAROUTE_API_KEY
uv run pytest tests/unit -q  # the fast loop
uv run python -m djev_sensors --config config.yaml
```

`scripts/check` needs Docker, since the integration tests start MediaMTX and
Mosquitto. The README's [Run the checks](README.md#run-the-checks) section
lists the ports and images each run needs.

## Where to look

- The README's [Where things live](README.md#where-things-live) table maps
  the code and the tests.
- `docs/spec.md` is the design spec, and code comments cite its sections.
- `gotchas.md` records what broke and why. Read it before touching PyAV,
  paho-mqtt, the LunaRoute gateway, or the Docker test stacks, and add an
  entry when something surprises you.

## House rules

- Every hand-written source file opens with two `ABOUTME:` comment lines that
  say what the file does. Never put "coding:" or "coding=" in one, because
  Python reads that as an encoding declaration.
- Write the failing test first. End-to-end tests run against real services,
  never mocks.
- Never put a key, a broker password, or a camera URL in a commit, a test, or
  a log line. PyAV errors quote the full camera URL, so log only the exception
  class and errno.
- Update the README and `docs/spec.md` in the same pull request as the
  behavior they describe, and keep the README's example config identical to
  `config.example.yaml`.
- Describe machines by role, never by host name, LAN address, or SSH login.
  Plans under `docs/superpowers/`, audits under `docs/audits/`, and
  `.private-journal/` stay out of git.
- Commit messages follow Conventional Commits: `feat:`, `fix:`, `docs:`, and
  so on.
- The package stays `djev_sensors`, and the MQTT defaults stay
  `djev-sensors`. The README explains why near the top.
