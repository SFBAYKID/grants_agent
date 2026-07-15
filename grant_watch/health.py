"""Repository health checks that enforce the grants_agent documentation contract.

Why: Ruff and Vulture do not enforce the project-specific requirement that every
Python module/function is documented and fully annotated, nor do they detect stale
repository copies that can break pytest discovery. This reusable offline check keeps
those rules executable without becoming a one-time diagnostic script.
"""

from __future__ import annotations

import ast
from pathlib import Path


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


def health_issues(root: Path = ROOT) -> list[str]:
    """Return every project-specific health violation in deterministic order."""
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
