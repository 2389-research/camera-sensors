# ABOUTME: Tests test.sh, the LunaRoute smoke script, with a stand-in curl first on PATH
# ABOUTME: so no request leaves: a failed request must fail the script on its own.
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = 30.0

# Stands in for curl: records that it ran, prints a whole JSON body, and exits
# with the status curl --fail gives an HTTP error.
FAKE_CURL = """#!/bin/sh
: > "$FAKE_CURL_RAN"
echo '{"answers": {}}'
exit 22
"""


def test_a_failed_request_fails_the_smoke_script_even_when_its_output_parses(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(FAKE_CURL)
    curl.chmod(0o755)
    # The script pipes the reply through python3 -m json.tool.
    python3 = bin_dir / "python3"
    python3.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python3.chmod(0o755)
    ran = tmp_path / "curl-ran"

    result = subprocess.run(
        [str(REPO_ROOT / "test.sh")],
        # The stand-in comes first on PATH. /usr/bin, where macOS keeps the
        # real curl, stays off it too.
        env={
            "PATH": f"{bin_dir}:/bin",
            "LUNAROUTE_API_KEY": "lr_fake_key_never_sent",
            "FAKE_CURL_RAN": str(ran),
        },
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )

    assert ran.exists(), result.stderr
    # json.tool accepts the body, so only pipefail can pass curl's status on.
    assert result.returncode == 22, result.stderr
