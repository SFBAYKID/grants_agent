"""Refuse a Salesforce write when the account could destroy the org.

WHY THIS EXISTS. Measured 2026-08-22: the integration authenticated as
``chase@monarchconnected.com.monarchdev`` -- profile System Administrator, holding
``ModifyAllData`` and delete rights on 11 of the 12 objects Grant touches. Until
now the create-only guarantee in ``salesforce_campaign_gateway`` rested entirely on
the ABSENCE of delete code: true today, and one careless commit from gone, with no
backstop underneath it.

This module is that backstop, and it is a different KIND of control from the rest of
the gateway. Every other guard here is code asking code to behave. This one asks
SALESFORCE what the authenticated user may actually do, and refuses the write when
the answer is "anything it likes". A permission the account does not hold cannot be
exercised by a bug we write later -- which is the only guarantee that survives our
own mistakes rather than depending on us not making any.

DELIBERATELY NOT A STARTUP GATE. A network check that can block boot trades one
outage for another; CLAUDE.md records a fail-closed configuration gate nearly taking
Grant offline on 2026-08-13. This runs at the WRITE chokepoint instead, so an
unreachable Salesforce blocks writes that would have failed anyway and leaves the
bot answering questions.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any  # Salesforce describe/query responses are runtime-shaped JSON.

# Every object Grant reads or creates. `Task` is listed deliberately even though
# Grant must never create one: an account that can DELETE Tasks can erase a
# colleague's call history, which is not Grant's data to lose.
GRANT_OBJECTS: tuple[str, ...] = (
    "Account",
    "Contact",
    "Lead",
    "Opportunity",
    "Campaign",
    "CampaignMember",
    "CampaignMemberStatus",
    "ContentNote",
    "ContentDocumentLink",
    "Note",
    "Task",
)

# System permissions that turn a scoped bug into an org-wide one. The second is the
# one that removes the last line of defence: hard delete bypasses the Recycle Bin,
# so the 15-day fallback never sees the records at all.
FORBIDDEN_SYSTEM_PERMISSIONS: tuple[tuple[str, str], ...] = (
    (
        "PermissionsModifyAllData",
        "edit and delete EVERY record in the org, ignoring all sharing rules",
    ),
    (
        "PermissionsBulkApiHardDelete",
        "delete records BYPASSING the Recycle Bin, so nothing is recoverable",
    ),
)

# One audit costs a describe per object. Re-running that before every write would
# multiply a 6-record campaign into ~70 HTTP calls, so it is cached per instance.
# Short enough that revoking a permission takes effect within the hour.
AUDIT_TTL_SECONDS = 900.0

# `path`, `params` -> decoded JSON, matching SalesforceCampaignGateway._get.
JsonGetter = Callable[[str, dict[str, str] | None], dict[str, Any]]


@dataclass(frozen=True)
class ObjectPrivileges:
    """What the authenticated user may do to one sObject, per Salesforce itself."""

    sobject: str
    createable: bool
    updateable: bool
    deletable: bool
    accessible: bool = True


@dataclass(frozen=True)
class PrivilegeAudit:
    """The authenticated user's destructive reach, as measured not assumed."""

    instance_url: str
    username: str
    profile_name: str
    objects: tuple[ObjectPrivileges, ...]
    system_permissions: dict[str, bool] = field(default_factory=dict)

    @property
    def deletable_objects(self) -> tuple[str, ...]:
        """Every object this account can destroy records in."""
        return tuple(item.sobject for item in self.objects if item.deletable)

    def violations(self) -> tuple[str, ...]:
        """Return one plain sentence per reason this account must not write.

        Empty means safe. The sentences are written for a human reading a refusal in
        Slack or a deploy log, so they name the account and the specific power rather
        than reporting a boolean.
        """
        who = self.username or "the integration user"
        reasons: list[str] = []
        deletable = self.deletable_objects
        if deletable:
            reasons.append(
                f"{who} can DELETE {len(deletable)} of the {len(self.objects)} "
                f"objects Grant touches ({', '.join(deletable)})"
            )
        for flag, consequence in FORBIDDEN_SYSTEM_PERMISSIONS:
            if self.system_permissions.get(flag):
                reasons.append(f"{who} holds {flag}, which can {consequence}")
        return tuple(reasons)

    @property
    def is_safe(self) -> bool:
        """True when this account structurally cannot destroy anything."""
        return not self.violations()


def _describe(get_json: JsonGetter, sobject: str) -> ObjectPrivileges:
    """Ask Salesforce what the current user may do to one object.

    `describe` reports the effective permissions of the CALLER, so this measures the
    live account rather than reading a profile we hope is still assigned. It writes
    nothing and touches no records.
    """
    try:
        body = get_json(f"sobjects/{sobject}/describe", None)
    except Exception:  # noqa: BLE001 - 403/404 both mean "this user cannot see it".
        # A least-privilege account cannot even DESCRIBE an object it has no rights
        # to, and Salesforce answers 404 rather than an empty permission set. That is
        # the SAFEST possible state -- nothing can be deleted through a door that is
        # not there -- so it must not crash the audit, and must never be mistaken for
        # a permission this account actually holds.
        return ObjectPrivileges(sobject, False, False, False, accessible=False)
    return ObjectPrivileges(
        sobject=sobject,
        createable=bool(body.get("createable")),
        updateable=bool(body.get("updateable")),
        deletable=bool(body.get("deletable")),
    )


def _system_permissions(get_json: JsonGetter, profile_name: str) -> dict[str, bool]:
    """Read the forbidden system flags for a profile, tolerating a blocked query.

    A least-privilege account may not be able to read Profile at all. That is not a
    failure: an account that cannot query Profile is not the over-privileged account
    this guard exists to stop, and the per-object describe already answers the
    question that matters. Returning empty means "not proven set", never "proven safe"
    -- `violations` only ever fires on a flag it has positively seen as True.
    """
    if not profile_name:
        return {}
    columns = ",".join(flag for flag, _ in FORBIDDEN_SYSTEM_PERMISSIONS)
    escaped = profile_name.replace("\\", "\\\\").replace("'", "\\'")
    try:
        body = get_json(
            "query", {"q": f"SELECT {columns} FROM Profile WHERE Name='{escaped}'"}
        )
    except Exception:  # noqa: BLE001 - any transport/permission failure is "unknown".
        return {}
    records = body.get("records") or []
    if not records:
        return {}
    return {
        flag: bool(records[0].get(flag)) for flag, _ in FORBIDDEN_SYSTEM_PERMISSIONS
    }


def audit_privileges(
    get_json: JsonGetter,
    instance_url: str,
    username: str = "",
    profile_name: str = "",
) -> PrivilegeAudit:
    """Measure what the authenticated account can destroy. Reads only."""
    objects = tuple(_describe(get_json, sobject) for sobject in GRANT_OBJECTS)
    return PrivilegeAudit(
        instance_url=instance_url,
        username=username,
        profile_name=profile_name,
        objects=objects,
        system_permissions=_system_permissions(get_json, profile_name),
    )


def assert_write_safe(report: PrivilegeAudit) -> None:
    """Raise unless the account structurally cannot destroy anything.

    The message names every reason at once. A guard that reports one problem per
    attempt turns a five-minute permission fix into five deploys.
    """
    reasons = report.violations()
    if not reasons:
        return
    raise PermissionError(
        "Refusing to write to Salesforce with a destructive account. "
        + "; ".join(reasons)
        + ". Grant is create-only by design, so this account is far more powerful "
        "than it needs to be: remove Delete on those objects (and any "
        "Modify All Data) from the integration user's permission set."
    )


_CACHE: dict[str, tuple[float, PrivilegeAudit]] = {}


def cached_audit(
    get_json: JsonGetter,
    instance_url: str,
    username: str = "",
    profile_name: str = "",
    now: Callable[[], float] = time.monotonic,
) -> PrivilegeAudit:
    """Audit once per instance per TTL; `now` is injected so tests own the clock."""
    stamped = _CACHE.get(instance_url)
    current = now()
    if stamped is not None and current - stamped[0] < AUDIT_TTL_SECONDS:
        return stamped[1]
    report = audit_privileges(get_json, instance_url, username, profile_name)
    _CACHE[instance_url] = (current, report)
    return report


def reset_cache() -> None:
    """Drop cached audits. For tests and for re-proving a permission change."""
    _CACHE.clear()


def report() -> int:
    """Print the account's destructive reach; exit non-zero while it is unsafe.

    Run after any change to the integration user or its permission set:

        python -m grant_watch.enrich.salesforce_privileges

    This deliberately does NOT live in `cli.py`, which sits at 999 of the 1000-line
    rule-4 cap -- a single added subcommand breaks the build for whoever is next.
    """
    import os

    import requests
    from dotenv import load_dotenv

    # Without this the command reads an empty environment and reports a
    # configuration error as though the account were unreachable.
    load_dotenv()

    domain = os.environ["SALESFORCE_WRITE_MY_DOMAIN_URL"].rstrip("/")
    granted = requests.post(
        f"{domain}/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": os.environ["SALESFORCE_WRITE_CLIENT_ID"],
            "client_secret": os.environ["SALESFORCE_WRITE_CLIENT_SECRET"],
        },
        timeout=30,
    )
    granted.raise_for_status()
    body = granted.json()
    token, instance = body["access_token"], body["instance_url"].rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    version = os.environ.get("SALESFORCE_API_VERSION", "v60.0")

    def get_json(path: str, params: dict[str, str] | None) -> dict[str, Any]:
        """Read one Salesforce REST resource with the writer credentials."""
        response = requests.get(
            f"{instance}/services/data/{version}/{path}",
            params=params,
            headers=headers,
            timeout=20,
        )
        response.raise_for_status()
        return response.json()

    info = requests.get(
        f"{instance}/services/oauth2/userinfo", headers=headers, timeout=20
    )
    who = str(info.json().get("preferred_username") or "") if info.ok else ""

    audit = audit_privileges(get_json, instance, who)
    print(f"instance : {instance}")
    print(f"account  : {who or '(identity unavailable)'}")
    print(f"{'object':<24}{'create':>8}{'update':>8}{'DELETE':>8}")
    for item in audit.objects:
        if not item.accessible:
            print(f"{item.sobject:<24}{'— no access at all —':>26}")
            continue
        mark = "  <-- CAN DELETE" if item.deletable else ""
        print(
            f"{item.sobject:<24}{str(item.createable):>8}"
            f"{str(item.updateable):>8}{str(item.deletable):>8}{mark}"
        )
    if audit.is_safe:
        print("\nSAFE: this account cannot delete anything Grant touches.")
        return 0
    for reason in audit.violations():
        print(f"\nUNSAFE: {reason}")
    return 1


if __name__ == "__main__":
    raise SystemExit(report())
