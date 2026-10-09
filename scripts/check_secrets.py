#!/usr/bin/env python3
"""Report whether the two required secrets are resolvable, without printing either.

`make up` used to print "Requires GROQ_API_KEY and PINECONE_API_KEY in .env" on
every run, whether or not they were set — a static reminder that reads exactly
like a warning. A message that is always shown is a message nobody reads, and a
message nobody reads is worse than none: someone with a correctly configured
`.env` reads it as a problem they do not have.

So the check is made conditional, and the presence logic mirrors what Docker
Compose actually does, in this precedence:

1. the process environment — Compose substitutes `${VAR:-}` from the shell, and
   the shell wins over the file;
2. `.env` in the repository root, parsed the way Compose parses it: `KEY=value`,
   `#` comments, optional surrounding quotes, blank lines ignored.

Exit status is always 0. This is a diagnostic printed next to a stack that is
already starting; failing it would turn a warning into the failure it is
describing. The backend's own startup validation remains the thing that
actually refuses to serve — this only says whether it will.

No value is printed, ever. Presence is the whole answer (FR-042).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"

#: The only two settings declared required, mirroring `app/core/config.py`.
REQUIRED = ("GROQ_API_KEY", "PINECONE_API_KEY")


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main() -> int:
    from_file = parse_env_file(ENV_FILE)

    missing = [
        name
        for name in REQUIRED
        if not (os.environ.get(name) or from_file.get(name, "").strip())
    ]

    if missing:
        print("  Secrets: NOT all resolvable.")
        for name in missing:
            print(f"    - {name} is unset. The backend will exit at startup and name it.")
        if not ENV_FILE.is_file():
            print(f"    No {ENV_FILE.name} in the repository root. Copy .env.example and fill it in.")
        return 0

    print("  Secrets: both required keys resolvable (values never printed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())