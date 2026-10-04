"""Conformance between the running app's OpenAPI document and the ratified contract.

T036, FR-001. `specs/001-enterprise-knowledge-rag/contracts/openapi.yaml` is the
agreed HTTP surface. This file checks the app against it in both directions.

**Both directions matter, and one alone is a common mistake.** Checking that the
contract describes what the app does catches drift in the implementation. It does
not catch a contract that omits an endpoint the app has added, or one that
documents a field the app stopped returning — both of which are how a contract
quietly stops being the source of truth. So every path in the generated document
must appear in the contract, and every path in the contract must appear in the
generated document.

The second half of this file is the reasoning-leak check. FR-034 forbids exposing
chain-of-thought, and a contract conformance test is the right place to prove it
*structurally*: if no schema anywhere in the document can carry a field whose name
matches `REASONING_KEY_PATTERN`, then no client — including a future one this
repository does not contain — can discover a way to read deliberation. The check
is a source-level grep of the generated document, which catches a field that a
schema technically permits but never populates, and which a response test would
miss until a model actually leaked one.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.contract

#: `specs/001-enterprise-knowledge-rag/contracts/` — repo-relative, so this
#: resolves identically on every machine.
_CONTRACTS = (
    Path(__file__).resolve().parents[3] / "specs" / "001-enterprise-knowledge-rag" / "contracts"
)
_CONTRACT_PATH = _CONTRACTS / "openapi.yaml"


def _contract() -> dict[str, Any]:
    if not _CONTRACT_PATH.is_file():
        pytest.fail(f"the contract is missing: {_CONTRACT_PATH.relative_to(Path.cwd())}")
    return yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))


def _generated() -> dict[str, Any]:
    """The app's own OpenAPI document, built exactly as it is served."""
    from app.main import create_app

    return create_app(enable_probes=False).openapi()


# ============================================================================
# The contract file itself
# ============================================================================


class TestContractFile:
    def test_the_contract_is_valid_openapi_3_1(self) -> None:
        """A contract that will not parse cannot constrain anything."""
        contract = _contract()

        assert contract.get("openapi", "").startswith("3.1"), (
            f"expected OpenAPI 3.1.x, found {contract.get('openapi')!r}"
        )

    def test_the_contract_is_internally_valid(self) -> None:
        """Checked by an independent validator, not by this file's own parsing.

        The point of a separate validator is that it does not share this
        repository's assumptions about what it wrote.
        """
        from openapi_spec_validator import validate

        validate(_contract())

    def test_every_operation_declares_responses(self) -> None:
        """An operation with no documented success response cannot be called.

        `DELETE /sources/{source_id}` documents only `204` and `404`, which is
        correct — but a path documenting nothing but `404` is a specification
        mistake rather than a runtime one, and it is worth catching here.
        """
        contract = _contract()
        methods = {"get", "post", "put", "patch", "delete", "head"}

        for path, operations in contract["paths"].items():
            for method, operation in operations.items():
                if method not in methods:
                    continue
                responses = operation.get("responses", {})
                assert responses, f"{method.upper()} {path} declares no responses"
                successes = [status for status in responses if str(status).startswith("2")]
                assert successes, (
                    f"{method.upper()} {path} has no 2xx response; "
                    f"it documents only {sorted(responses)}"
                )


# ============================================================================
# Paths: the contract and the app must agree, in both directions
# ============================================================================


def _base_path(contract: dict[str, Any]) -> str:
    """The path portion of the contract's first server URL.

    The contract declares `servers: [http://localhost:8000/api/v1]` and writes
    its paths relative to that — `/chat`, not `/api/v1/chat`. FastAPI generates
    absolute paths. Comparing them directly reports every path as missing on
    both sides at once, which is the sort of false failure that gets a
    conformance test deleted rather than fixed, so the prefix is resolved here
    explicitly and asserted on its own below.
    """
    from urllib.parse import urlsplit

    servers = contract.get("servers") or []
    if not servers:
        return ""

    return urlsplit(servers[0].get("url", "")).path.rstrip("/")


def _contract_operations(contract: dict[str, Any]) -> dict[str, set[str]]:
    """Absolute path → methods, for comparison against the generated document."""
    base = _base_path(contract)
    methods = {"get", "post", "put", "patch", "delete", "head"}
    return {
        f"{base}{path}": {method for method in operations if method in methods}
        for path, operations in contract.get("paths", {}).items()
    }


def _generated_operations(document: dict[str, Any]) -> dict[str, set[str]]:
    methods = {"get", "post", "put", "patch", "delete", "head"}
    return {
        path: {method for method in operations if method in methods}
        for path, operations in document.get("paths", {}).items()
    }


def _paths(document: dict[str, Any]) -> set[str]:
    return set(document.get("paths", {}))


#: Contract paths that are specified but **not yet built**, keyed by the task
#: that will build them.
#:
#: The contract describes the whole API; the app grows towards it one phase at a
#: time. A conformance test that demanded all seventeen paths would fail until
#: the last phase and would be deleted long before that. So the shortfall is
#: recorded here instead, which makes the test useful now and keeps it honest:
#:
#: - an endpoint **added** to the app that is not in the contract still fails
#:   (`test_the_app_exposes_nothing_undocumented`), which is the drift that
#:   matters;
#: - an entry **left behind** after its task lands fails
#:   (`test_the_unbuilt_list_contains_no_built_endpoint`), so the list cannot rot
#:   into a permanent excuse;
#: - a task **missing** from this list fails (`test_every_contract_path_is_either
#:   built_or_tracked`), so a new contract path cannot be quietly skipped.
#: Contract paths with no implementation yet, and the task that owns them.
#:
#: `/chat` and `/chat/{request_id}` left this list on 2026-10-04 (T090), which is
#: what `test_the_pending_list_contains_no_endpoint_that_already_works` exists to
#: catch: an entry that no longer describes anything is a list nobody reads.
_PENDING = {
    "/chat/stream": "T127",
    "/evaluations/dataset": "T148",
    "/evaluations/runs": "T148",
    "/evaluations/runs/{run_id}": "T148",
    "/evaluations/runs/{run_id}/results": "T148",
}


class TestPathAgreement:
    def test_the_server_base_path_is_what_the_app_mounts(self) -> None:
        """The prefix is the thing being reconciled, so assert it directly.

        Without this, a change to either side's base path produces a wall of
        per-path failures whose common cause is not visible in any one of them.
        """
        from app.core.config import get_settings

        base = _base_path(_contract())

        assert base, "the contract declares no server base path"
        assert base == get_settings().api_prefix, (
            f"the contract's server base path is {base!r} but the app is mounted "
            f"at {get_settings().api_prefix!r}"
        )

    def test_every_contract_path_is_either_built_or_tracked(self) -> None:
        """The accounting must balance, in both directions.

        A contract path that is neither implemented nor listed in `_PENDING` is
        an endpoint nobody is building — a specification promise with no owner.
        """
        contract_ops = _contract_operations(_contract())
        generated = set(_generated_operations(_generated()))

        unaccounted = (
            set(contract_ops) - generated - {f"{_base_path(_contract())}{p}" for p in _PENDING}
        )

        assert unaccounted == set(), (
            f"the contract documents paths that are neither implemented nor "
            f"tracked in _PENDING: {sorted(unaccounted)}"
        )

    def test_the_pending_list_only_names_real_contract_paths(self) -> None:
        """A typo in `_PENDING` would otherwise mask a real gap.

        If `/chat/strem` were listed instead of `/chat/stream`, the genuine
        `stream` path would look "unaccounted for" only by accident, and if the
        contract were later edited to drop a path, the stale entry would keep
        passing forever.
        """
        contract_paths = set(_contract()["paths"])

        stale = set(_PENDING) - contract_paths
        assert stale == set(), f"_PENDING names paths the contract does not define: {sorted(stale)}"

    def test_the_pending_list_contains_no_endpoint_that_already_works(self) -> None:
        """The list is a work queue, not an excuse.

        When T090 lands, `/chat` must be removed from `_PENDING` in the same
        change. Leaving it there would make the test keep passing while
        reporting a false shortfall — the most corrosive failure mode a
        conformance test can have, because it looks correct.
        """
        base = _base_path(_contract())
        built = set(_generated_operations(_generated()))

        stale = {path for path in _PENDING if f"{base}{path}" in built}

        assert stale == set(), (
            f"these paths are implemented but still listed in _PENDING — remove "
            f"them: {sorted(stale)}"
        )

    def test_the_app_exposes_nothing_undocumented(self) -> None:
        """App → contract. The direction that matters most, and the one a
        "does the app implement the contract?" check never catches.

        An endpoint added to the app without a contract update is a public
        surface nobody reviewed and nobody documented — and the automatic docs
        page would advertise it as though it were specified.
        """
        contract_ops = _contract_operations(_contract())
        generated = set(_generated_operations(_generated()))

        extra = generated - set(contract_ops)
        assert extra == set(), (
            f"the app exposes paths the contract does not document: {sorted(extra)}"
        )

    def test_operations_agree_where_the_app_is_built(self) -> None:
        """Same path, different method: also drift.

        `GET /health` in the contract and `DELETE /health` in the app is a
        disagreement that a path-only check misses in both directions. Only the
        paths that exist are compared, since an unbuilt path has no methods yet.
        """
        contract_ops = _contract_operations(_contract())
        generated_ops = _generated_operations(_generated())

        disagreeing = {
            path: {"contract": sorted(methods), "app": sorted(generated_ops[path])}
            for path, methods in contract_ops.items()
            if path in generated_ops and generated_ops[path] != methods
        }

        assert disagreeing == {}, f"methods disagree: {disagreeing}"

    def test_the_built_endpoints_are_the_expected_ones(self) -> None:
        """A positive check, so a router that fails to mount reads as an empty
        implementation rather than as a pass.

        Every other test in this class is a difference check, and a difference
        check against a *smaller* implementation passes trivially. This one
        asserts the count is at least what is known to be built, so
        "the app serves nothing" cannot pass as conformance.
        """
        built = set(_generated_operations(_generated()))

        assert len(built) >= 2, f"only {len(built)} endpoint(s) are mounted: {sorted(built)}"
        assert f"{_base_path(_contract())}/health" in built, "/health is not mounted"
        assert f"{_base_path(_contract())}/config" in built, "/config is not mounted"


# ============================================================================
# The response envelope and the error codes
# ============================================================================


def _implemented_error_codes() -> set[str]:
    """The error codes this codebase can actually emit.

    Read from the `ErrorCode` enum rather than a hand-maintained list, so a new
    code cannot be added without this test noticing. A duplicated list would be
    a duplicated definition, and the two would drift.
    """
    from app.core.errors import ErrorCode

    return {str(code.value) for code in ErrorCode}


def _error_envelope(contract: dict[str, Any]) -> dict[str, Any]:
    """The inner envelope schema: `Error.properties.error`.

    The envelope is nested, not flat — `{error: {code, message, request_id}}` —
    so the fields and the enum are one level down. Reaching for
    `Error.properties.code` finds nothing, and a check written that way reports
    "the contract declares no error-code enum" rather than the shape problem,
    which is how a broken check gets mistaken for a missing field.
    """
    envelope = contract.get("components", {}).get("schemas", {}).get("Error", {})
    inner = envelope.get("properties", {}).get("error", {})
    return inner if isinstance(inner, dict) else {}


def _contract_error_codes(contract: dict[str, Any]) -> set[str]:
    """The enum the contract declares for `code`."""
    enum = _error_envelope(contract).get("properties", {}).get("code", {}).get("enum", [])
    return {str(value) for value in enum}


class TestErrorContract:
    def test_every_error_code_in_the_contract_is_implemented(self) -> None:
        """The contract's enum and the implementation's enum must be the same set.

        A code in the implementation but not the contract is undocumented
        behaviour a client cannot handle. A code in the contract but not the
        implementation is a promise the system cannot keep. Both directions are
        drift, so both are checked.
        """
        contract = _contract()
        declared = _contract_error_codes(contract)
        implemented = _implemented_error_codes()

        assert declared, "the contract declares no error-code enum"
        assert declared == implemented, (
            f"contract-only: {sorted(declared - implemented)}; "
            f"implementation-only: {sorted(implemented - declared)}"
        )

    def test_the_error_envelope_requires_code_message_and_request_id(self) -> None:
        """Every error body carries the same three fields.

        `code`, `message` and `request_id` are what the whole API promises on
        failure. A handler that omits one breaks every client that reads the
        envelope uniformly — including this repository's own frontend, which
        quotes the request id in a bug report.
        """
        inner = _error_envelope(_contract())

        assert inner, "the contract's Error schema has no inner envelope"
        required = set(inner.get("required", []))
        assert required == {"code", "message", "request_id"}, (
            f"the error envelope requires {sorted(required)}, expected code, message and request_id"
        )

    def test_the_error_envelope_is_closed(self) -> None:
        """`additionalProperties: false`, so a handler cannot add a field.

        An open error envelope is how a debugging aid becomes a leak: adding
        `error.response_body` to a 401 from a vendor SDK is a natural mistake,
        and that body contains the request that failed, which contains a
        credential.
        """
        for schema, name in (
            (_contract().get("components", {}).get("schemas", {}).get("Error", {}), "Error"),
            (_error_envelope(_contract()), "Error.error"),
        ):
            assert schema.get("additionalProperties") is False, (
                f"{name} permits additional properties; an error body must be closed"
            )

    def test_the_catalogue_has_not_shrunk(self) -> None:
        """FR-013 enumerates the codes the system distinguishes.

        A removed code means two failure modes are now reported identically, and
        the remedy for each is different — that is the entire reason they are
        separate. This is a floor rather than an exact count because the check
        above already pins the exact set.
        """
        assert len(_implemented_error_codes()) >= 14, (
            "the error catalogue has shrunk; two failure modes with different "
            "remedies are probably now reported identically"
        )

    def test_details_carries_no_free_form_user_input(self) -> None:
        """The one deliberately-open object in the envelope.

        `details` is `additionalProperties: true` because the field names vary by
        error — a validation error names fields, a state conflict names a state.
        The contract's own description constrains it to "field names and
        enumerated values only. Never free-form user input", and that constraint
        is the whole safety argument. Asserting the description keeps a future
        edit that widens it from passing silently.
        """
        details = _error_envelope(_contract()).get("properties", {}).get("details", {})

        assert details, "the error envelope declares no details object"
        assert details.get("additionalProperties") is True
        description = (details.get("description") or "").lower()
        assert "never free-form" in description, (
            f"the details object no longer documents its input constraint: {description!r}"
        )

    def test_no_success_schema_carries_an_error_code(self) -> None:
        """A `code` field on a 200 would be a status lie.

        A client that reads `code` and branches on it would treat a successful
        answer as a failure, or — worse — treat a failure as a success if the
        field is absent.
        """
        contract = _contract()
        error_codes = _contract_error_codes(contract)
        offenders: list[str] = []

        for name, schema in contract["components"]["schemas"].items():
            properties = schema.get("properties", {})
            code_spec = properties.get("code")
            if not isinstance(code_spec, dict) or "enum" not in code_spec:
                continue
            # Only the error envelope declares a `code` with an enum. A success
            # schema doing so would be reusing the field for a different meaning.
            if name != "Error":
                offenders.append(name)

        assert offenders == [], f"a non-error schema carries an enumerated `code`: {offenders}"
        assert error_codes

    def test_every_shared_error_response_uses_the_envelope(self) -> None:
        """The reusable responses in `components/responses` must all be it.

        A shared response that returns a different shape is a client that must
        special-case one error, and a special case is where the code that does
        not validate citations ends up.
        """
        contract = _contract()
        shared = contract.get("components", {}).get("responses", {})

        assert shared, "the contract declares no shared error responses"
        for name, response in shared.items():
            schema = response.get("content", {}).get("application/json", {}).get("schema", {})
            ref = schema.get("$ref", "")
            assert ref.endswith("/Error"), (
                f"the shared response {name!r} does not reference the Error schema (got {ref!r})"
            )


# ============================================================================
# The reasoning-leak check (FR-034)
# ============================================================================


#: The one field whose name contains "reasoning" and is correct: an attestation
#: that reasoning is *not* exposed, always the constant `False` (FR-034). It is
#: the opposite of a leak, and a blanket substring check flags it — which is
#: worth recording, because a conformance test that fails on the requirement it
#: is enforcing tends to get deleted rather than fixed.
_ALLOWED_REASONING_NAMES = frozenset({"reasoning_exposed"})

#: Field names that would carry deliberation if a model ever returned one.
_REASONING_FIELD_NAMES = frozenset(
    {
        "reasoning",
        "reasoning_content",
        "reasoning_text",
        "chain_of_thought",
        "chainofthought",
        "chain-of-thought",
        "thoughts",
        "thinking",
        "scratchpad",
        "deliberation",
        "internal_reasoning",
        "model_thoughts",
        "raw_response",
    }
)


def _is_reasoning_leak(name: str) -> bool:
    """Whether a property name could carry a reasoning channel.

    Exact match against the known names, plus a substring rule for "reasoning",
    minus the one attestation field. The substring rule is what catches a newly
    invented `reasoning_summary` or `assistant_reasoning`, which a fixed list
    would miss until it shipped.
    """
    lowered = name.lower()
    if lowered in _ALLOWED_REASONING_NAMES:
        return False
    if lowered in _REASONING_FIELD_NAMES:
        return True
    return "reasoning" in lowered and "exposed" not in lowered


class TestNoReasoningIsExposed:
    """The structural half of FR-034: the API *cannot* carry deliberation.

    `app/generation/provider.py` refuses any response exposing a reasoning
    channel, and the unit tests prove it. This checks the other side — that the
    HTTP surface has no field through which deliberation could be read even if
    some future model returned one, or if some future handler passed it through
    by accident.

    A response test cannot do this. It would pass until a model actually leaked,
    which is the wrong time to find out. A schema check is evaluated on every
    run and fails the moment a field is added.
    """

    def test_the_attestation_is_the_only_reasoning_named_field(self) -> None:
        """`reasoning_exposed` must exist, and must be the constant `False`.

        Checked in both directions. Present-and-wrong means the contract claims
        something it does not deliver; absent means a reader cannot tell that the
        suppression is a decision rather than an oversight.
        """
        schemas = _generated()["components"]["schemas"]

        declaring = [
            name
            for name, schema in schemas.items()
            if "reasoning_exposed" in schema.get("properties", {})
        ]
        assert declaring, "no schema declares reasoning_exposed; the attestation is missing"

        for name in declaring:
            spec = schemas[name]["properties"]["reasoning_exposed"]
            assert spec.get("default") is False or spec.get("const") is False, (
                f"{name}.reasoning_exposed does not default to false"
            )

    def test_no_other_schema_property_could_carry_a_reasoning_channel(self) -> None:
        """No property anywhere may carry deliberation.

        Checked against the *generated* document rather than the contract alone,
        because the contract is the thing that is supposed to be correct — and
        this check must be independent of the path check that already compares
        the two documents.
        """
        offenders = _find_leaking_properties(_generated())

        assert offenders == [], f"a schema property could carry a reasoning channel: {offenders}"

    def test_the_contract_declares_no_reasoning_field_either(self) -> None:
        """The contract is what a client generator reads, so it matters
        independently of what the app currently generates."""
        offenders = _find_leaking_properties(_contract())

        assert offenders == [], f"the contract exposes a reasoning field: {offenders}"

    def test_no_schema_permits_an_arbitrary_additional_object(self) -> None:
        """A response schema that accepts any property is a standing invitation
        to leak a field nobody reviewed.

        With a bare `additionalProperties: true`, the checks above become
        advisory: a `reasoning` key could appear in a response at any time
        without the schema changing. Only the bare boolean is a hole — a *typed*
        `additionalProperties` is a map with a declared value schema, which is
        how `HealthResponse.dependencies` is legitimately expressed.
        """
        offenders = _find_unbounded_objects(_generated())

        assert offenders == set(), (
            f"response schemas permit arbitrary additional properties: {sorted(offenders)}"
        )

    def test_a_property_map_is_typed_not_free_form(self) -> None:
        """The positive counterpart of the check above.

        `HealthResponse.dependencies` is a map keyed by dependency name. A bare
        `additionalProperties: true` there would mean a dependency could report
        any object at all, including one carrying a `reasoning` key — so the
        map's value type must be a declared schema.
        """
        dependencies = _generated()["components"]["schemas"]["HealthResponse"]["properties"][
            "dependencies"
        ]

        extra = dependencies.get("additionalProperties")
        assert extra is not None, "HealthResponse.dependencies declares no value type"
        assert extra is not True, "HealthResponse.dependencies accepts any value"

    def test_the_prompt_is_never_echoed_in_a_response(self) -> None:
        """A response field for the prompt verbatim would turn the API into a
        content-distribution surface, and the prompt is where the evidence text
        lives."""
        forbidden_in_responses = (
            "prompt",
            "system_prompt",
            "raw_prompt",
            "evidence_text",
            "chunk_text",
        )
        document = _generated()

        for field in forbidden_in_responses:
            offenders = _find_property_in_success_responses(document, field)
            assert offenders == [], (
                f"a success response exposes {field!r}, which would echo model input: {offenders}"
            )


# ============================================================================
# Secret exposure
# ============================================================================


class TestNoSecretsInTheContract:
    def test_no_schema_exposes_a_value_field_under_secrets(self) -> None:
        """`SecretPresence` is presence-only by design (FR-042).

        A `value` field under `secrets` would be the whole leak, and it would
        appear in the generated client for every consumer.
        """
        document = _generated()

        offenders: list[str] = []
        for name, schema in document.get("components", {}).get("schemas", {}).items():
            properties = set(schema.get("properties", {}))
            if "configured" in properties or "secrets" in name.lower():
                if {"value", "secret", "key"} & properties:
                    offenders.append(name)
                if {"value", "secret", "key"} & set(schema.get("required", [])):
                    offenders.append(f"{name} (required)")

        assert offenders == [], f"a secrets schema exposes a value field: {offenders}"

    def test_no_response_schema_is_named_like_a_credential(self) -> None:
        document = _generated()
        pattern = re.compile(r"(api[_-]?key|secret|credential|token|password)", re.IGNORECASE)

        offenders = [
            name
            for name in document.get("components", {}).get("schemas", {})
            if pattern.search(name)
            and "presence" not in name.lower()
            and "secret" not in name.lower()
        ]

        assert offenders == [], f"a schema is named like a credential: {offenders}"


# ============================================================================
# Corpus-independent invariants
# ============================================================================


class TestContractInvariants:
    def test_the_retrieval_budget_is_the_documented_one(self) -> None:
        """12 candidates, 6 reranked, 3–6 evidence units.

        Asserted in three places, because a mismatch between the documented
        budget and the running one produces results nobody can interpret: the
        numbers in an answer stop meaning what the documentation says.

        The response uses the *contract's* key names (`evidence_min`,
        `evidence_max`) while `Settings` uses `evidence_min_units`,
        `evidence_max_units`. Both spellings are checked here, because the
        renaming is deliberate — the contract is the public interface — and a
        future edit that "corrects" one side to match the other would break a
        client.
        """
        from app.core.config import get_settings

        settings = get_settings()

        assert (settings.retrieval_candidate_pool, settings.retrieval_rerank_top_n) == (12, 6)
        assert (settings.evidence_min_units, settings.evidence_max_units) == (3, 6)

        # The contract must document the same budget, under its own names.
        rendered = yaml.safe_dump(_contract()).lower()
        assert "candidate_pool" in rendered
        assert "rerank_top_n" in rendered
        assert "evidence_min" in rendered
        assert "evidence_max" in rendered

    def test_threshold_bearing_scores_are_bounded_to_the_unit_interval(self) -> None:
        """The scores a gate is compared against must be in the gate's range.

        `MIN_RERANK_SCORE` is a threshold on a rerank score, so a rerank score
        outside [0, 1] would make the threshold mean something different from
        what its name says — and, worse, would make the *observed* pass rate
        uninterpretable, because a vendor that changed its scaling would change
        the system's behaviour without changing any code here.

        Only threshold-bearing fields are in scope. See
        `test_similarity_scores_are_not_bounded_below_zero` and
        `test_selection_scores_are_not_forced_onto_one_scale` for the two
        families that must **not** carry these bounds.
        """
        contract = _contract()
        checked: list[str] = []

        for name, schema in contract["components"]["schemas"].items():
            for field, spec in (schema.get("properties") or {}).items():
                if field != "rerank_score":
                    continue
                checked.append(f"{name}.{field}")
                assert spec.get("minimum") == 0, f"{name}.{field} allows a negative value"
                assert spec.get("maximum") == 1, f"{name}.{field} allows a value above 1"

        assert checked, "no rerank_score found in the contract; the gate has no schema"

    def test_similarity_scores_are_not_bounded_below_zero(self) -> None:
        """Cosine similarity is [-1, 1], and pretending otherwise is a bug.

        A `minimum: 0` on a similarity score would be *wrong*, not merely
        loose: two chunks about opposite operations genuinely score below zero,
        and a schema that forbids that value would have to reject a correct
        result. The bound that is legitimate is -1, and the field is documented
        as a similarity rather than as a probability.

        This test exists because the first version of the file asserted
        [0, 1] for everything named `*score*`, which would have forced exactly
        that error into the contract to satisfy a test.
        """
        contract = _contract()
        checked: list[str] = []

        for name, schema in contract["components"]["schemas"].items():
            for field, spec in (schema.get("properties") or {}).items():
                if field != "similarity_score":
                    continue
                checked.append(f"{name}.{field}")
                assert spec.get("maximum") in (None, 1), (
                    f"{name}.{field} exceeds the cosine range; similarity is not a probability"
                )
                # Either -1 or no floor at all. A floor of 0 would reject
                # legitimately dissimilar chunks rather than merely reporting
                # them, which is a different and wrong behaviour.
                assert spec.get("minimum") in (None, -1), (
                    f"{name}.{field} forbids negative similarity, which cosine can produce"
                )

        assert checked, "no similarity_score found in the contract"

    def test_selection_scores_are_not_forced_onto_one_scale(self) -> None:
        """FR-021: reported independently, never averaged into one number.

        The contract's five selection signals are deliberately *not* bounded to
        a common interval, and must not be. Putting them all in [0, 1] would
        assert they share a scale, which is the assumption the selector is
        forbidden from making — a schema that implied a shared scale would
        license the averaging the requirement prohibits, and would do so
        somewhere less visible than the selector itself.

        What is checked instead is the property that actually matters: no
        combined score exists. A `total_score` or `combined_score` field would
        be the averaging FR-021 forbids, presented as a number a reader could
        trust.
        """
        contract = _contract()
        selection = contract["components"]["schemas"]["SelectedEvidence"]["properties"][
            "selection_scores"
        ]
        signals = set(selection["properties"])

        assert signals == {
            "semantic_relevance",
            "rerank_strength",
            "metadata_match",
            "heading_relevance",
            "diversity_penalty",
        }, f"the selection signals changed: {sorted(signals)}"

        # No shared interval is declared for the group as a whole.
        assert "minimum" not in selection and "maximum" not in selection, (
            "selection_scores declares a shared range, which implies the signals "
            "are comparable and licenses the averaging FR-021 forbids"
        )

        # And nothing anywhere combines them into one.
        forbidden = (
            "total_score",
            "combined_score",
            "overall_score",
            "aggregate_score",
            "relevance_score",
        )
        for name, schema in contract["components"]["schemas"].items():
            fields = set(schema.get("properties") or {})
            collisions = fields & set(forbidden)
            assert collisions == set(), (
                f"{name} exposes a combined score {sorted(collisions)}; FR-021 requires "
                f"the signals to be reported independently"
            )

    def test_confidences_are_bounded_where_declared(self) -> None:
        """`QueryAnalysis.confidence` *is* a probability, so it is bounded.

        The per-field confidences are a different family from the scores: a
        confidence is a probability over a three-way classification, so [0, 1]
        is the correct domain and the contract already declares it. Keeping the
        check means the score checks can stay narrow without losing coverage.
        """
        contract = _contract()
        confidence = contract["components"]["schemas"]["QueryAnalysis"]["properties"]["confidence"]

        for field, spec in confidence["properties"].items():
            assert spec.get("minimum") == 0, f"confidence.{field} allows a negative value"
            assert spec.get("maximum") == 1, f"confidence.{field} allows a value above 1"

    def test_latency_is_an_integer_where_declared(self) -> None:
        """A float `latency_ms` fails the contract's own validation.

        `round(x, 2)` produces a float, and that is exactly what the first
        version of the health route returned — the health endpoint answered 200
        with a body the contract rejected.
        """
        contract = _contract()
        checked: list[str] = []

        for name, schema in contract["components"]["schemas"].items():
            for field, spec in (schema.get("properties") or {}).items():
                if "latency" in field.lower() and "ms" in field.lower():
                    checked.append(f"{name}.{field}")
                    # Nullable is fine (`[integer, "null"]`); a float is not.
                    assert _types(spec) <= {"integer", "null"}, (
                        f"{name}.{field} declares {sorted(_types(spec))}, not an integer"
                    )

        assert checked, "no latency field found in the contract"

    def test_a_dependency_status_is_never_a_free_string(self) -> None:
        """`/health` status is an enum. A free string would let an unprobeable
        dependency report something a monitor cannot act on."""
        contract = _contract()

        statuses = set()
        for schema in contract.get("components", {}).get("schemas", {}).values():
            for field, spec in schema.get("properties", {}).items():
                if field == "status" and "enum" in spec:
                    statuses |= set(spec["enum"])

        assert statuses, "no status enum found in the contract"
        # An unprobeable dependency must be `unavailable`, never defaulted to ok.
        assert "unavailable" in statuses
        assert "degraded" in statuses


# ============================================================================
# Helpers
# ============================================================================


def _find_property(document: Any, needle: str) -> list[str]:
    """Every dotted path in the document whose property name contains `needle`."""
    found: list[str] = []

    def walk(node: Any, trail: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    for property_name in value:
                        if needle.lower() in property_name.lower():
                            found.append(f"{trail}.properties.{property_name}")
                walk(value, f"{trail}.{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{trail}[{index}]")

    walk(document, "$")
    return found


def _find_property_in_success_responses(document: Any, needle: str) -> list[str]:
    """Like `_find_property`, but only within 2xx response bodies."""
    found: list[str] = []

    def walk(node: Any, trail: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "responses" and isinstance(value, dict):
                    for status, operation in value.items():
                        if not str(status).startswith("2"):
                            continue
                        for match in _find_property(operation, needle):
                            found.append(f"{trail}.responses.{status}{match}")
                walk(value, f"{trail}.{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{trail}[{index}]")

    walk(document, "$")
    return found


def _find_leaking_properties(document: Any) -> list[str]:
    """Every dotted path in the document whose property name could carry reasoning."""
    found: list[str] = []

    def walk(node: Any, trail: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    for name in value:
                        if _is_reasoning_leak(name):
                            found.append(f"{trail}.properties.{name}")
                walk(value, f"{trail}.{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{trail}[{index}]")

    walk(document, "$")
    return found


def _find_unbounded_objects(document: Any) -> set[str]:
    """Schema names that permit arbitrary extra properties.

    Only a **bare boolean** `true` counts. A typed `additionalProperties` is a
    map whose values are constrained to a declared schema, which is how a
    dependency-name-keyed object is properly expressed — flagging that would
    have reported `HealthResponse.dependencies` as a hole when it is in fact the
    correct way to model a fixed set of named dependencies.
    """
    unbounded: set[str] = set()

    for name, schema in document.get("components", {}).get("schemas", {}).items():
        if not isinstance(schema, dict):
            continue
        if schema.get("additionalProperties") is True:
            unbounded.add(name)

        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            continue
        for field, spec in properties.items():
            if not isinstance(spec, dict):
                continue
            if spec.get("additionalProperties") is True:
                unbounded.add(f"{name}.{field}")
            # An untyped object with no declared properties is a hole with more
            # indirection: `{"type": "object"}` constrains nothing.
            if (
                spec.get("type") == "object"
                and not spec.get("properties")
                and "additionalProperties" not in spec
            ):
                unbounded.add(f"{name}.{field}")

    return unbounded


def _types(spec: dict[str, Any]) -> set[str]:
    """A schema's declared types.

    OpenAPI 3.1 replaced the `nullable: true` keyword with a type union, so a
    nullable integer is `type: [integer, "null"]` rather than
    `type: integer, nullable: true`. Comparing against a bare string therefore
    fails on every optional field, which reads as a contract defect and is
    actually a version difference.
    """
    declared = spec.get("type")
    if declared is None:
        return set()
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return {str(item) for item in declared}
    return set()
