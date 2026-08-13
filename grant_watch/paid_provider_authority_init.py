"""Preview or create the host-local paid-provider authority capability.

The default is write-free.  Execution creates one owner-only JSON file with a random
authority id and operator-supplied opaque vendor-account scope ids; it never writes
credentials and never replaces an existing capability.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .paid_provider_authority import (
    PaidProviderAuthorityError,
    initialize_authority_file,
)


def _scope(value: str) -> tuple[str, str]:
    """Parse ``provider=opaque-account-scope`` from one repeatable argument."""
    provider, separator, scope = value.partition("=")
    if not separator or not provider.strip() or not scope.strip():
        raise argparse.ArgumentTypeError("scope must be provider=opaque-account-scope")
    return provider.strip().lower(), scope.strip()


def run(
    destination: Path,
    scopes: tuple[tuple[str, str], ...],
    *,
    execute: bool,
) -> int:
    """Validate a unique provider mapping and optionally create the capability."""
    provider_scopes: dict[str, str] = {}
    for provider, scope in scopes:
        if provider in provider_scopes:
            print(f"refused: duplicate provider scope {provider}", file=sys.stderr)
            return 2
        provider_scopes[provider] = scope
    if not execute:
        print(
            "preview: would create a private paid-provider authority for "
            + ", ".join(sorted(provider_scopes))
            + "; no file changed"
        )
        return 0
    try:
        authority_id = initialize_authority_file(destination, provider_scopes)
    except (PaidProviderAuthorityError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(
        f"executed: created private paid-provider authority {authority_id} for "
        + ", ".join(sorted(provider_scopes))
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse the explicit destination/scopes and preserve preview as the default."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--scope", action="append", type=_scope, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    return run(args.destination, tuple(args.scope), execute=args.execute)


if __name__ == "__main__":
    raise SystemExit(main())
