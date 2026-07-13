---
name: email-batch-test
description: Use when Invoice-Downloader work involves mailbox batch tests, truth audit, regression validation, P0/P1/P2 decisions, packaged frontend acceptance, or clean reruns.
---

# Email Batch Test

## Current Authority

Read `AGENTS.md`, then read both files under `references/`.

The canonical QQ full-range truth manifest is:

`test_dataset/qq_20251125_20260614_rebuilt_20260614_1035/truth_manifest.json`

Its required contract is:

- `included_count = 215`
- `pending_review_count = 0`
- `finalized = true`
- date range `2025-11-25` through `2026-06-14`

Do not substitute an older manifest or describe a non-final truth set as authoritative.

## Required Gate

1. Build the Windows package from the exact candidate revision.
2. Create a fresh child of `manual_frontend_runs` and clear only its output-scoped state.
3. Preserve `%APPDATA%\InvoiceFlowAI\user_settings.json` and its encrypted credentials.
4. Launch the packaged EXE visibly through `scripts/frontend_truth_acceptance.py`.
5. Require finalized run evidence and a fresh `batch_validation.py` audit.
6. Accept only an explicit result with P0, P1, P2, and manual counts all zero.

No result may be reported as probable, suspected, or unconfirmed. Missing authority is a failed gate.

## Secondary Mailboxes

Run another mailbox only when its local manifest is present, finalized, has zero pending review, and the task explicitly requires that scope. Never depend on a stale or missing worktree path.
