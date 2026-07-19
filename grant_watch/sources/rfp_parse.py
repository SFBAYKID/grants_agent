"""Pure, fixture-testable logic for the security-RFP discovery source.

Why this is a separate module (architectural-critic, 2026-07-18): the RFP source is
the first poller that mints a lead from an LLM reading an ARBITRARY page, so the
anti-fabrication bar (Constitution rule 1) is higher than any HTTP poller. The model
output is UNTRUSTED — every trust-bearing decision lives here as a pure function over
`(model_output, page_text)`, so the whole surface is tested on recorded fixtures with
no Firecrawl/Anthropic call. `rfp.py` holds only the live I/O orchestration.

The load-bearing rule: *verbatim-present is not semantically-correct*. A page carries
many dates (pre-bid meeting, questions-due, addendum, award); proving a date STRING is
on the page does not prove it is the SUBMISSION deadline. So we verify the (deadline
label + date) are ADJACENT, parse the date exactly as printed (never the ISO form,
which the page never contains), and drop anything we cannot prove.
"""

from __future__ import annotations

import re
from datetime import date

from ..enrich.finder import _host, _text_field_on_page
from ..models import (
    DatePrecision,
    FundingEventType,
    RawItem,
    VerificationStatus,
)

# --- physical-security relevance (H2): allow the catalog, block the look-alikes ----
# "security" alone is far too broad — a guard-services or cybersecurity RFP is NOT
# physical security. Deterministic over the verified title/excerpt, never the model's
# own category judgment.
_RELEVANCE_ALLOW_RE = re.compile(
    r"camera|cctv|video surveillance|surveillance system|access control|"
    r"door hardening|door access|card reader|badge reader|keycard|"
    r"intrusion|burglar alarm|security vestibule|entry control|entrance control|"
    r"video management|door hardware|electronic lock|security camera",
    re.IGNORECASE,
)
_RELEVANCE_BLOCK_RE = re.compile(
    r"guard service|security guard|armed guard|security officer|"
    r"cyber\s?security|information security|network security|data security|"
    r"security deposit|food security|job security|social security|"
    r"school resource officer|\bsro\b|security screening personnel",
    re.IGNORECASE,
)

# --- deadline labels (C1): the SUBMISSION deadline, never a meeting/questions date ---
_DEADLINE_LABEL_RE = re.compile(
    r"(?:proposals?|responses?|bids?|submissions?|statements? of qualifications?|"
    r"soq|qualifications?)\s+(?:are\s+)?due"
    r"|proposals?\s+must\s+be\s+received"
    r"|(?:submittal|submission|response|proposal|bid|closing|due)\s+deadline"
    r"|bid\s*/?\s*rfp\s+due\s+date"
    r"|closing\s+date(?:\s*/?\s*time)?"
    r"|due\s+date(?:\s*/?\s*time)?"
    r"|will\s+be\s+(?:accepted|received)\s+until"
    r"|received\s+(?:by|until|no\s+later\s+than)"
    r"|no\s+later\s+than"
    r"|deadline\s+(?:for|to)\s+(?:submit|submission)",
    re.IGNORECASE,
)
# Labels that mark a DIFFERENT date — if one sits between the deadline label and the
# date, the date is not the submission deadline.
_NON_DEADLINE_LABEL_RE = re.compile(
    r"pre-?bid|pre-?proposal|pre-?submittal|questions?\s+(?:are\s+)?due|"
    r"question\s+deadline|addend|award\s+date|notice\s+of\s+award|"
    r"publication\s+date|posted|issue\s+date|meeting|walk-?through|site\s+visit|"
    r"anticipated|projected\s+award",
    re.IGNORECASE,
)

# --- posting-date labels: when the RFP was PUT OUT (drives fresh=GOLD vs SILVER) -----
# Chase (2026-07-18): a recently-posted RFP is GOLD, an older-but-open one is SILVER.
# Same adjacency discipline as the deadline — a posting date must sit next to a posting
# label, never guessed. Absent/unverifiable posting date -> SILVER (conservative).
_POSTED_LABEL_RE = re.compile(
    r"(?:date\s+)?(?:posted|issued|advertised|released|published)"
    r"|(?:posting|publication|issue|release|advertis\w*)\s+date"
    r"|date\s+of\s+(?:issue|posting|publication)",
    re.IGNORECASE,
)

# --- closed/withdrawn status (H1): open needs a future date AND no closed token -----
_CLOSED_STATUS_RE = re.compile(
    # status label then a closed word within a few separators (handles markdown tables
    # like "| **Status:** | Closed |")
    r"status[\s:|*]{0,10}(?:closed|cancel+ed|awarded|withdrawn|expired|complete)"
    r"|no\s+longer\s+accepting\s+(?:bids|proposals)"
    r"|this\s+(?:bid|rfp|solicitation)\s+(?:is|has)\s+(?:closed|been\s+awarded)",
    re.IGNORECASE,
)

# --- government awarder patterns (C4): the entity must be a government, on its host --
_GOV_ENTITY_RE = re.compile(
    r"\bcity of\b|\btown of\b|\bvillage of\b|\bborough of\b|\btownship of\b|"
    r"\bcounty of\b|\b\w+ county\b|school district|\bisd\b|\busd\b|unified|"
    r"public schools|\bschools?\b|\bacademy\b|\bcity\b|\btown\b|municipal|"
    r"board of education",
    re.IGNORECASE,
)
_GOV_HOST_RE = re.compile(r"\.gov$|\.k12\.\w\w\.us$|\.\w\w\.us$|\.us$", re.IGNORECASE)
# generic name words that don't identify a specific place — never the distinctive token
_GENERIC_ENTITY_WORDS = frozenset(
    {
        "city", "town", "village", "borough", "township", "county", "of", "the",
        "school", "district", "public", "schools", "unified", "board", "education",
        "municipal", "municipality", "isd", "usd", "department", "police",
    }
)

_STATE_FROM_HOST_RE = re.compile(r"\.(\w\w)\.us$", re.IGNORECASE)
_MONTHS = {
    m: i
    for i, m in enumerate(
        (
            "january", "february", "march", "april", "may", "june", "july",
            "august", "september", "october", "november", "december",
        ),
        start=1,
    )
}
_MONTH_ABBR = {k[:3]: v for k, v in _MONTHS.items()}


def _distinctive_token(entity: str) -> str:
    """Longest place-identifying word from an entity name (e.g. 'kemah', 'woodland')."""
    words = re.findall(r"[a-z0-9]+", entity.lower())
    candidates = [w for w in words if w not in _GENERIC_ENTITY_WORDS and len(w) >= 4]
    return max(candidates, key=len) if candidates else ""


def entity_matches_host(entity: str, url: str) -> bool:
    """The awarder must be a government whose name is echoed by its own official host.

    Blocks C4 (a vendor/architect named on the page) and aggregator hosts: the host
    must be a government TLD, the entity must read as a government, and a distinctive
    place token from the entity must appear in the host (City of Kemah -> kemahtx.gov).

    This deliberately trades recall for zero fabrication (the critic's C4): an RFP on a
    heavily-abbreviated official domain (Ypsilanti Community Schools on ycschools.us) or
    on a document CDN (finalsite/thrillshare) is dropped rather than trusted. As a
    labeled probe, missing some open RFPs is acceptable; asserting a wrong awarder is not.
    """
    host = _host(url)
    if not host or not _GOV_HOST_RE.search(host):
        return False
    if not _GOV_ENTITY_RE.search(entity or ""):
        return False
    token = _distinctive_token(entity)
    return bool(token) and token in host.replace(".", "")


def state_from(url: str, model_state: str) -> str:
    """State from the host's `.<st>.us` TLD when present, else a clean 2-letter model
    value, else '' — a 2-letter code is never verbatim-verifiable, so the host wins."""
    match = _STATE_FROM_HOST_RE.search(_host(url))
    if match:
        return match.group(1).upper()
    candidate = (model_state or "").strip().upper()
    return candidate if re.fullmatch(r"[A-Z]{2}", candidate) else ""


def parse_iso_date(raw: str) -> str | None:
    """Parse ONE unambiguous printed date to ISO, or None. Never guesses.

    Accepts a single M/D/Y (2- or 4-digit year), a single 'Month D, YYYY', or a single
    ISO date — optionally wrapped in a weekday/time ('Fri, 01/30/2026 - 2:00 PM'). Two
    date tokens (a range like 'May 1-28, 2026'), zero tokens, 'TBD', or an out-of-range
    day/month all return None: an ambiguous or absent date is omitted, not invented (C2).
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    iso = re.findall(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    numeric = re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", text)
    named = re.findall(
        r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", text
    )
    named = [n for n in named if n[0].lower()[:3] in _MONTH_ABBR]
    if len(iso) + len(numeric) + len(named) != 1:  # zero, or a range/ambiguous
        return None
    try:
        if iso:
            y, mo, d = (int(x) for x in iso[0])
        elif numeric:
            mo, d, yy = (int(x) for x in numeric[0])  # US gov is reliably M/D/Y
            y = yy + 2000 if yy < 100 else yy
        else:
            mon, d, y = named[0]
            mo = _MONTHS.get(mon.lower()) or _MONTH_ABBR.get(mon.lower()[:3], 0)
            d = int(d)
            y = int(y)
        return date(y, mo, d).isoformat()  # date() rejects an impossible day/month
    except (ValueError, TypeError):
        return None


def _label_adjacent_date(
    page_text: str,
    printed_date: str,
    want_re: re.Pattern[str],
    avoid_re: re.Pattern[str] | None,
    window: int = 90,
) -> str:
    """Return the verbatim '<want label> … <date>' evidence line, or '' (C1/M2).

    The date must appear on the page (destroying it as evidence otherwise), AND a
    `want_re` label must sit within `window` characters before it, AND no `avoid_re`
    label may sit between that label and the date. This is what `_text_field_on_page`
    cannot do — it flattens the whole page and loses adjacency, so a pre-bid date that
    is verbatim-present would pass.
    """
    if not printed_date.strip() or not _text_field_on_page(page_text, printed_date):
        return ""
    # Match the printed date with flexible whitespace/case — scraped markdown spacing
    # varies, and a false-negative here would silently drop a real open RFP.
    pattern = r"\s+".join(re.escape(tok) for tok in printed_date.split())
    for occurrence in re.finditer(pattern, page_text, re.IGNORECASE):
        preceding = page_text[max(0, occurrence.start() - window) : occurrence.start()]
        label = None
        for label in want_re.finditer(preceding):
            pass  # keep the LAST (closest) wanted label before the date
        if label is None:
            continue
        between = preceding[label.end() :]
        if avoid_re is not None and avoid_re.search(between):
            continue  # a different-date label sits closer to the date — not it
        snippet = preceding[label.start() :] + occurrence.group()
        return " ".join(snippet.split())[:300]
    return ""


def label_adjacent_date(page_text: str, printed_date: str, window: int = 90) -> str:
    """Evidence line proving the printed date is the SUBMISSION deadline (C1)."""
    return _label_adjacent_date(
        page_text, printed_date, _DEADLINE_LABEL_RE, _NON_DEADLINE_LABEL_RE, window
    )


def posted_iso_date(page_text: str, printed_date: str, window: int = 90) -> str | None:
    """ISO posting date only when a posting label sits right beside the printed date.

    Freshness (GOLD vs SILVER) hangs on this, so it is held to the same adjacency bar:
    a bare date, or one not next to a posting label, yields None -> the RFP defaults to
    SILVER rather than being called freshly-posted on a guess.
    """
    if not _label_adjacent_date(page_text, printed_date, _POSTED_LABEL_RE, None, window):
        return None
    return parse_iso_date(printed_date)


def is_relevant(text: str) -> bool:
    """Physical-security only: an allow-list term present and no block-list term (H2)."""
    return bool(_RELEVANCE_ALLOW_RE.search(text)) and not _RELEVANCE_BLOCK_RE.search(
        text
    )


def is_index_page(page_text: str) -> bool:
    """Skip multi-solicitation index/aggregator pages (C5) — cross-row field mixing is a
    fabrication vector, and the verbatim fields we trust live on single-RFP pages.

    The reliable signal is MORE THAN ONE distinct solicitation number: a single RFP
    restates its own number many times (deduped by the set), while an index lists
    several. Date-label count is NOT usable — one real RFP normally prints a pre-bid,
    a questions-due, and a proposals-due label, so a threshold there flags valid pages.
    A very high deadline-label count is kept only as a weak backstop for number-less
    index pages.
    """
    bid_numbers = len(
        set(re.findall(r"(?:bid|rfp|rfq|project|solicitation)\s*(?:no\.?|number|#)?\s*"
                       r":?\s*([0-9]{2,}[-/][0-9]{2,})", page_text, re.IGNORECASE))
    )
    deadline_labels = len(_DEADLINE_LABEL_RE.findall(page_text))
    return bid_numbers > 1 or deadline_labels > 8


def has_closed_status(page_text: str) -> bool:
    """A verbatim closed/cancelled/awarded/withdrawn status token (H1)."""
    return bool(_CLOSED_STATUS_RE.search(page_text))


def rfp_item_id(entity: str, rfp_number: str, title: str, due_iso: str, url: str) -> str:
    """Stable dedup key namespaced by ENTITY (C3): a bare 'RFP 2026-05' is not globally
    unique, so two cities' '2026-05' must never collide in upsert_lead."""
    ent = "-".join(re.findall(r"[a-z0-9]+", (entity or "").lower())) or "unknown"
    if rfp_number and rfp_number.strip():
        num = "-".join(re.findall(r"[a-z0-9]+", rfp_number.lower()))
        return f"{ent}|{num}"
    title_tokens = "-".join(re.findall(r"[a-z0-9]+", (title or "").lower())[:6])
    if title_tokens and due_iso:
        return f"{ent}|{title_tokens}|{due_iso}"
    # last resort: normalized URL (strip query/fragment/trailing slash, lowercase host)
    clean = re.sub(r"[?#].*$", "", url or "").rstrip("/").lower()
    return f"{ent}|{clean}" if clean else f"{ent}|{title_tokens or due_iso}"


def build_rawitem(
    extracted: dict[str, str], page_text: str, url: str, today: date
) -> RawItem | None:
    """Turn ONE untrusted model extraction of ONE scraped page into a verified open
    physical-security RFP RawItem, or None. Every gate below is a reason to drop.

    Order (C1/C2/L1): reject index pages; require a gov entity echoed by the official
    host; require the printed due date to be label-adjacent on the page; parse THAT
    verbatim date to ISO; require physical-security relevance on verified text; require
    OPEN (future date AND no closed status). Only then is it VERIFIED; scoring then
    grades it GOLD (a verified recent posting date) or SILVER (older / unproven posting).
    """
    if is_index_page(page_text):
        return None
    entity = (extracted.get("entity") or "").strip()
    if not entity or not entity_matches_host(entity, url):
        return None

    printed_date = (extracted.get("due_date") or "").strip()
    evidence = label_adjacent_date(page_text, printed_date)
    if not evidence:
        return None
    due_iso = parse_iso_date(printed_date)
    if not due_iso:
        return None
    if date.fromisoformat(due_iso) < today or has_closed_status(page_text):
        return None  # closed by date or by an explicit status token

    title = (extracted.get("title") or "").strip()
    rfp_number = (extracted.get("rfp_number") or "").strip()
    # Relevance is judged only on text we can trust: the title (verbatim-verified) plus
    # the deadline evidence line — never the model's free-form category.
    verified_title = title if title and _text_field_on_page(page_text, title) else ""
    relevance_text = f"{verified_title} {evidence} {rfp_number}"
    if not is_relevant(relevance_text):
        return None

    state = state_from(url, extracted.get("state") or "")
    portal = (extracted.get("portal") or "").strip()[:80]
    item_id = rfp_item_id(entity, rfp_number, verified_title or title, due_iso, url)
    # Posting date drives GOLD (freshly put out) vs SILVER (older-but-open) in scoring —
    # only when a posting label sits next to it; otherwise blank -> SILVER default.
    posted_iso = posted_iso_date(page_text, (extracted.get("posted_date") or "").strip())
    return RawItem(
        source="rfp",
        item_id=item_id,
        title=(verified_title or f"Security RFP — {entity}")[:200],
        entity=entity,
        state=state,
        program="RFP:security",
        amount=None,  # a solicitation has no awarded dollars — never fabricate one
        start=posted_iso or "",  # verified posting date, or blank when unproven
        end=due_iso,  # the SUBMISSION deadline; scoring grades SILVER/GOLD when >= today
        url=url,
        raw={
            "rfp_number": rfp_number,
            "portal": portal,
            "due_date_printed": printed_date,
            "posted_date_printed": (extracted.get("posted_date") or "").strip(),
        },
        event_type=FundingEventType.RFP_POSTED,
        event_date=posted_iso or "",  # posting date -> freshness (GOLD when recent)
        date_precision=DatePrecision.DAY,
        application_portal=portal,
        source_locator=rfp_number or item_id,
        evidence_excerpt=evidence,
        verification_status=VerificationStatus.VERIFIED,
    )
