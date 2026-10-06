"""The gold set: loaded, schema-validated, fingerprinted (T140).

**Validation at load time or not at all.** A dataset that fails its schema
near the end of a 32-question run wastes the one thing the run cannot return —
every answer it asked for. The committed
`contracts/evaluation-dataset.schema.json` is therefore applied before the
first question is graded, and an invalid file refuses the run with a named
reason rather than quietly changing what "32 questions" means.

**The fingerprint is a claim about comparability.** `dataset_version` is a
human label that two edits of the file can share without lying very hard;
`sha256` of the canonicalised content cannot. A run is only ever compared
against runs of the same fingerprint (FR-036, data-model.md).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]

from app.schemas.evaluations import EvaluationDataset, EvaluationDatasetQuestion

# The dataset lives at the repository root, three parents above this file. The
# runner's cwd cannot be relied on (uvicorn may start from anywhere), so the
# default is anchored to the package and overridable by setting.
DEFAULT_DATASET_PATH = Path(__file__).resolve().parents[3] / "evaluation" / "golden_questions.json"


class DatasetInvalid(Exception):
    """The gold set could not be trusted, for a named reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _schema() -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parents[3]
        / "specs"
        / "001-enterprise-knowledge-rag"
        / "contracts"
        / "evaluation-dataset.schema.json"
    )
    result: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return result


def load_dataset(path: Path | str | None = None) -> tuple[EvaluationDataset, dict[str, Any]]:
    """Load and validate the gold set. Return the typed model and the raw object.

    Raises :class:`DatasetInvalid` when the file is missing, unreadable, fails
    the committed schema, or carries two questions under one id — ids are the
    join key to `evaluation_results`, and a duplicate would make a per-question
    audit row ambiguous.
    """
    resolved = Path(path) if path is not None else DEFAULT_DATASET_PATH
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DatasetInvalid(f"the dataset file does not exist: {resolved}") from exc
    except json.JSONDecodeError as exc:
        raise DatasetInvalid(f"the dataset is not JSON: {exc}") from exc

    try:
        jsonschema.validate(instance=raw, schema=_schema())
    except jsonschema.ValidationError as exc:
        raise DatasetInvalid(f"the dataset fails its schema: {exc.message}") from exc

    # The committed JSON Schema is verified above; what the API serves is the
    # closed openapi shape, which drops the file's grading annotations
    # (`tags`, `acceptable_alternatives`) into the registry tables and the raw
    # handle rather than onto a response a client would have to parse around.
    projected = {
        "dataset_version": raw["dataset_version"],
        "generated_at": raw.get("generated_at"),
        "questions": [
            {
                "id": q["id"],
                "question": q["question"],
                "category": q["category"],
                "difficulty": q["difficulty"],
                "is_unsupported": q["is_unsupported"],
                "expected": {
                    "must_refuse": q["expected"]["must_refuse"],
                    "product": q["expected"].get("product"),
                    "category": q["expected"].get("category"),
                    "heading_path": q["expected"].get("heading_path", []),
                    "topics": q["expected"]["topics"],
                    "notes": q["expected"].get("notes"),
                },
            }
            for q in raw["questions"]
        ],
    }
    try:
        dataset = EvaluationDataset.model_validate(projected)
    except ValueError as exc:
        raise DatasetInvalid(f"the dataset fails its schema: {exc}") from exc

    ids = [q.id for q in dataset.questions]
    if len(ids) != len(set(ids)):
        raise DatasetInvalid("two questions carry the same id")
    return dataset, raw


def dataset_fingerprint(raw: dict[str, Any]) -> str:
    """sha256 of the canonicalised dataset — the comparability anchor."""
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def by_id(dataset: EvaluationDataset) -> dict[str, EvaluationDatasetQuestion]:
    return {q.id: q for q in dataset.questions}
