"""Salesforce cross-reference — STRICTLY READ-ONLY.

Grant answers "is this awardee already in our CRM, and who owns it?" so a rep never
cold-emails an org a teammate is already working. It returns record links + owner.

╔═══════════════════════════════════════════════════════════════════════════════════╗
║ READ-ONLY IS A HARD RULE. This module issues ONLY GET /query and GET /search.       ║
║ It contains NO create / update / delete / upsert — not now, not ever. All data      ║
║ access goes through _readonly_get(), which refuses anything but GET. (The one POST  ║
║ is the OAuth token request, which mints a read token — it changes no CRM data.)     ║
║ Defense in depth: the connected app's run-as user should also have a read-only      ║
║ profile, so even a bug cannot write.                                                ║
╚═══════════════════════════════════════════════════════════════════════════════════╝

Matching is intelligent, not exact: it searches by the DISTINCTIVE words of the name
(so "ABC Schools" finds "ABC School District"), across all fields (so a domain, phone,
or city can match too). Uncertain hits are labeled 'possible', never asserted
(CLAUDE.md rule 1). Auth: OAuth client-credentials against the sandbox my-domain
(verified 2026-07-14). Same code serves production once those creds go in.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests

API_VERSION = "v60.0"
_TOKEN_CACHE: dict[str, Any] = {"access_token": None, "instance_url": None, "exp": 0.0}

Progress = Callable[[str], None]
_NOOP: Progress = lambda _msg: None

# Generic org words dropped from the SEARCH TERM so name variations still match
# (e.g. "Mt Morris Consolidated Schools" and "Mt Morris School District" both match
# the distinctive term "Mt Morris").
_GENERIC_WORDS = {
    "school", "schools", "district", "districts", "unified", "consolidated", "public",
    "city", "town", "county", "of", "the", "inc", "llc", "corp", "corporation",
    "company", "co", "department", "dept", "hospital", "hospitals", "health", "system",
    "systems", "medical", "center", "authority", "board", "education", "isd", "usd",
}


@dataclass
class SFMatch:
    """One Salesforce record match with a clickable Lightning link."""

    sobject: str          # Account | Lead | Opportunity
    record_id: str
    name: str             # record name (Account.Name / Lead full name)
    company: str          # Lead.Company / '' for Account/Opp
    owner: str
    link: str
    confidence: str       # high (strong match) | possible (fuzzy)


@dataclass
class SFResult:
    matched: bool = False
    matches: list[SFMatch] = field(default_factory=list)
    error: str = ""


def _auth() -> tuple[str, str]:
    """Return (access_token, instance_url), cached until ~5 min before expiry."""
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
                        exp=now + 25 * 60)
    return _TOKEN_CACHE["access_token"], _TOKEN_CACHE["instance_url"]


def _readonly_get(path: str, params: dict[str, str], token: str, inst: str) -> dict:
    """The ONLY data-access primitive: an authenticated GET. Read-only by construction
    — this function cannot issue a write, and nothing else in this module talks to the
    CRM data API."""
    r = requests.get(f"{inst}/services/data/{API_VERSION}/{path}", params=params,
                     headers={"Authorization": f"Bearer {token}"}, timeout=20)
    r.raise_for_status()
    return r.json()


def distinctive_term(entity: str) -> str:
    """Strip SOSL-reserved punctuation and generic org words, leaving the words that
    actually identify the org — so variations of the name still match."""
    cleaned = re.sub(r"[?&|!{}\[\]()^~*:\\\"'+\-.,]", " ", entity)
    words = [w for w in cleaned.split()
             if len(w) > 1 and w.lower() not in _GENERIC_WORDS]
    return " ".join(words) or cleaned.strip()


def lookup(entity: str, domain: str = "", phone: str = "",
           on_progress: Progress | None = None) -> SFResult:
    """Intelligent, read-only CRM search across Account/Lead/Opportunity by the org's
    distinctive name (and, if given, domain/phone). Never raises — returns
    SFResult(error=...) so Grant can speak honestly."""
    say = on_progress or _NOOP
    say("Checking Salesforce")
    try:
        token, inst = _auth()
    except (requests.RequestException, KeyError) as exc:
        return SFResult(error=f"Salesforce auth failed ({type(exc).__name__})")

    # Search terms: distinctive name first, then any domain-core / phone digits given.
    terms: list[str] = []
    name_term = distinctive_term(entity)
    if name_term:
        terms.append(name_term)
    if domain:
        core = re.sub(r"^https?://(www\.)?", "", domain).split("/")[0].split(".")[0]
        if core:
            terms.append(core)
    if phone:
        digits = re.sub(r"\D", "", phone)
        if len(digits) >= 7:
            terms.append(digits[-7:])  # last 7 digits are the discriminating part

    found: dict[str, SFMatch] = {}
    entity_low = entity.lower()
    for term in terms:
        # IN ALL FIELDS lets a domain/phone/city match too, not just the name.
        sosl = (f"FIND {{{term}}} IN ALL FIELDS RETURNING "
                f"Account(Id,Name,Owner.Name), "
                f"Lead(Id,Name,Company,Owner.Name), "
                f"Opportunity(Id,Name,Owner.Name) LIMIT 20")
        try:
            records = _readonly_get("search", {"q": sosl}, token, inst).get(
                "searchRecords", [])
        except requests.RequestException:
            continue
        for rec in records:
            if rec["Id"] in found:
                continue
            sobj = rec["attributes"]["type"]
            name = rec.get("Name") or "(unnamed)"
            company = rec.get("Company") or ""
            owner = ((rec.get("Owner") or {}).get("Name")) or ""
            hay = f"{name} {company}".lower()
            conf = "high" if (name_term.lower() in hay or entity_low in hay
                              or hay.strip() in entity_low) else "possible"
            found[rec["Id"]] = SFMatch(
                sobject=sobj, record_id=rec["Id"], name=name, company=company,
                owner=owner, link=f"{inst}/lightning/r/{sobj}/{rec['Id']}/view",
                confidence=conf)

    matches = sorted(found.values(),
                     key=lambda m: 0 if m.confidence == "high" else 1)
    if matches:
        say(f"Found {len(matches)} in Salesforce")
    return SFResult(matched=bool(matches), matches=matches)
