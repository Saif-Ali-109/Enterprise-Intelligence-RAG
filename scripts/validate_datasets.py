#!/usr/bin/env python3
"""Validate the two committed data files against their committed JSON Schemas.

FR-032, FR-063, FR-064. The point of this script is that two guarantees are
enforced by the FILE FORMAT rather than by reviewer discipline:

  1. `data/source_manifest.json` has no field in which page content could be
     recorded. `additionalProperties: false` at every level means adding one
     fails validation, so a scraped page can never be committed (SC-015).
  2. `evaluation/golden_questions.json` has no URL field at all. Grading is by
     topic coverage, so a publisher reorganising their documentation cannot
     invalidate the dataset — and a pinned URL cannot be smuggled in through a
     new property either (FR-063, FR-064).

A schema that merely documents a rule can be ignored. A schema that a build step
refuses to pass is a rule. That is why this runs in `make validate-data`, which
`make up` depends on, and in `make check`.

Usage:
    python scripts/validate_datasets.py              # validate both files
    python scripts/validate_datasets.py --manifest   # manifest only
    python scripts/validate_datasets.py --dataset   # dataset only

Exit codes:
    0  both files valid
    1  a file is missing, unparseable, or invalid
    2  the validator itself could not run (missing dependency, bad schema)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Repo-relative. A path that depends on where the repository was cloned is a
# defect, not a convenience (tasks.md, "Paths in code and specs"). The repository
# root is derived from this file's own location, which is the one path that is
# correct by construction.
REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = REPO_ROOT / "specs" / "001-enterprise-knowledge-rag" / "contracts"

TARGETS: tuple[tuple[str, Path, str], ...] = (
    (
        "source manifest",
        REPO_ROOT / "data" / "source_manifest.json",
        "source-manifest.schema.json",
    ),
    (
        "evaluation dataset",
        REPO_ROOT / "evaluation" / "golden_questions.json",
        "evaluation-dataset.schema.json",
    ),
)


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _format_error(error: Any, *, indent: int = 0) -> list[str]:
    """Render a jsonschema error as something an author can act on.

    A bare "is not valid under any of the given schemas" is useless when the
    point is to tell someone which of their 30 questions broke which rule.

    `error` is a `jsonschema.ValidationError`, so its fields are **attributes**,
    not dict keys. An earlier version called `error.get("validator")` and crashed
    with `AttributeError` on the first real violation -- which means this
    function, the entire reason the validator is usable, had never run on a
    failing document. Every schema violation since it was written produced a
    traceback instead of a message. Found by planting a bad field and running the
    gate, which is the only way it could have been found: the gate is only ever
    run on data that is meant to be valid.

    A plain dict is also accepted, so the function stays testable without the
    `jsonschema` dependency installed.
    """
    pad = "  " * indent
    lines: list[str] = []

    def field(name: str, default: Any = None) -> Any:
        if isinstance(error, dict):
            return error.get(name, default)
        return getattr(error, name, default)

    def children_of(node: Any) -> list[Any]:
        raw = node.get("context") if isinstance(node, dict) else getattr(node, "context", None)
        return list(raw or [])

    if field("validator") in {"anyOf", "oneOf"}:
        # A closed schema with `oneOf`/`anyOf` branches reports every branch's
        # failure at once, which buries the real cause. Report the branch with the
        # fewest errors -- that is the closest match.
        contexts = children_of(error)
        if contexts:
            best = min(contexts, key=lambda ctx: len(children_of(ctx)))
            return _format_error(best, indent=indent)

    location = "/".join(str(part) for part in (field("absolute_path") or ())) or "(root)"
    lines.append(f"{pad}{location}: {field('message', 'invalid')}")

    for child in children_of(error):
        lines.extend(_format_error(child, indent=indent + 1))

    return lines
def validate(target_name: str, data_path: Path, schema_name: str) -> list[str]:
    """Validate one file. Returns a list of human-readable failures (empty = pass)."""
    schema_path = CONTRACTS / schema_name

    if not schema_path.is_file():
        return [f"{target_name}: schema not found at {schema_path}"]

    try:
        from jsonschema import Draft202012Validator
    except ImportError:  # pragma: no cover - environment problem, not a data problem
        print(
            "error: jsonschema is not installed.\n"
            "       run: make venv   (or pip install -r backend/requirements-dev.txt)",
            file=sys.stderr,
        )
        raise SystemExit(2) from None

    if not data_path.is_file():
        return [f"{target_name}: file not found at {data_path}"]

    try:
        instance = _load_json(data_path)
    except json.JSONDecodeError as exc:
        return [f"{target_name}: {data_path.name} is not valid JSON — {exc}"]

    schema = _load_json(schema_path)

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(instance), key=lambda err: list(err.absolute_path))

    if not errors:
        return []

    failures = [f"{target_name}: {data_path.name} failed {schema_name}"]
    for error in errors[:20]:
        failures.extend(f"  {line}" for line in _format_error(error))
    if len(errors) > 20:
        failures.append(f"  ... and {len(errors) - 20} more")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--manifest", action="store_true", help="validate only the source manifest")
    group.add_argument("--dataset", action="store_true", help="validate only the evaluation dataset")
    args = parser.parse_args()

    targets = TARGETS
    if args.manifest:
        targets = (TARGETS[0],)
    elif args.dataset:
        targets = (TARGETS[1],)

    failures: list[str] = []
    for target in targets:
        failures.extend(validate(*target))

    if failures:
        print("Dataset validation FAILED\n", file=sys.stderr)
        print("\n\n".join(failures), file=sys.stderr)
        print(
            "\n\nBoth guarantees below are enforced by the schema, not by discipline:\n"
            "  - the manifest has no field in which page content could be recorded (FR-032)\n"
            "  - the dataset has no URL field, so a question cannot pin a source (FR-063)",
            file=sys.stderr,
        )
        return 1

    for name, path, schema_name in targets:
        print(f"  ok  {name:20} {path.relative_to(REPO_ROOT)}  <- {schema_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
