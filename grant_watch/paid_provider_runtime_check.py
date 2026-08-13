"""Preflight paid-provider authority, credentials, limits, and bound ledgers.

This command is read-only. It loads the ordinary environment, validates the private
host capability, and deep-opens each configured standalone ledger without contacting
Firecrawl or ZoomInfo. It is the deploy-facing equivalent of the listener's startup
gate and deliberately never prints a credential value.
"""

from __future__ import annotations

from dotenv import load_dotenv

from .health import runtime_configuration_issues


def main() -> int:
    """Report every fail-closed runtime issue and return a shell-friendly status."""
    load_dotenv()
    issues = runtime_configuration_issues()
    if issues:
        for issue in issues:
            print(f"refused: {issue}")
        return 2
    print("verified: paid-provider runtime authority and ledgers are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
