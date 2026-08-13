"""Every model client is bounded; no Slack or cron path inherits SDK defaults."""

from __future__ import annotations

import ast
from pathlib import Path

from grant_watch.llm import anthropic_client_options


def test_shared_anthropic_policy_is_bounded() -> None:
    """Pin the request timeout and retry ceiling used throughout the application."""
    assert anthropic_client_options() == {"timeout": 60.0, "max_retries": 2}


def test_no_source_constructs_an_unbounded_anthropic_client() -> None:
    """A newly added bare ``Anthropic()`` call fails review automatically."""
    root = Path(__file__).resolve().parents[1] / "grant_watch"
    violations: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Anthropic"
                and not node.args
                and not node.keywords
            ):
                violations.append(f"{path.relative_to(root)}:{node.lineno}")
    assert violations == []
