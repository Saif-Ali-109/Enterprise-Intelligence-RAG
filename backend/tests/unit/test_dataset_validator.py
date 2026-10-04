"""The dataset validator's error reporting must actually work.

Two defects found by planting bad data and running the gate, which is the only
way either could have been found: the gate is normally run on data that is valid,
so its failure path was never exercised.

1. `_format_error` called `error.get("validator")` on a `jsonschema.ValidationError`,
   which exposes attributes rather than dict keys. It raised `AttributeError` on
   the first real violation, so every schema error since the validator was
   written surfaced as a traceback instead of a message — including the two
   guarantees the script's own output advertises.
2. The new `sitemap_url` / `scope_prefix` manifest fields needed to be proven to
   be *validated*, not merely accepted. A schema that documents a rule can be
   ignored; these are checked by planting a value that violates each one.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
VALIDATOR = REPO_ROOT / "scripts" / "validate_datasets.py"
MANIFEST = REPO_ROOT / "data" / "source_manifest.json"
QUESTIONS = REPO_ROOT / "evaluation" / "golden_questions.json"


def _run(*, manifest_only: bool = False) -> tuple[int, str]:
    """Run the validator and return `(exit_code, combined_output)`.

    Combined deliberately. The failure text goes to **stderr** while the success
    lines go to stdout, so a test that checks only one stream sees an empty string
    for the message it is asserting on -- which is how this file's first draft
    failed five of its own tests.
    """
    args = [sys.executable, str(VALIDATOR)]
    if manifest_only:
        args.append("--manifest")
    result = subprocess.run(args, capture_output=True, text=True, cwd=REPO_ROOT, check=False)
    return result.returncode, result.stdout + result.stderr


@pytest.fixture
def restore() -> object:
    """Guarantee the datasets are put back even when an assertion fails."""
    saved = {path: path.read_text() for path in (MANIFEST, QUESTIONS)}
    try:
        yield
    finally:
        for path, text in saved.items():
            path.write_text(text)


def _plant(path: Path, mutate) -> None:
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data, indent=2) + "\n")


class TestValidatorReportsRatherThanCrashes:
    def test_a_bad_field_produces_a_message_not_a_traceback(self, restore: None) -> None:
        """The defect: `AttributeError: 'ValidationError' object has no attribute 'get'`."""
        _plant(MANIFEST, lambda d: d["sources"][0].__setitem__("page_content", "scraped"))

        code, combined = _run(manifest_only=True)

        assert code == 1
        assert "Traceback" not in combined, "the validator still crashes instead of reporting"
        assert "AttributeError" not in combined
        # And the message must be specific enough to act on: which field, which path.
        assert "page_content" in combined
        assert "sources/0" in combined

    def test_the_promised_guarantees_are_still_reported_on_failure(self, restore: None) -> None:
        """The script advertises two guarantees in its output. They must survive
        an error, since that is exactly when a reader needs them."""
        _plant(MANIFEST, lambda d: d["sources"][0].__setitem__("page_content", "scraped"))

        _code, combined = _run(manifest_only=True)

        assert "FR-032" in combined
        assert "FR-063" in combined

    def test_a_clean_dataset_passes(self, restore: None) -> None:
        code, combined = _run()
        assert code == 0, combined
        assert "Traceback" not in combined


class TestManifestFieldsAreValidatedNotJustAccepted:
    """A closed schema is only useful if its fields are actually checked."""

    def test_scope_prefix_must_be_an_absolute_path(self, restore: None) -> None:
        """Without the leading slash it would be joined onto a host and silently
        match nothing — the source would crawl zero pages and look merely empty."""
        _plant(
            MANIFEST,
            lambda d: d["sources"][4].__setitem__("scope_prefix", "cloud/jira/platform/"),
        )

        code, combined = _run(manifest_only=True)

        assert code == 1
        assert "scope_prefix" in combined

    def test_sitemap_url_must_be_http_or_https(self, restore: None) -> None:
        """A `file://` or `ftp://` sitemap would be a local-read primitive aimed
        by a manifest that is otherwise all remote URLs."""
        _plant(
            MANIFEST,
            lambda d: d["sources"][0].__setitem__(
                "sitemap_url", "ftp://support.atlassian.com/x.xml"
            ),
        )

        code, combined = _run(manifest_only=True)

        assert code == 1
        assert "sitemap_url" in combined

    def test_an_unknown_field_is_still_refused(self, restore: None) -> None:
        """The guarantee that no page content can be committed (FR-032, SC-015)."""
        _plant(MANIFEST, lambda d: d["sources"][0].__setitem__("html", "<html>...</html>"))

        code, combined = _run(manifest_only=True)

        assert code == 1
        assert "not allowed" in combined

    def test_a_question_missing_a_required_field_is_reported_with_its_path(
        self, restore: None
    ) -> None:
        """`id` is the key `evaluation_results.question_id` refers to, so a
        question without one cannot be attributed to a per-question result."""
        _plant(QUESTIONS, lambda d: d["questions"][0].pop("id", None))

        code, combined = _run()

        assert code == 1
        assert "questions/0" in combined
        assert "'id' is a required property" in combined
