from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / ".codex" / "skills" / "email-batch-test"
CANONICAL_MANIFEST = (
    "test_dataset/qq_20251125_20260614_rebuilt_20260614_1035/"
    "truth_manifest.json"
)


def test_project_batch_skill_uses_current_authoritative_full_range_truth():
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert CANONICAL_MANIFEST in text
    assert "included_count = 215" in text
    assert "pending_review_count = 0" in text
    assert "finalized = true" in text
    assert "CLAUDE.md" not in text
    assert "qq_20260201_20260311_final" not in text


def test_project_batch_skill_requires_zero_p0_p1_p2_and_manual_results():
    standards = (SKILL_ROOT / "references" / "test-standards.md").read_text(
        encoding="utf-8"
    )
    gate = (SKILL_ROOT / "references" / "regression-gate.md").read_text(
        encoding="utf-8"
    )

    for marker in ("P0 = 0", "P1 = 0", "P2 = 0", "manual = 0"):
        assert marker in standards
        assert marker in gate
    assert "允许少量" not in standards
    assert "≤ 15%" not in standards
    assert "frontend_truth_acceptance.py" in gate
    assert "batch_validation.py" in gate
    assert CANONICAL_MANIFEST in gate


def test_project_batch_skill_does_not_require_missing_stale_163_worktree():
    gate = (SKILL_ROOT / "references" / "regression-gate.md").read_text(
        encoding="utf-8"
    )

    assert ".claude/worktrees/test-163-batch" not in gate
    assert "163_20260301_20260319" not in gate
