from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.frontend_truth_acceptance as acceptance
from batch_validation import BatchValidationError
from scripts.frontend_truth_acceptance import (
    AcceptanceRuntimeError,
    AcceptanceSafetyError,
    audit_completed_run,
    build_run_context,
    evaluate_acceptance,
    launch_frontend,
    prepare_clean_run_root,
    terminate_frontend,
    wait_for_finalized_evidence,
)


def _state_key(output_path: Path) -> str:
    normalized = os.path.normcase(os.path.abspath(str(output_path)))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _settings_tree(tmp_path: Path, output_path: Path):
    app_dir = tmp_path / "AppData" / "Roaming" / "InvoiceFlowAI"
    settings_path = app_dir / "user_settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_bytes(b'{"api_key":"encrypted-value","auth_code":"encrypted"}')
    state_dir = app_dir / "state" / "output_scoped" / _state_key(output_path)
    state_dir.mkdir(parents=True)
    (state_dir / "run_state.json").write_text('{"status":"completed"}', encoding="utf-8")
    return settings_path, state_dir


def _write_manifest(
    path: Path,
    *,
    included_count: int = 215,
    pending_review_count: int = 0,
    finalized: bool = True,
    date_from: str = "2025-11-25",
    date_to: str = "2026-06-14",
) -> Path:
    path.write_text(
        json.dumps(
            {
                "summary": {
                    "included_count": included_count,
                    "pending_review_count": pending_review_count,
                    "finalized": finalized,
                    "date_from": date_from,
                    "date_to": date_to,
                }
            }
        ),
        encoding="utf-8",
    )
    acceptance.CANONICAL_MANIFEST_SHA256 = hashlib.sha256(path.read_bytes()).hexdigest()
    return path


def test_acceptance_cleanup_removes_only_target_run_and_output_state_preserving_settings(
    tmp_path: Path,
):
    manual_root = tmp_path / "manual_frontend_runs"
    run_root = manual_root / "frontend_acceptance_test"
    (run_root / "output").mkdir(parents=True)
    (run_root / "output" / "old.pdf").write_bytes(b"old")
    sibling = manual_root / "preserved_evidence" / "result.json"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("preserve", encoding="utf-8")
    settings_path, state_dir = _settings_tree(tmp_path, run_root / "output")
    settings_before = settings_path.read_bytes()

    evidence = prepare_clean_run_root(
        run_root,
        state_dir,
        settings_path=settings_path,
    )

    assert not (run_root / "output" / "old.pdf").exists()
    assert (run_root / "output").is_dir()
    assert (run_root / "staging").is_dir()
    assert (run_root / "diagnostics").is_dir()
    assert (run_root / "monitoring" / "qc").is_dir()
    assert not state_dir.exists()
    assert sibling.read_text(encoding="utf-8") == "preserve"
    assert settings_path.read_bytes() == settings_before
    digest = hashlib.sha256(settings_before).hexdigest()
    assert evidence.settings_sha256_before == digest
    assert evidence.settings_sha256_after == digest


@pytest.mark.parametrize("outside", ["ordinary-run", "manual_frontend_runs"])
def test_acceptance_cleanup_rejects_run_root_outside_or_equal_to_manual_root(
    tmp_path: Path, outside: str
):
    run_root = tmp_path / outside
    settings_path, state_dir = _settings_tree(tmp_path, run_root / "output")

    with pytest.raises(AcceptanceSafetyError):
        prepare_clean_run_root(run_root, state_dir, settings_path=settings_path)


def test_acceptance_cleanup_rejects_non_output_scoped_state_path(tmp_path: Path):
    run_root = tmp_path / "manual_frontend_runs" / "run"
    settings_path, _state_dir = _settings_tree(tmp_path, run_root / "output")
    unsafe_state = settings_path.parent / "state"

    with pytest.raises(AcceptanceSafetyError):
        prepare_clean_run_root(run_root, unsafe_state, settings_path=settings_path)


def test_acceptance_cleanup_rejects_state_for_another_output(tmp_path: Path):
    run_root = tmp_path / "manual_frontend_runs" / "run"
    settings_path, wrong_state = _settings_tree(tmp_path, tmp_path / "other" / "output")

    with pytest.raises(AcceptanceSafetyError, match="output_state_scope_mismatch"):
        prepare_clean_run_root(run_root, wrong_state, settings_path=settings_path)


def test_acceptance_cleanup_rejects_matching_state_outside_settings_root(tmp_path: Path):
    run_root = tmp_path / "manual_frontend_runs" / "run"
    settings_path, _state_dir = _settings_tree(tmp_path, run_root / "output")
    rogue_state = (
        tmp_path
        / "rogue"
        / "state"
        / "output_scoped"
        / _state_key(run_root / "output")
    )
    rogue_state.mkdir(parents=True)

    with pytest.raises(AcceptanceSafetyError, match="output_state_root_mismatch"):
        prepare_clean_run_root(run_root, rogue_state, settings_path=settings_path)


def test_acceptance_cleanup_rejects_reparse_component(tmp_path: Path, monkeypatch):
    run_root = tmp_path / "manual_frontend_runs" / "reparse" / "run"
    settings_path, state_dir = _settings_tree(tmp_path, run_root / "output")
    original = acceptance._is_reparse_point

    monkeypatch.setattr(
        acceptance,
        "_is_reparse_point",
        lambda path: Path(path).name == "reparse" or original(Path(path)),
    )

    with pytest.raises(AcceptanceSafetyError, match="run_root_reparse_point"):
        prepare_clean_run_root(run_root, state_dir, settings_path=settings_path)


def test_build_run_context_locks_scope_autostarts_and_contains_no_secrets(tmp_path: Path):
    run_root = tmp_path / "manual_frontend_runs" / "run"
    manifest_path = _write_manifest(tmp_path / "truth_manifest.json")

    context = build_run_context(
        run_root,
        manifest_path,
        date_from="2025-11-25",
        date_to="2026-06-14",
        run_id="frontend_acceptance_test",
    )

    assert context["controlled_run"] is True
    assert context["autostart_enabled"] is True
    assert context["validation_required"] is True
    assert context["manifest_included_count"] == 215
    assert context["locked_date_from"] == "2025-11-25"
    assert context["locked_date_to"] == "2026-06-14"
    assert Path(context["locked_output_path"]) == run_root.resolve() / "output"
    assert context["truth_manifest_path"] == str(manifest_path.resolve())
    assert "disable_auto_local_scan" not in context
    rendered = json.dumps(context, ensure_ascii=False).lower()
    assert "api_key" not in rendered
    assert "auth_code" not in rendered


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"finalized": False}, "truth_manifest_not_finalized"),
        ({"pending_review_count": 1}, "truth_manifest_pending_review"),
        ({"included_count": 214}, "truth_manifest_included_count_mismatch"),
        ({"date_from": "2025-11-26"}, "truth_manifest_date_range_mismatch"),
        ({"date_to": "2026-06-13"}, "truth_manifest_date_range_mismatch"),
    ],
)
def test_build_run_context_rejects_noncanonical_manifest_scope(
    tmp_path: Path, overrides: dict, error: str
):
    manifest_path = _write_manifest(tmp_path / "truth_manifest.json", **overrides)

    with pytest.raises(AcceptanceSafetyError, match=error):
        build_run_context(
            tmp_path / "manual_frontend_runs" / "run",
            manifest_path,
            date_from="2025-11-25",
            date_to="2026-06-14",
            run_id="run",
        )


def test_build_run_context_rejects_manifest_with_wrong_canonical_digest(tmp_path: Path):
    manifest_path = _write_manifest(tmp_path / "truth_manifest.json")
    acceptance.CANONICAL_MANIFEST_SHA256 = "0" * 64

    with pytest.raises(AcceptanceSafetyError, match="truth_manifest_digest_mismatch"):
        build_run_context(
            tmp_path / "manual_frontend_runs" / "run",
            manifest_path,
            date_from="2025-11-25",
            date_to="2026-06-14",
            run_id="run",
        )


def test_restore_settings_snapshot_recovers_exact_bytes(tmp_path: Path):
    settings_path = tmp_path / "user_settings.json"
    original = b'{"api_key":"encrypted","theme":"dark"}'
    settings_path.write_bytes(b'{"api_key":"encrypted","theme":"changed"}')

    acceptance._restore_settings_snapshot(settings_path, original)

    assert settings_path.read_bytes() == original


def test_launch_frontend_uses_visible_executable_and_explicit_context(tmp_path: Path):
    executable = tmp_path / "InvoiceFlowAI.exe"
    executable.write_bytes(b"exe")
    context_path = tmp_path / "run context.json"
    context_path.write_text("{}", encoding="utf-8")
    calls = []
    process = object()

    def popen_factory(command, **kwargs):
        calls.append((command, kwargs))
        return process

    assert launch_frontend(executable, context_path, popen_factory=popen_factory) is process
    command, kwargs = calls[0]
    assert command == [str(executable.resolve()), "--run-context", str(context_path.resolve())]
    assert kwargs["cwd"] == str(executable.resolve().parent)
    assert kwargs["shell"] is False
    assert "startupinfo" not in kwargs
    assert "creationflags" not in kwargs


class _Process:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.pid = 123
        self.terminated = False
        self.killed = False
        self.wait_timeouts = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout):
        self.wait_timeouts.append(timeout)
        raise TimeoutError


class _Clock:
    def __init__(self, step=0.06):
        self.value = 0.0
        self.step = step

    def __call__(self):
        current = self.value
        self.value += self.step
        return current


def test_wait_for_evidence_rejects_nonfinal_evidence_and_times_out(tmp_path: Path):
    root = tmp_path / "manual_frontend_runs" / "run"
    evidence_path = root / "diagnostics" / "run_evidence.json"
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text('{"evidence_digest":"not-final"}', encoding="utf-8")

    with pytest.raises(AcceptanceRuntimeError, match="frontend_acceptance_timeout"):
        wait_for_finalized_evidence(
            root,
            _Process(),
            timeout_seconds=0.1,
            poll_seconds=0.01,
            monotonic=_Clock(),
            sleeper=lambda _seconds: None,
        )


def test_wait_for_evidence_fails_when_frontend_exits(tmp_path: Path):
    root = tmp_path / "manual_frontend_runs" / "run"

    with pytest.raises(
        AcceptanceRuntimeError, match="frontend_exited_before_evidence:23"
    ):
        wait_for_finalized_evidence(
            root,
            _Process(returncode=23),
            timeout_seconds=1,
            monotonic=_Clock(step=0.01),
            sleeper=lambda _seconds: None,
        )


def test_wait_for_evidence_returns_only_validator_accepted_authority(tmp_path: Path):
    root = tmp_path / "manual_frontend_runs" / "run"
    evidence_path = root / "diagnostics" / "run_evidence.json"
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text('{"evidence_digest":"valid"}', encoding="utf-8")
    calls = []

    evidence = wait_for_finalized_evidence(
        root,
        _Process(),
        timeout_seconds=1,
        evidence_validator=lambda payload, run_root: calls.append(
            (payload, run_root)
        ),
    )

    assert evidence["evidence_digest"] == "valid"
    assert calls == [({"evidence_digest": "valid"}, root.resolve())]


def test_secret_scan_rejects_api_or_imap_credentials_in_logs(tmp_path: Path):
    log_path = tmp_path / "debug_trace.jsonl"
    log_path.write_text('{"message":"imap-secret-value"}', encoding="utf-8")

    with pytest.raises(AcceptanceSafetyError, match="secret_leak_detected"):
        acceptance.ensure_no_secret_leaks(
            [log_path], ["api-secret-value", "imap-secret-value"]
        )


def _strict_result(root: Path, *, authoritative: bool = True):
    return {
        "run_root": str(root),
        "audit_authority": {"authoritative": authoritative, "reasons": []},
        "p0_conclusion": {"count": 0, "passed": authoritative, "bad_rows": []},
        "user_p1_conclusion": {
            "count": 0,
            "category_rows": [],
            "field_mismatch_rows": [],
        },
        "p2_conclusion": {"count": 0, "passed": True, "bad_rows": []},
        "manual_check_rows": [],
        "matched_rows": [],
    }


class _Validator:
    def __init__(self, root: Path, *, fail: bool = False):
        self.root = root
        self.fail = fail

    def validate(self, manifest_path, run_root):
        assert Path(manifest_path).is_file()
        assert Path(run_root) == self.root
        if self.fail:
            raise BatchValidationError("strict_audit_not_authoritative")
        return SimpleNamespace(
            passed=True,
            candidate_revision="b" * 40,
            counts={"p0": 0, "p1": 0, "p2": 0, "manual": 0},
        )

    def write_report(self, validation, run_root):
        report = Path(run_root) / "diagnostics" / "batch_validation.json"
        report.write_text('{"passed":true}', encoding="utf-8")
        return report


def _completed_run(tmp_path: Path):
    root = tmp_path / "manual_frontend_runs" / "run"
    (root / "diagnostics").mkdir(parents=True)
    (root / "diagnostics" / "run_evidence.json").write_text(
        json.dumps(
            {
                "run_id": "run",
                "elapsed_seconds": "100",
                "candidate_revision": "a" * 40,
            }
        ),
        encoding="utf-8",
    )
    return root, _write_manifest(tmp_path / "truth_manifest.json")


def test_audit_calls_real_strict_interface_and_fails_false_authority(tmp_path: Path):
    root, manifest_path = _completed_run(tmp_path)
    strict_calls = []

    result = audit_completed_run(
        manifest_path,
        root,
        max_seconds=1836,
        strict_runner=lambda manifest, run_root: (
            strict_calls.append((manifest, run_root))
            or _strict_result(run_root, authoritative=False)
        ),
        validator=_Validator(root),
    )

    assert strict_calls[0][0]["summary"]["included_count"] == 215
    assert strict_calls[0][1] == root.resolve()
    assert result["passed"] is False
    assert result["authoritative"] is False


def test_audit_fails_when_batch_validation_fails(tmp_path: Path):
    root, manifest_path = _completed_run(tmp_path)

    result = audit_completed_run(
        manifest_path,
        root,
        max_seconds=1836,
        strict_runner=lambda _manifest, run_root: _strict_result(run_root),
        validator=_Validator(root, fail=True),
    )

    assert result["passed"] is False
    assert result["batch_validation_passed"] is False
    assert result["validation_error"] == "strict_audit_not_authoritative"


def test_terminate_frontend_is_bounded_when_graceful_stop_stalls():
    process = _Process()
    command_timeouts = []

    def command_runner(*_args, **kwargs):
        command_timeouts.append(kwargs["timeout"])
        raise TimeoutError

    terminate_frontend(
        process,
        timeout_seconds=0.2,
        command_runner=command_runner,
        monotonic=_Clock(step=0.02),
    )

    assert process.killed is True
    assert all(0 < timeout <= 0.2 for timeout in command_timeouts)
    assert all(0 < timeout <= 0.2 for timeout in process.wait_timeouts)


@pytest.mark.parametrize(
    ("counts", "elapsed", "authoritative", "validation", "credentials", "passed"),
    [
        ({"p0": 0, "p1": 0, "p2": 0, "manual": 0}, "1836", True, True, True, True),
        ({"p0": 0, "p1": 0, "p2": 0, "manual": 0}, "1836.01", True, True, True, False),
        ({"p0": 1, "p1": 0, "p2": 0, "manual": 0}, "100", True, False, True, False),
        ({"p0": 0, "p1": 0, "p2": 1, "manual": 0}, "100", True, False, True, False),
        ({"p0": 0, "p1": 0, "p2": 0, "manual": 0}, "100", False, False, True, False),
        ({"p0": 0, "p1": 0, "p2": 0, "manual": 0}, "100", True, True, False, False),
    ],
)
def test_acceptance_verdict_requires_authority_zero_counts_and_time_gate(
    counts, elapsed, authoritative, validation, credentials, passed
):
    result = evaluate_acceptance(
        counts=counts,
        elapsed_seconds=elapsed,
        max_seconds="1836",
        authoritative=authoritative,
        batch_validation_passed=validation,
        credentials_preserved=credentials,
    )

    assert result["passed"] is passed
    assert result["p0_count"] == counts["p0"]
    assert result["user_p1_count"] == counts["p1"]
    assert result["p2_count"] == counts["p2"]
    assert result["manual_check_count"] == counts["manual"]
    assert result["credentials_preserved"] is credentials
