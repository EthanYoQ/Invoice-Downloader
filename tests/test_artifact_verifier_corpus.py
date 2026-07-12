from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from artifact_verifier import verify_final_artifact
from strict_truth_audit import compare


DEFAULT_CORPUS_ROOT = Path(r"C:\Vibe Coding Project\Invoice-Downloader")
MANIFEST_RELATIVE = Path(
    "test_dataset/qq_20251125_20260614_rebuilt_20260614_1035/truth_manifest.json"
)
RUN_RELATIVE = Path(
    "manual_program_runs/refactor_range2_20251125_20260614_20260624_231421"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_all_25_accepted_transformed_artifacts_pass_independent_visual_verification():
    corpus_root = Path(
        os.environ.get("INVOICEFLOW_ACCEPTED_CORPUS_ROOT", DEFAULT_CORPUS_ROOT)
    )
    manifest_path = corpus_root / MANIFEST_RELATIVE
    run_root = corpus_root / RUN_RELATIVE
    if not manifest_path.is_file() or not run_root.is_dir():
        pytest.skip("local read-only accepted transformed corpus is unavailable")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    truth_by_id = {row["truth_id"]: row for row in manifest["included"]}
    audit = compare(manifest, run_root)
    assert audit["p0_conclusion"]["count"] == 0
    assert audit["user_p1_conclusion"]["count"] == 0
    assert audit["p2_conclusion"]["count"] == 0

    transformed = []
    for assignment in audit["matched_rows"]:
        truth = truth_by_id[assignment["truth_id"]]
        output = Path(assignment["matched_path"])
        output_sha256 = _sha256(output)
        if output_sha256 == truth["sha256"]:
            continue
        verdict = verify_final_artifact(
            truth,
            output,
            output_sha256=output_sha256,
            source_chain_sha256s=[truth["sha256"]],
        )
        transformed.append((truth["truth_id"], verdict))

    assert len(transformed) == 25
    failures = [
        (truth_id, verdict.reason_code)
        for truth_id, verdict in transformed
        if not verdict.passed
    ]
    assert failures == []
