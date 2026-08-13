"""Tests for the reusable repository-specific health gate."""

from __future__ import annotations

from pathlib import Path

from grant_watch import health


def _package(root: Path, source: str) -> Path:
    """Create the minimal package/test layout consumed by the health checker."""
    package = root / "grant_watch"
    package.mkdir()
    (root / "tests").mkdir()
    target = package / "sample.py"
    target.write_text(source)
    return target


def test_documentation_audit_accepts_fully_documented_code(tmp_path: Path) -> None:
    """A documented and annotated module passes the project-specific audit."""
    _package(
        tmp_path,
        '"""Module purpose."""\n\ndef value(number: int) -> int:\n'
        '    """Return the supplied number."""\n    return number\n',
    )
    assert health.documentation_issues(tmp_path) == []


def test_documentation_audit_reports_each_missing_contract(tmp_path: Path) -> None:
    """Missing module/function docs and annotations are reported independently."""
    _package(tmp_path, "def value(number):\n    return number\n")
    issues = health.documentation_issues(tmp_path)
    assert len(issues) == 4
    assert any("missing module docstring" in issue for issue in issues)
    assert any("missing docstring" in issue for issue in issues)
    assert any("untyped args" in issue for issue in issues)
    assert any("missing return type" in issue for issue in issues)


def test_size_and_nested_tree_audits_detect_repository_debris(tmp_path: Path) -> None:
    """Oversized text and copied test trees fail before normal pytest collection."""
    _package(tmp_path, '"""Healthy module."""\n')
    (tmp_path / "large.md").write_text("line\n" * (health.LINE_CAP + 1))
    (tmp_path / "cron.log").write_text("tick\n" * (health.LINE_CAP + 1))
    copied_tests = tmp_path / "review-copy" / "tests"
    copied_tests.mkdir(parents=True)
    (copied_tests / "test_copy.py").write_text('"""Copied test."""\n')
    assert health.oversized_text_issues(tmp_path) == [
        f"large.md: {health.LINE_CAP + 1} lines exceeds {health.LINE_CAP}",
    ]
    assert health.nested_test_tree_issues(tmp_path) == [
        "review-copy/tests: unexpected nested test tree",
    ]


def test_current_repository_passes_project_specific_health_gate() -> None:
    """The checked-in working tree satisfies documentation and debris rules."""
    assert health.health_issues() == []


def test_enabled_rich_actions_require_an_exact_workspace_identity() -> None:
    """Health fails before an enabled card can publish an unusable draft button."""
    assert health.runtime_configuration_issues(
        {"GRANT_RICH_CARD_ENABLED": "1", "SLACK_WORKSPACE_ID": ""}
    )
    assert (
        health.runtime_configuration_issues(
            {"GRANT_RICH_CARD_ENABLED": "1", "SLACK_WORKSPACE_ID": "T123"}
        )
        == []
    )


def test_positive_zoominfo_budget_requires_an_absolute_shared_ledger() -> None:
    """Health exposes an unsafe paid cutover before runtime reaches a pull."""
    assert health.runtime_configuration_issues(
        {"ZOOMINFO_MONTHLY_CREDITS": "1000", "ZOOMINFO_CREDIT_LEDGER_PATH": ""}
    )
    assert health.runtime_configuration_issues(
        {
            "ZOOMINFO_MONTHLY_CREDITS": "1000",
            "ZOOMINFO_CREDIT_LEDGER_PATH": "relative.db",
        }
    )
    assert (
        health.runtime_configuration_issues(
            {
                "ZOOMINFO_MONTHLY_CREDITS": "1000",
                "ZOOMINFO_CREDIT_LEDGER_PATH": "/private/ledger.db",
            }
        )
        == []
    )
