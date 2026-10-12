#!/usr/bin/env python3
"""Render docs/API.md's reference tables from the OpenAPI contract.

The contract is normative. This script exists so the *reference* in the docs
cannot drift from it: the prose around the tables is hand-written and explains
intent, while the tables themselves — every path, method, status code, and
schema name — are generated. `make api-docs-check` fails when they are out of
date, and it is wired into `make check`, so a contract change without a
regenerated reference is a failing gate rather than a documentation bug found
by a reader.

Why generate a table rather than write it: a hand-written endpoint list is a
second copy of the contract, and the failure mode of a second copy is that it
stops matching the first one silently. Nothing here re-describes behaviour — if
this file and the contract disagree about what an endpoint does, the contract
wins and this file is wrong.

Usage:
    scripts/generate_api_docs.py           # rewrite docs/API.md in place
    scripts/generate_api_docs.py --check   # exit 1 if docs/API.md is stale
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = REPO_ROOT / "specs" / "001-enterprise-knowledge-rag" / "contracts" / "openapi.yaml"
TARGET = REPO_ROOT / "docs" / "API.md"

BEGIN = "<!-- BEGIN GENERATED: contracts/openapi.yaml -->"
END = "<!-- END GENERATED -->"

METHODS = ("get", "post", "patch", "put", "delete")


def load_contract() -> dict[str, Any]:
    try:
        import yaml
    except ImportError:  # pragma: no cover - developer environment without PyYAML
        print(
            "PyYAML is required to render the reference: pip install -r "
            "backend/requirements-dev.txt",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def summarise(operation: dict[str, Any]) -> str:
    """First sentence of the description, with newlines collapsed."""
    description = (operation.get("description") or "").strip()
    summary = (operation.get("summary") or "").strip()
    text = description or summary
    if not text:
        return summary or "—"
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return " ".join(first.split())


def render(contract: dict[str, Any]) -> str:
    lines: list[str] = [BEGIN, ""]
    lines.append(f"Contract `{CONTRACT.relative_to(REPO_ROOT)}` · "
                 f"{contract.get('info', {}).get('version', 'unversioned')}")
    lines.append("")

    paths: dict[str, dict[str, Any]] = contract.get("paths", {})
    by_tag: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}

    for path, item in paths.items():
        for method in METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            tags = operation.get("tags") or ["other"]
            by_tag.setdefault(tags[0], []).append((method.upper(), path, operation))

    for tag in sorted(by_tag):
        lines.append(f"### `{tag}`")
        lines.append("")
        lines.append("| Method | Path | Summary |")
        lines.append("|---|---|---|")
        for method, path, operation in sorted(by_tag[tag], key=lambda row: (row[1], row[0])):
            summary = summarise(operation).replace("|", "\\|")
            lines.append(f"| `{method}` | `{path}` | {summary} |")
        lines.append("")

    lines.append("### Response codes used by this contract")
    lines.append("")
    lines.append("| Status | Where |")
    lines.append("|---|---|")
    for path, item in sorted(paths.items()):
        for method in METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            codes = sorted((operation.get("responses") or {}).keys())
            rendered = ", ".join(f"`{code}`" for code in codes) or "—"
            lines.append(f"| `{method.upper()} {path}` | {rendered} |")
    lines.append("")

    lines.append("### Schemas")
    lines.append("")
    lines.append("| Schema | Required fields |")
    lines.append("|---|---|")
    for name in sorted((contract.get("components", {}).get("schemas") or {})):
        schema = contract["components"]["schemas"][name] or {}
        required = schema.get("required") or []
        rendered = ", ".join(f"`{field}`" for field in required) or "—"
        lines.append(f"| `{name}` | {rendered} |")
    lines.append("")
    lines.append(END)
    return "\n".join(lines)


def splice(document: str, generated: str) -> str:
    pattern = re.compile(
        re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL
    )
    if not pattern.search(document):
        raise SystemExit(
            f"{TARGET} has no generated block. Add these markers where the tables belong:\n"
            f"  {BEGIN}\n  {END}"
        )
    return pattern.sub(lambda _: generated, document, count=1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if docs/API.md is stale")
    args = parser.parse_args()

    generated = render(load_contract())
    current = TARGET.read_text(encoding="utf-8")
    updated = splice(current, generated)

    if args.check:
        if updated != current:
            print(
                f"{TARGET.relative_to(REPO_ROOT)} is out of date with the contract.\n"
                f"Run: make api-docs",
                file=sys.stderr,
            )
            return 1
        print(f"api-docs current ({TARGET.relative_to(REPO_ROOT)})")
        return 0

    TARGET.write_text(updated, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())