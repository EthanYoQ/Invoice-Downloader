# Frontend Runtime Zero-P012 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the packaged, user-visible frontend batch complete the QQ 2025-11-25 through 2026-06-14 truth run with P0=0, P1=0, P2=0 in at most 30.6 minutes.

**Architecture:** Keep the modular monolith, but move Playwright URL recovery behind a killable subprocess boundary and schedule deferred URL groups concurrently with deterministic output ordering. Controlled QA and normal interactive launches must execute the same candidate and URL policy; QA context may add evidence paths and autostart only. Final pairing, evidence, and strict truth audit remain mandatory terminal stages.

**Tech Stack:** Python 3.12, pywebview, Playwright Chromium, concurrent.futures, subprocess, PyInstaller, pytest, project truth/audit tooling.

## Global Constraints

- Preserve all existing mailbox, extraction, classification, naming, archive, local-state, diagnostics, Excel, and frontend behavior except the confirmed defects.
- Preserve encrypted API key and IMAP authorization settings.
- Do not silently skip a strong provider candidate; a strong provider timeout is terminal and must fail the run.
- Low-confidence non-provider URL failures are retained with evidence and cannot block terminal cleanup.
- Controlled and interactive runs use identical candidate processing.
- Final acceptance uses the packaged frontend, a clean run root, and `test_dataset/qq_20251125_20260614_rebuilt_20260614_1035/truth_manifest.json`.
- Acceptance requires P0=0, P1=0, P2=0, manual-check count=0, finalized run evidence, and elapsed time <= 1836 seconds.
- Do not increase GLM concurrency until timing evidence shows model requests are the remaining bottleneck.

---

### Task 1: Killable URL Recovery Boundary

**Files:**
- Create: `url_recovery_worker.py`
- Create: `bounded_url_recovery.py`
- Modify: `main.py`
- Test: `tests/test_bounded_url_recovery.py`
- Test: `tests/test_main_worker_dispatch.py`

**Interfaces:**
- Produces: `BoundedUrlRecoveryClient.process_invoice_links(text_content, subject, email_id, return_metadata=False, candidate_info=None)` with the existing `PDFConverter`-compatible return contract.
- Produces: `run_url_recovery_job(job_path: str) -> int` for the hidden packaged subprocess mode.
- Produces: `--url-recovery-worker <job.json>` dispatch in `main.py` that exits before creating pywebview.

- [ ] **Step 1: Write failing subprocess timeout and dispatch tests**

```python
def test_bounded_client_kills_worker_tree_after_deadline(tmp_path):
    runner = HangingRunner()
    client = BoundedUrlRecoveryClient(
        staging_dir=tmp_path,
        process_runner=runner,
        provider_timeout_seconds=0.01,
        generic_timeout_seconds=0.01,
    )
    result = client.process_invoice_links(
        "https://provider.example/invoice",
        "invoice",
        "mail-1",
        return_metadata=True,
        candidate_info={"provider_family": "nuonuo_scan_invoice"},
    )
    assert runner.terminated_tree is True
    assert result[0]["reason_code"] == "URL_RECOVERY_DEADLINE_EXCEEDED"

def test_worker_dispatch_does_not_create_ui(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(main, "run_url_recovery_job", lambda path: called.append(path) or 0)
    assert main.main(["--url-recovery-worker", str(tmp_path / "job.json")]) == 0
    assert called == [str(tmp_path / "job.json")]
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_bounded_url_recovery.py tests/test_main_worker_dispatch.py -q`

Expected: FAIL because the client, worker entry, and CLI option do not exist.

- [ ] **Step 3: Implement atomic job files and process-tree termination**

```python
class BoundedUrlRecoveryClient:
    def process_invoice_links(self, text_content, subject, email_id, return_metadata=False, candidate_info=None):
        timeout = self.provider_timeout_seconds if (candidate_info or {}).get("provider_family") else self.generic_timeout_seconds
        with tempfile.TemporaryDirectory(prefix="url-recovery-", dir=self.staging_dir) as job_dir:
            request_path, result_path = self._write_job(job_dir, text_content, subject, email_id, candidate_info)
            completed = self.process_runner.run(self._worker_command(request_path), timeout=timeout)
            if completed.timed_out:
                return [self._failure("URL_RECOVERY_DEADLINE_EXCEEDED", timeout)] if return_metadata else []
            return self._read_result(result_path, return_metadata=return_metadata)
```

The default runner must call `taskkill /PID <pid> /T /F` without `shell=True` after timeout on Windows, wait for exit, and delete request/result files in `finally`.

- [ ] **Step 4: Implement hidden worker dispatch**

```python
def run_url_recovery_job(job_path: str) -> int:
    payload = json.loads(Path(job_path).read_text(encoding="utf-8"))
    converter = PDFConverter(staging_dir=payload["staging_dir"], timeout_ms=payload["timeout_ms"])
    result = converter.process_invoice_links(
        payload["text_content"], payload["subject"], payload["email_id"],
        return_metadata=True, candidate_info=payload.get("candidate_info") or {},
    )
    atomic_write_json(Path(payload["result_path"]), {"result": sanitize_persistence_payload(result)})
    return 0
```

- [ ] **Step 5: Run focused tests and commit**

Run: `python -m pytest tests/test_bounded_url_recovery.py tests/test_main_worker_dispatch.py tests/test_provider_url_recovery.py -q`

Expected: PASS with no surviving fake worker.

Commit: `git commit -m "fix: bound browser recovery in killable workers"`

---

### Task 2: Product/Test Parity and Concurrent Deferred Recovery

**Files:**
- Create: `deferred_url_recovery.py`
- Modify: `candidate_pipeline.py`
- Modify: `extraction_pipeline.py`
- Modify: `app_api.py`
- Test: `tests/test_deferred_url_recovery.py`
- Test: `tests/test_processing_pipeline.py`

**Interfaces:**
- Produces: `DeferredUrlRecoveryScheduler(max_workers, stop_requested, progress_callback).recover(candidates, recover_one) -> list[ExtractionOutcome]`.
- Consumes: `BoundedUrlRecoveryClient` as the default URL converter factory.
- Preserves: sequence-ordered outcomes and provider-group serialization.

- [ ] **Step 1: Write failing parity, ordering, concurrency, and timeout-status tests**

```python
def test_controlled_and_interactive_candidates_both_invoke_bounded_recovery():
    controlled = run_preflight(controlled=True)
    interactive = run_preflight(controlled=False)
    assert controlled.reason_code == interactive.reason_code == "URL_RECOVERY_DEADLINE_EXCEEDED"

def test_scheduler_runs_provider_groups_concurrently_and_preserves_sequence():
    scheduler = DeferredUrlRecoveryScheduler(max_workers=4)
    outcomes = scheduler.recover(candidates, recover_one)
    assert [item.candidate.sequence for item in outcomes] == sorted(item.candidate.sequence for item in outcomes)
    assert observed_max_concurrency == 4

def test_generic_deadline_is_retained_but_provider_deadline_is_unresolved():
    assert generic.status == "retained"
    assert provider.status == "unresolved"
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_deferred_url_recovery.py tests/test_processing_pipeline.py -q`

Expected: FAIL because deferred URL recovery is serial and controlled runs skip non-provider URLs.

- [ ] **Step 3: Implement deterministic group scheduler**

Group by `provider_group_key`; use the document id as a one-item group when no provider group exists. Sort strong provider groups before generic groups, execute at most four groups concurrently, process candidates inside a group sequentially, collect futures, and return outcomes sorted by candidate sequence.

- [ ] **Step 4: Make CandidatePreflight shared state thread-safe**

Use one `threading.RLock` for current-run identity/history admission and provider-group success registration. Do not reserve a provider group before successful recovery; group serialization prevents duplicate in-flight recovery without suppressing fallback candidates.

- [ ] **Step 5: Remove controlled-only URL behavior**

Delete the `_should_gate_controlled_run_url` decision from `CandidatePreflight._recover_url`. QA context may alter evidence destinations and autostart, but not candidate execution.

- [ ] **Step 6: Integrate scheduler and bounded client**

Construct `BoundedUrlRecoveryClient` in `_create_processing_pipeline_session`, call the scheduler for initial URL outcomes and strong-provider retries, and treat `URL_DOWNLOAD_FAILED`, `URL_PAGE_TIMEOUT`, and `URL_RECOVERY_DEADLINE_EXCEEDED` as retryable strong-provider failures.

- [ ] **Step 7: Run focused tests and commit**

Run: `python -m pytest tests/test_deferred_url_recovery.py tests/test_processing_pipeline.py tests/test_url_persistence.py -q`

Expected: PASS; synthetic four-group recovery reaches concurrency four and preserves order.

Commit: `git commit -m "perf: parallelize deferred URL recovery safely"`

---

### Task 3: Terminal Pairing and Failure Semantics

**Files:**
- Modify: `app_api.py`
- Modify: `archive_pairing_service.py`
- Test: `tests/test_processing_pipeline.py`
- Test: `tests/test_pairing_engine.py`
- Test: `tests/test_invoice_regression_p2.py`

**Interfaces:**
- Consumes: bounded URL outcomes from Task 2.
- Preserves: `ArchiveService.finalize(report, save_path)` as the sole final pairing boundary.
- Produces: terminal output where required hotel and ride pairs use adjacent pair names before run evidence capture.

- [ ] **Step 1: Write failing end-to-end archive-session test**

```python
def test_generic_browser_deadline_cannot_block_provider_archive_or_pair_finalizer(tmp_path):
    report = session.archive(primary_outcomes)
    assert report.can_complete is True
    assert sorted(path.name for path in hotel_dir.iterdir()) == [
        "20260610-住宿-01-发票_424.15元.pdf",
        "20260610-住宿-01-水单_424.15元.pdf",
    ]
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m pytest tests/test_processing_pipeline.py -k "deadline_cannot_block" -q`

Expected: FAIL because current serial recovery never reaches finalization when browser close hangs.

- [ ] **Step 3: Enforce terminal outcome rules**

Convert failed low-confidence non-provider URL outcomes into retained records with their reason code. Keep strong-provider failures unresolved so `ArchiveReport.can_complete` is false and the frontend cannot claim success while a known invoice is missing.

- [ ] **Step 4: Verify pairing remains the last filesystem mutation**

Run `ArchiveService.finalize` exactly once after all provider reports are merged; update trace targets and lineage after renames, then commit output state. No evidence capture may precede pair renames.

- [ ] **Step 5: Run pairing and strict-audit tests and commit**

Run: `python -m pytest tests/test_processing_pipeline.py tests/test_pairing_engine.py tests/test_invoice_regression_p2.py tests/test_strict_truth_audit.py -q`

Expected: PASS including required hotel and ride pair checks.

Commit: `git commit -m "fix: guarantee terminal pairing after URL recovery"`

---

### Task 4: Truth Skill and Frontend Acceptance Harness

**Files:**
- Modify: `.codex/skills/email-batch-test/SKILL.md`
- Modify: `.codex/skills/email-batch-test/references/test-standards.md`
- Modify: `.codex/skills/email-batch-test/references/regression-gate.md`
- Create: `scripts/frontend_truth_acceptance.py`
- Test: `tests/test_project_skill_contracts.py`
- Test: `tests/test_frontend_truth_acceptance.py`

**Interfaces:**
- Produces: `scripts/frontend_truth_acceptance.py --exe <path> --truth-manifest <path> --date-from <date> --date-to <date> --max-seconds 1836`.
- Produces: a fresh run context, isolated output, monitoring, diagnostics, finalized evidence, strict audit, and elapsed-time report.

- [ ] **Step 1: Write failing skill-path and cleanup-scope tests**

```python
def test_project_batch_skill_points_to_latest_full_range_truth():
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "qq_20251125_20260614_rebuilt_20260614_1035/truth_manifest.json" in text

def test_acceptance_cleanup_preserves_encrypted_user_settings(tmp_path):
    before = settings_path.read_bytes()
    prepare_clean_run_root(run_root, output_state_dir)
    assert settings_path.read_bytes() == before
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_project_skill_contracts.py tests/test_frontend_truth_acceptance.py -q`

Expected: FAIL because the skill points to stale datasets and no acceptance harness exists.

- [ ] **Step 3: Update P0/P1/P2 definitions and canonical path**

Set P0 to any included truth miss, P1 to category or semantic field mismatch, and P2 to required hotel/ride pairing or adjacent-name failure. Set the full-range canonical manifest to the valid 10:35 rebuild.

- [ ] **Step 4: Implement guarded clean-run preparation**

Resolve every deletion target, require the run root to be a child of `manual_frontend_runs`, delete only that run root and its output-scoped state, and hash `user_settings.json` before/after preparation to prove credentials were preserved.

- [ ] **Step 5: Implement visible packaged-frontend launch and gate monitoring**

Generate explicit controlled run context with product-parity processing, launch the packaged EXE visibly, monitor terminal run-state/evidence files, run `strict_truth_audit.py`, and write `acceptance_result.json` containing elapsed seconds and P0/P1/P2/manual counts.

- [ ] **Step 6: Run harness tests and commit**

Run: `python -m pytest tests/test_project_skill_contracts.py tests/test_frontend_truth_acceptance.py -q`

Expected: PASS without deleting settings or any path outside the new run root.

Commit: `git commit -m "test: codify packaged frontend truth acceptance"`

---

### Task 5: Full Verification, Package, and Clean Frontend Batch

**Files:**
- Verify: all tracked Python and packaging files
- Generate: `dist/InvoiceFlowAI-portable-unsigned.zip`
- Generate: `manual_frontend_runs/<new-run-id>/acceptance_result.json`

**Interfaces:**
- Consumes: all implementations and the canonical 215-row truth manifest.
- Produces: the final evidence used to complete the Goal.

- [ ] **Step 1: Run full source verification**

Run: `python -m pytest -q`

Expected: `699+ passed`, zero failures.

Run: `python -m pip check`

Expected: `No broken requirements found.`

Run: `python -m compileall -q .`

Expected: exit code 0.

- [ ] **Step 2: Build portable package**

Run: `powershell -File build/windows/build_release.ps1 -Version 2026.07.12.3 -PythonExe .venv/Scripts/python.exe -BuildPythonExe .venv/Scripts/python.exe -RunPyInstaller -RunPortableZip`

Expected: exit code 0, full 40-character source revision, worker modules collected, ZIP hash produced.

- [ ] **Step 3: Clean the acceptance environment**

Run the acceptance harness preparation for a new timestamped run root. Confirm output count zero, no prior run state, and unchanged encrypted settings hash before launching.

- [ ] **Step 4: Launch visible packaged frontend and monitor to terminal**

Run: `python scripts/frontend_truth_acceptance.py --exe <rebuilt-exe> --truth-manifest test_dataset/qq_20251125_20260614_rebuilt_20260614_1035/truth_manifest.json --date-from 2025-11-25 --date-to 2026-06-14 --max-seconds 1836`

Expected: visible pywebview frontend, terminal completed state, no browser worker beyond its deadline, and elapsed <= 1836 seconds.

- [ ] **Step 5: Enforce the strict gate**

Expected `acceptance_result.json` values:

```json
{
  "p0_count": 0,
  "user_p1_count": 0,
  "p2_count": 0,
  "manual_check_count": 0,
  "authoritative": true,
  "elapsed_seconds_max": 1836
}
```

If any count is nonzero or time exceeds 1836 seconds, preserve the failed run root, diagnose from its evidence, implement one root-cause fix under TDD, clean a new run root, and rerun. Stop after success or five complete frontend cycles.

- [ ] **Step 6: Request review and integrate**

Run the Superpowers requesting-code-review gate, address confirmed findings, rerun affected tests, merge the worktree branch into `main`, rebuild from merged `main`, and repeat the package identity smoke check.

Commit: `git commit -m "release: validate zero-P012 frontend batch"`

---

## Self-Review

- Spec coverage: browser hang, product/test parity, P0/P1/P2, clean environment, packaged frontend validation, and 30% compression each map to a task.
- Placeholder scan: no deferred implementation markers are present.
- Type consistency: the bounded client keeps the `PDFConverter.process_invoice_links` contract; the scheduler returns ordered `ExtractionOutcome` values; the acceptance harness consumes the existing run-context and strict-audit contracts.
- Safety: settings are hash-verified unchanged; deletion is confined to a newly generated run root; strong provider failures cannot be downgraded into success.
