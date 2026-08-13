"""Repository health checks that enforce the grants_agent documentation contract.

Why: Ruff and Vulture do not enforce the project-specific requirement that every
Python module/function is documented and fully annotated, nor do they detect stale
repository copies that can break pytest discovery. This reusable offline check keeps
those rules executable without becoming a one-time diagnostic script.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from collections.abc import Mapping

from .paid_provider_authority import configuration_issues as authority_issues


ROOT = Path(__file__).resolve().parent.parent
LINE_CAP = 1_000
SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
    }
)
# Runtime artifacts the deployed bot writes in place (keepalive/cron logs grow
# without bound on the droplet); they are environment-owned, not repository text.
RUNTIME_ARTIFACT_SUFFIXES = frozenset({".log"})
RUNTIME_ARTIFACT_NAMES = frozenset({"nohup.out"})


def _is_skipped(path: Path, root: Path) -> bool:
    """Return whether a path is inside a generated or environment-owned directory."""
    return any(part in SKIP_DIRECTORIES for part in path.relative_to(root).parts)


def documentation_issues(root: Path = ROOT) -> list[str]:
    """Find Python modules/functions missing docstrings or complete annotations."""
    issues: list[str] = []
    for package in (root / "grant_watch", root / "tests"):
        for path in sorted(package.rglob("*.py")):
            if _is_skipped(path, root):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            relative = path.relative_to(root)
            if ast.get_docstring(tree) is None:
                issues.append(f"{relative}: missing module docstring")
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if ast.get_docstring(node) is None:
                    issues.append(
                        f"{relative}:{node.lineno}:{node.name}: missing docstring"
                    )
                arguments = [
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                ]
                if node.args.vararg is not None:
                    arguments.append(node.args.vararg)
                if node.args.kwarg is not None:
                    arguments.append(node.args.kwarg)
                missing = [
                    argument.arg
                    for argument in arguments
                    if argument.arg not in {"self", "cls"}
                    and argument.annotation is None
                ]
                if missing:
                    issues.append(
                        f"{relative}:{node.lineno}:{node.name}: untyped args {missing}"
                    )
                if node.returns is None:
                    issues.append(
                        f"{relative}:{node.lineno}:{node.name}: missing return type"
                    )
    return issues


def oversized_text_issues(root: Path = ROOT) -> list[str]:
    """Find readable repository files that exceed the constitutional line cap."""
    issues: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or _is_skipped(path, root) or path.name == ".env":
            continue
        if (
            path.suffix in RUNTIME_ARTIFACT_SUFFIXES
            or path.name in RUNTIME_ARTIFACT_NAMES
        ):
            continue
        raw = path.read_bytes()
        if b"\x00" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        line_count = len(text.splitlines())
        if line_count > LINE_CAP:
            issues.append(
                f"{path.relative_to(root)}: {line_count} lines exceeds {LINE_CAP}"
            )
    return issues


def nested_test_tree_issues(root: Path = ROOT) -> list[str]:
    """Detect copied repository test trees that can corrupt pytest collection."""
    canonical = (root / "tests").resolve()
    issues: list[str] = []
    for path in sorted(root.rglob("tests")):
        if not path.is_dir() or _is_skipped(path, root):
            continue
        if path.resolve() != canonical and any(path.glob("test_*.py")):
            issues.append(f"{path.relative_to(root)}: unexpected nested test tree")
    return issues


def runtime_configuration_issues(
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Report enabled runtime features whose required identity gates are absent."""
    values = environ if environ is not None else os.environ
    rich_enabled = str(values.get("GRANT_RICH_CARD_ENABLED", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    issues: list[str] = []
    issues.extend(authority_issues(values))
    if rich_enabled and not str(values.get("SLACK_WORKSPACE_ID", "")).strip():
        issues.append(
            "GRANT_RICH_CARD_ENABLED is on but SLACK_WORKSPACE_ID is missing; "
            "draft actions would be unreachable"
        )
    try:
        zoominfo_limit = int(str(values.get("ZOOMINFO_MONTHLY_CREDITS", "0")).strip())
    except ValueError:
        zoominfo_limit = 0
    ledger_path = str(values.get("ZOOMINFO_CREDIT_LEDGER_PATH", "")).strip()
    if zoominfo_limit > 0 and not ledger_path:
        issues.append(
            "ZOOMINFO_MONTHLY_CREDITS is positive but ZOOMINFO_CREDIT_LEDGER_PATH "
            "is missing; paid pulls would have no account-wide authority"
        )
    elif ledger_path and not Path(ledger_path).expanduser().is_absolute():
        issues.append("ZOOMINFO_CREDIT_LEDGER_PATH must be absolute")
    firecrawl_key = str(values.get("FIRECRAWL_API_KEY", "")).strip()
    firecrawl_ledger = str(values.get("FIRECRAWL_RUNTIME_LEDGER_PATH", "")).strip()
    try:
        firecrawl_limit = int(
            str(values.get("FIRECRAWL_RUNTIME_MONTHLY_CALL_LIMIT", "0")).strip()
        )
    except ValueError:
        firecrawl_limit = 0
    if firecrawl_key and firecrawl_limit <= 0:
        issues.append(
            "FIRECRAWL_API_KEY is configured but the runtime monthly limit is not positive"
        )
    try:
        firecrawl_rate = int(
            str(values.get("FIRECRAWL_RUNTIME_REQUESTS_PER_MINUTE", "0")).strip()
        )
    except ValueError:
        firecrawl_rate = 0
    if firecrawl_key and not 1 <= firecrawl_rate <= 600:
        issues.append(
            "FIRECRAWL_API_KEY is configured but the runtime requests-per-minute "
            "limit is not between 1 and 600"
        )
    if firecrawl_key and not firecrawl_ledger:
        issues.append(
            "FIRECRAWL_API_KEY is configured but FIRECRAWL_RUNTIME_LEDGER_PATH is missing"
        )
    elif firecrawl_ledger and not Path(firecrawl_ledger).expanduser().is_absolute():
        issues.append("FIRECRAWL_RUNTIME_LEDGER_PATH must be absolute")
    if environ is None and not issues:
        # The Socket Mode startup path calls this after loading .env. Deep-open both
        # enabled ledgers so malformed files and authority/account mismatches stop the
        # listener, not merely the first Slack request.
        if firecrawl_key:
            try:
                from .enrich import firecrawl_gateway

                firecrawl_gateway.connect_ledger().close()
            except RuntimeError as exc:
                issues.append(f"Firecrawl runtime authority is invalid: {exc}")
        zoominfo_credentials = bool(
            str(values.get("ZOOMINFO_CLIENT_ID", "")).strip()
            or str(values.get("ZOOMINFO_CLIENT_SECRET", "")).strip()
        )
        if zoominfo_credentials:
            try:
                from .enrich import zoominfo_credits

                zoominfo_credits.connect_ledger().close()
            except RuntimeError as exc:
                issues.append(f"ZoomInfo runtime authority is invalid: {exc}")
    return issues


def health_issues(root: Path = ROOT) -> list[str]:
    """Return every repository health violation in deterministic order.

    Deployment configuration is intentionally checked by the listener after it
    loads ``.env``.  Keeping that check out of this offline repository gate makes
    the result independent of unrelated variables inherited by a developer shell.
    """
    return [
        *documentation_issues(root),
        *oversized_text_issues(root),
        *nested_test_tree_issues(root),
    ]


def main() -> int:
    """Run repository-specific health checks and return a shell-friendly status."""
    issues = health_issues()
    if issues:
        for issue in issues:
            print(f"needs-testing: {issue}")
        return 1
    print(
        "verified: documentation, annotations, file sizes, and test-tree layout are healthy"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
