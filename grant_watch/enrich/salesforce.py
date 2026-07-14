"""Salesforce cross-reference: does this awardee already exist in our CRM, and who owns it?

Grant surfaces the answer as an honest, actionable line — "already an Account, owned by
Anthony, here's the link" or "no CRM record — net-new" — so a rep never cold-emails a
district a teammate is already working.

Auth: OAuth 2.0 client-credentials against the sandbox my-domain (verified working
2026-07-14). Search: SOSL across Account / Lead / Opportunity by entity name. Matching
is fuzzy, so uncertain hits are labeled 'possible match' — never asserted as fact
(CLAUDE.md rule 1). Sandbox now; production creds slot in later (same code).
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

API_VERSION = "v60.0"
_TOKEN_CACHE: dict[str, Any] = {"access_token": None, "instance_url": None, "exp": 0.0}


@dataclass
class SFMatch:
    """One Salesforce record match with a clickable Lightning link."""

    sobject: str          # Account | Lead | Opportunity
    record_id: str
    name: str
    owner: str            # owner name, or '' if not resolved
    link: str
    confidence: str       # high (strong name match) | possible (fuzzy)


@dataclass
class SFResult:
    matched: bool = False
    matches: list[SFMatch] = field(default_factory=list)
    error: str = ""


def _auth() -> tuple[str, str]:
    """Return (access_token, instance_url), cached until ~2 min before expiry."""
    now = time.time()
    if _TOKEN_CACHE["access_token"] and _TOKEN_CACHE["exp"] > now:
        return _TOKEN_CACHE["access_token"], _TOKEN_CACHE["instance_url"]
    dom = os.environ["SALESFORCE_MY_DOMAIN_URL"].rstrip("/")
    resp = requests.post(f"{dom}/services/oauth2/token", data={
        "grant_type": "client_credentials",
        "client_id": os.environ["SALESFORCE_CLIENT_ID"],
        "client_secret": os.environ["SALESFORCE_CLIENT_SECRET"],
    }, timeout=20)
    resp.raise_for_status()
    body = resp.json()
    _TOKEN_CACHE.update(access_token=body["access_token"],
                        instance_url=body.get("instance_url", dom),
                        exp=now + 25 * 60)  # client-cred tokens ~30 min; refresh early
    return _TOKEN_CACHE["access_token"], _TOKEN_CACHE["instance_url"]


def _sosl_term(entity: str) -> str:
    """Strip SOSL-reserved punctuation; keep the meaningful words for a name search."""
    cleaned = re.sub(r"[?&|!{}\[\]()^~*:\\\"'+\-.,]", " ", entity)
    words = [w for w in cleaned.split() if len(w) > 1]
    return " ".join(words)


def lookup(entity: str) -> SFResult:
    """Search CRM for an awardee across Account/Lead/Opportunity. Never raises —
    returns SFResult(error=...) on failure so Grant can speak honestly."""
    try:
        token, inst = _auth()
    except (requests.RequestException, KeyError) as exc:
        return SFResult(error=f"Salesforce auth failed ({type(exc).__name__})")

    term = _sosl_term(entity)
    if not term:
        return SFResult()
    sosl = (f"FIND {{{term}}} IN NAME FIELDS RETURNING "
            f"Account(Id,Name,Owner.Name), "
            f"Lead(Id,Name,Company,Owner.Name), "
            f"Opportunity(Id,Name,Owner.Name) LIMIT 10")
    try:
        r = requests.get(f"{inst}/services/data/{API_VERSION}/search",
                         params={"q": sosl},
                         headers={"Authorization": f"Bearer {token}"}, timeout=20)
        r.raise_for_status()
        records = r.json().get("searchRecords", [])
    except requests.RequestException as exc:
        return SFResult(error=f"Salesforce search failed ({type(exc).__name__})")

    entity_low = entity.lower()
    matches: list[SFMatch] = []
    for rec in records:
        sobj = rec["attributes"]["type"]
        rid = rec["Id"]
        name = rec.get("Name") or rec.get("Company") or "(unnamed)"
        owner = ((rec.get("Owner") or {}).get("Name")) or ""
        # 'high' when the record name is essentially the entity; else 'possible'.
        conf = "high" if name.lower() in entity_low or entity_low in name.lower() \
            else "possible"
        matches.append(SFMatch(
            sobject=sobj, record_id=rid, name=name, owner=owner,
            link=f"{inst}/lightning/r/{sobj}/{rid}/view", confidence=conf))
    return SFResult(matched=bool(matches), matches=matches)
