# Packaged Frontend Regression Gate

## Canonical Scope

Manifest:

`test_dataset/qq_20251125_20260614_rebuilt_20260614_1035/truth_manifest.json`

Dates: `2025-11-25` through `2026-06-14`.

## Command

```powershell
python scripts/frontend_truth_acceptance.py `
  --exe <packaged-exe> `
  --truth-manifest test_dataset/qq_20251125_20260614_rebuilt_20260614_1035/truth_manifest.json `
  --date-from 2025-11-25 `
  --date-to 2026-06-14 `
  --max-seconds 1836
```

The harness launches the actual Windows frontend, waits for finalized evidence, runs `strict_truth_audit.py` through the structured audit API, and then runs `batch_validation.py` through `BatchValidator`.

## Pass Contract

- `P0 = 0`
- `P1 = 0`
- `P2 = 0`
- `manual = 0`
- authoritative evidence is true
- elapsed time is at most 1836 seconds
- encrypted API and IMAP credentials remain present and unchanged

Any nonzero count, missing authority, cleanup escape, credential change, process exit, timeout, or validation exception is a failed run. Preserve that run root for diagnosis and use a new clean run root for the next cycle.

An additional mailbox is a separate gate only when a finalized local truth manifest for that mailbox is available and explicitly in scope.
