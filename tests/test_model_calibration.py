import hashlib
import json
import math
from decimal import Decimal

import pytest


def _artifact_identity_hash(rows):
    identities = sorted(str(row.get("artifact_id") or "") for row in rows)
    canonical = json.dumps(identities, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _bind_evidence(rows, role):
    bound = [dict(row) for row in rows]
    model_name = "glm-4.5v" if role == "reference" else "glm-4.6v"
    run_id = "reference-run-001" if role == "reference" else "candidate-run-001"
    identity_hash = _artifact_identity_hash(bound)
    for row in bound:
        row.setdefault("model_name", model_name)
        row.setdefault("run_id", run_id)
        row.setdefault("role", role)
        row.setdefault("artifact_set_sha256", identity_hash)
    return bound


def _write_jsonl(path, rows, *, bind_identity=True):
    payload = list(rows)
    if bind_identity:
        role = "reference" if "reference" in path.stem else "candidate"
        payload = _bind_evidence(payload, role)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in payload), encoding="utf-8")
    return path


def _rows(latencies=(100, 120, 140), **overrides):
    result = []
    for index, latency in enumerate(latencies, 1):
        row = {
            "artifact_id": f"artifact-{index}",
            "accepted": True,
            "truth_finalized": True,
            "schema_valid": True,
            "latency_ms": latency,
            "p0": 0,
            "p1": 0,
            "p2": 0,
            "manual": 0,
            "entitlement_success": True,
        }
        row.update(overrides)
        result.append(row)
    return result


def _compare(reference, candidate):
    from model_calibration import compare_calibration

    return compare_calibration(reference, candidate)


def test_approval_requires_identical_finalized_zero_defect_faster_evidence(tmp_path):
    reference = _write_jsonl(tmp_path / "reference.jsonl", _rows((100, 130, 160)))
    candidate = _write_jsonl(tmp_path / "candidate.jsonl", _rows((90, 120, 150)))

    verdict = _compare(reference, candidate)

    assert verdict.approved is True
    assert verdict.accepted_identities_match is True
    assert verdict.truth_finalized is True
    assert verdict.schema_rate == 1.0
    assert verdict.p0 == verdict.p1 == verdict.p2 == verdict.manual == 0
    assert verdict.entitlement_success is True
    assert verdict.candidate_p50_ms <= verdict.reference_p50_ms
    assert verdict.candidate_p95_ms <= verdict.reference_p95_ms
    assert verdict.reasons == ()


def test_self_comparison_and_same_resolved_path_are_rejected(tmp_path):
    evidence = _write_jsonl(tmp_path / "reference.jsonl", _rows())

    verdict = _compare(evidence, evidence.parent / "." / evidence.name)

    assert verdict.approved is False
    assert "same_evidence_path" in verdict.reasons


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("run_id", "reference-run-001", "same_run_id"),
        ("role", "reference", "invalid_candidate_role"),
        ("model_name", "", "invalid_metric:model_name"),
        ("artifact_set_sha256", "0" * 64, "artifact_identity_hash_mismatch"),
    ],
)
def test_evidence_identity_binding_rejects_cross_run_role_model_and_hash(
    tmp_path, field, value, reason
):
    reference = _write_jsonl(tmp_path / "reference.jsonl", _rows())
    candidate_rows = _bind_evidence(_rows(), "candidate")
    for row in candidate_rows:
        row[field] = value
    candidate = _write_jsonl(
        tmp_path / "candidate.jsonl", candidate_rows, bind_identity=False
    )

    verdict = _compare(reference, candidate)

    assert verdict.approved is False
    assert reason in verdict.reasons


@pytest.mark.parametrize("field", ["model_name", "run_id", "role", "artifact_set_sha256"])
def test_missing_evidence_identity_field_rejects(tmp_path, field):
    reference = _write_jsonl(tmp_path / "reference.jsonl", _rows())
    candidate_rows = _bind_evidence(_rows(), "candidate")
    candidate_rows[0].pop(field)
    candidate = _write_jsonl(
        tmp_path / "candidate.jsonl", candidate_rows, bind_identity=False
    )

    verdict = _compare(reference, candidate)

    assert verdict.approved is False
    assert f"missing_metric:{field}" in verdict.reasons


@pytest.mark.parametrize("field", ["model_name", "run_id", "role", "artifact_set_sha256"])
def test_inconsistent_evidence_identity_field_rejects(tmp_path, field):
    reference = _write_jsonl(tmp_path / "reference.jsonl", _rows())
    candidate_rows = _bind_evidence(_rows(), "candidate")
    candidate_rows[-1][field] = f"different-{field}"
    candidate = _write_jsonl(
        tmp_path / "candidate.jsonl", candidate_rows, bind_identity=False
    )

    verdict = _compare(reference, candidate)

    assert verdict.approved is False
    assert f"inconsistent_evidence:{field}" in verdict.reasons


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"truth_finalized": False}, "truth_not_finalized"),
        ({"p0": 1}, "p0_nonzero"),
        ({"p1": 1}, "p1_nonzero"),
        ({"p2": 1}, "p2_nonzero"),
        ({"manual": 1}, "manual_nonzero"),
        ({"schema_valid": False}, "schema_invalid"),
        ({"entitlement_success": False}, "entitlement_failed"),
        ({"latency_ms": 1000}, "latency_regression"),
    ],
)
def test_every_correctness_entitlement_and_latency_gate_is_mandatory(tmp_path, override, reason):
    reference = _write_jsonl(tmp_path / "reference.jsonl", _rows())
    candidate = _write_jsonl(tmp_path / "candidate.jsonl", _rows(**override))

    verdict = _compare(reference, candidate)

    assert verdict.approved is False
    assert reason in verdict.reasons


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"p0": 1}, "p0_nonzero"),
        ({"p1": 1}, "p1_nonzero"),
        ({"p2": 1}, "p2_nonzero"),
        ({"manual": 1}, "manual_nonzero"),
        ({"schema_valid": False}, "schema_invalid"),
        ({"entitlement_success": False}, "entitlement_failed"),
    ],
)
def test_reference_evidence_must_also_pass_correctness_schema_and_entitlement(
    tmp_path, override, reason
):
    reference = _write_jsonl(tmp_path / "reference.jsonl", _rows(**override))
    candidate = _write_jsonl(tmp_path / "candidate.jsonl", _rows())

    verdict = _compare(reference, candidate)

    assert verdict.approved is False
    assert reason in verdict.reasons


def test_identically_unaccepted_artifact_is_not_allowed_to_disappear(tmp_path):
    reference_rows = _rows()
    candidate_rows = _rows()
    reference_rows[-1]["accepted"] = False
    candidate_rows[-1]["accepted"] = False

    verdict = _compare(
        _write_jsonl(tmp_path / "reference.jsonl", reference_rows),
        _write_jsonl(tmp_path / "candidate.jsonl", candidate_rows),
    )

    assert verdict.approved is False
    assert "unaccepted_artifact" in verdict.reasons


def test_candidate_p50_and_p95_are_independent_gates(tmp_path):
    reference = _write_jsonl(tmp_path / "reference.jsonl", _rows((100, 100, 100, 100, 300)))
    p50_slower = _write_jsonl(tmp_path / "p50.jsonl", _rows((101, 101, 101, 101, 200)))
    p95_slower = _write_jsonl(tmp_path / "p95.jsonl", _rows((1, 1, 1, 1, 400)))

    assert "candidate_p50_regression" in _compare(reference, p50_slower).reasons
    assert "candidate_p95_regression" in _compare(reference, p95_slower).reasons


def test_identity_mismatch_and_duplicate_identities_fail_closed(tmp_path):
    reference = _write_jsonl(tmp_path / "reference.jsonl", _rows())
    mismatch_rows = _rows()
    mismatch_rows[-1]["artifact_id"] = "different"
    mismatch = _write_jsonl(tmp_path / "mismatch.jsonl", mismatch_rows)
    duplicate_rows = _rows()
    duplicate_rows[-1]["artifact_id"] = duplicate_rows[0]["artifact_id"]
    duplicate = _write_jsonl(tmp_path / "duplicate.jsonl", duplicate_rows)

    assert "accepted_artifact_identity_mismatch" in _compare(reference, mismatch).reasons
    duplicate_verdict = _compare(reference, duplicate)
    assert duplicate_verdict.approved is False
    assert "duplicate_artifact_id" in duplicate_verdict.reasons


def test_missing_or_nonfinite_metrics_reject_instead_of_being_coerced(tmp_path):
    required = (
        "artifact_id",
        "accepted",
        "truth_finalized",
        "schema_valid",
        "latency_ms",
        "p0",
        "p1",
        "p2",
        "manual",
        "entitlement_success",
        "model_name",
        "run_id",
        "role",
        "artifact_set_sha256",
    )
    reference = _write_jsonl(tmp_path / "reference.jsonl", _rows())
    for field in required:
        rows = _bind_evidence(_rows(), "candidate")
        rows[0].pop(field)
        verdict = _compare(
            reference,
            _write_jsonl(
                tmp_path / f"missing-{field}.jsonl", rows, bind_identity=False
            ),
        )
        assert verdict.approved is False
        assert f"missing_metric:{field}" in verdict.reasons

    for index, value in enumerate((math.nan, math.inf, -math.inf)):
        rows = _rows()
        rows[0]["latency_ms"] = value
        verdict = _compare(reference, _write_jsonl(tmp_path / f"nonfinite-{index}.jsonl", rows))
        assert verdict.approved is False
        assert "invalid_metric:latency_ms" in verdict.reasons


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("latency_ms", 10**400),
        ("p0", 10**400),
        ("latency_ms", "1e100000"),
    ],
)
def test_unsafe_numeric_magnitudes_are_invalid_metrics_not_tracebacks(tmp_path, field, value):
    reference = _write_jsonl(tmp_path / "reference.jsonl", _rows())
    rows = _rows()
    rows[0][field] = value
    candidate = _write_jsonl(tmp_path / "candidate.jsonl", rows)

    verdict = _compare(reference, candidate)

    assert verdict.approved is False
    assert f"invalid_metric:{field}" in verdict.reasons


@pytest.mark.parametrize(
    "value",
    [Decimal("1e100000"), Decimal("NaN"), Decimal("Infinity")],
)
def test_decimal_numeric_validation_never_raises(value):
    from model_calibration import _is_finite_nonnegative_number

    assert _is_finite_nonnegative_number(value) is False


def test_malformed_jsonl_produces_deterministic_rejection(tmp_path):
    from model_calibration import main

    reference = _write_jsonl(tmp_path / "reference.jsonl", _rows())
    candidate = tmp_path / "candidate.jsonl"
    candidate.write_text('{"artifact_id": 1}\nnot-json\n', encoding="utf-8")

    output_a = tmp_path / "a.json"
    output_b = tmp_path / "b.json"
    exit_a = main(
        [str(reference), str(candidate), "--reference-model", "glm-4.5v", "--candidate-model", "candidate", "--output", str(output_a)]
    )
    exit_b = main(
        [str(reference), str(candidate), "--reference-model", "glm-4.5v", "--candidate-model", "candidate", "--output", str(output_b)]
    )

    assert exit_a == exit_b == 1
    assert output_a.read_bytes() == output_b.read_bytes()
    assert json.loads(output_a.read_text(encoding="utf-8"))["reasons"] == ["invalid_evidence"]


@pytest.mark.parametrize("content", ["", "not-json\n", "[]\n"])
def test_compare_calibration_returns_rejected_verdict_for_malformed_evidence(tmp_path, content):
    reference = _write_jsonl(tmp_path / "reference.jsonl", _rows())
    candidate = tmp_path / "candidate.jsonl"
    candidate.write_text(content, encoding="utf-8")

    verdict = _compare(reference, candidate)

    assert verdict.approved is False
    assert verdict.reasons == ("invalid_evidence",)


def test_cli_emits_deterministic_json_and_has_no_api_key_argument(tmp_path, capsys):
    from model_calibration import main

    reference = _write_jsonl(tmp_path / "reference.jsonl", _rows())
    candidate = _write_jsonl(tmp_path / "candidate.jsonl", _rows((90, 110, 130)))
    args = [
        str(reference),
        str(candidate),
        "--reference-model",
        "glm-4.5v",
        "--candidate-model",
        "glm-4.6v",
    ]

    assert main(args) == 0
    first = capsys.readouterr().out
    assert main(args) == 0
    second = capsys.readouterr().out
    assert first == second
    payload = json.loads(first)
    assert payload["approved"] is True
    assert payload["reference_model"] == "glm-4.5v"
    assert payload["candidate_model"] == "glm-4.6v"
    assert "api_key" not in payload

    with pytest.raises(SystemExit) as caught:
        main(args + ["--api-key", "must-not-be-supported"])
    assert caught.value.code == 2


def test_cli_model_arguments_must_match_evidence_models(tmp_path, capsys):
    from model_calibration import main

    reference = _write_jsonl(tmp_path / "reference.jsonl", _rows())
    candidate = _write_jsonl(tmp_path / "candidate.jsonl", _rows())

    exit_code = main(
        [
            str(reference),
            str(candidate),
            "--reference-model",
            "wrong-reference",
            "--candidate-model",
            "wrong-candidate",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert "model_argument_mismatch" in payload["reasons"]


def test_cli_huge_decimal_exponent_is_deterministic_json_rejection(tmp_path, capsys):
    from model_calibration import main

    reference = _write_jsonl(tmp_path / "reference.jsonl", _rows())
    candidate = _write_jsonl(tmp_path / "candidate.jsonl", _rows())
    raw = candidate.read_text(encoding="utf-8")
    candidate.write_text(raw.replace('"latency_ms": 100', '"latency_ms": 1e100000', 1), encoding="utf-8")

    args = [
        str(reference),
        str(candidate),
        "--reference-model",
        "glm-4.5v",
        "--candidate-model",
        "glm-4.6v",
    ]
    assert main(args) == 1
    first = capsys.readouterr().out
    assert main(args) == 1
    second = capsys.readouterr().out
    assert first == second
    payload = json.loads(first)
    assert payload["approved"] is False
    assert "invalid_metric:latency_ms" in payload["reasons"]


def test_very_long_integer_literal_is_invalid_metric_not_invalid_evidence(tmp_path):
    reference = _write_jsonl(tmp_path / "reference.jsonl", _rows())
    candidate = _write_jsonl(tmp_path / "candidate.jsonl", _rows())
    raw = candidate.read_text(encoding="utf-8")
    candidate.write_text(
        raw.replace('"latency_ms": 100', f'"latency_ms": {"9" * 5000}', 1),
        encoding="utf-8",
    )

    verdict = _compare(reference, candidate)

    assert verdict.approved is False
    assert "invalid_metric:latency_ms" in verdict.reasons
    assert "invalid_evidence" not in verdict.reasons
