"""Re-download the live v4 specification over the vendored copy.

Run by ``.github/workflows/spec-drift.yml`` on a schedule. If the download
differs from the committed copy, the workflow runs the suite against it and
opens a pull request — that PR is the early warning that the API changed under
us.

Exits 0 whether or not anything changed; the workflow diffs the working tree.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

SPEC_PATH = Path(__file__).resolve().parents[1] / "openapi" / "huuray-v4.json"
DEFAULT_URL = "https://api.huuray.com/swagger/v4/swagger.json"


def main() -> int:
    url = os.environ.get("HUURAY_SPEC_URL", DEFAULT_URL)

    response = httpx.get(url, timeout=30.0)
    if response.status_code != 200:
        print(f"Failed to fetch spec: HTTP {response.status_code} from {url}", file=sys.stderr)
        return 1

    incoming = response.json()

    version = incoming.get("info", {}).get("version")
    if version != "v4":
        print(
            f'Refusing to write: expected info.version "v4", got "{version}".',
            file=sys.stderr,
        )
        print(
            "This SDK targets v4 only. A version change is a deliberate decision, not a sync.",
            file=sys.stderr,
        )
        return 1

    incoming_text = json.dumps(incoming, indent=2) + "\n"
    current = SPEC_PATH.read_text(encoding="utf-8") if SPEC_PATH.exists() else ""

    if current == incoming_text:
        print("Spec unchanged.")
        return 0

    # Explicit newline="\n": the vendored copy must be byte-identical on every
    # runner, or the drift job would report a diff on Windows every week.
    # (Path.write_text gained a newline argument only in 3.10.)
    with SPEC_PATH.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(incoming_text)
    print("Spec CHANGED — the diff must be reviewed and the gates re-run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
