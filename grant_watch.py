"""
grant_watch.py — First-pass WA security-grant/RFP watcher for Monarch Connected.

VERIFICATION STATUS (as of 2026-07-13, tested live via browser session):
  [VERIFIED] Grants.gov search2 API  — no auth, POST JSON, returned live data
  [VERIFIED] USASpending spending_by_award API — no auth, POST JSON, returned live data
  [VERIFIED] WEBS BidCalendar.aspx   — public HTML page, no login (parse logic below is
             written but NOT yet executed against the live page — verify selectors on first run)
  [STUBBED]  SAM.gov Opportunities API — requires api_key (get from
             https://sam.gov/workspace/profile/account-details while signed in)

NOTE: This script itself has NOT been executed end-to-end (built in a sandbox without
egress to these domains). The Grants.gov and USASpending request payloads are exact
copies of calls that returned real data in the browser. Run once manually before cron.

Usage:
    pip install requests beautifulsoup4
    python grant_watch.py
"""

import json
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import requests

DB_PATH = Path(__file__).parent / "grant_watch.db"

# Keywords tuned to Verkada's wheelhouse. NOTE: bare "surveillance" and "security"
# are too noisy on Grants.gov (CDC disease surveillance, cybersecurity, port security)
# — verified in live testing. Use the phrase list + a scoring pass instead.
KEYWORDS = [
    "school violence prevention",
    "physical security",
    "access control",
    "video surveillance",
    "security camera",
    "cctv",
    "intrusion detection",
    "visitor management",
]

# SVPP is split across two assistance listings — VERIFIED via USASpending:
#   16.710 = COPS umbrella (FY21–FY24 SVPP awards live here)
#   16.071 = SVPP-specific listing (FY25+ awards live here)
SVPP_CFDAS = ["16.710", "16.071"]


# ---------------------------------------------------------------- storage
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS seen (
               source TEXT NOT NULL,
               item_id TEXT NOT NULL,
               title TEXT,
               entity TEXT,
               close_date TEXT,
               amount REAL,
               url TEXT,
               first_seen TEXT,
               raw TEXT,
               PRIMARY KEY (source, item_id)
           )"""
    )
    return conn


def record_if_new(conn, source, item_id, title, entity, close_date, amount, url, raw) -> bool:
    """Insert; return True only if this item was never seen before (i.e., alert-worthy)."""
    try:
        conn.execute(
            "INSERT INTO seen VALUES (?,?,?,?,?,?,?,?,?)",
            (source, str(item_id), title, entity, close_date, amount, url,
             datetime.now().isoformat(), json.dumps(raw)[:5000]),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


# ---------------------------------------------------------------- sources
def poll_grants_gov() -> list[dict]:
    """Grants.gov search2 — VERIFIED endpoint, no key. One request per keyword phrase."""
    out = []
    for kw in KEYWORDS:
        resp = requests.post(
            "https://api.grants.gov/v1/api/search2",
            json={"keyword": kw, "oppStatuses": "posted", "rows": 25},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        for opp in data.get("oppHits", []):
            out.append({
                "source": "grants.gov",
                "item_id": opp["id"],
                "title": opp.get("title", ""),
                "entity": opp.get("agency") or opp.get("agencyName", ""),
                "close_date": opp.get("closeDate", ""),
                "amount": None,
                "url": f"https://www.grants.gov/search-results-detail/{opp['id']}",
                "matched_keyword": kw,
                "raw": opp,
            })
    return out


def poll_usaspending_svpp_awards() -> list[dict]:
    """Districts/cities in WA that WON school-security money — the real lead source.
    VERIFIED payload; returned Castle Rock SD ($500K) + Nespelem SD live."""
    out = []
    for cfda in SVPP_CFDAS:
        resp = requests.post(
            "https://api.usaspending.gov/api/v2/search/spending_by_award/",
            json={
                "filters": {
                    "award_type_codes": ["02", "03", "04", "05"],
                    "program_numbers": [cfda],
                    "recipient_locations": [{"country": "USA", "state": "WA"}],
                    "time_period": [
                        {"start_date": "2018-10-01", "end_date": date.today().isoformat()}
                    ],
                },
                "fields": ["Award ID", "Recipient Name", "Award Amount",
                           "Start Date", "End Date", "Description",
                           "generated_internal_id"],
                "limit": 100,
                "page": 1,
                "subawards": False,
            },
            timeout=30,
        )
        resp.raise_for_status()
        for a in resp.json().get("results", []):
            gid = a.get("generated_internal_id", "")
            out.append({
                "source": f"usaspending:{cfda}",
                "item_id": a.get("Award ID") or gid,
                "title": (a.get("Description") or "")[:120],
                "entity": a.get("Recipient Name", ""),
                "close_date": a.get("End Date", ""),   # end of spend window
                "amount": a.get("Award Amount"),
                "url": f"https://www.usaspending.gov/award/{gid}" if gid else "",
                "matched_keyword": f"CFDA {cfda}",
                "raw": a,
            })
    return out


def poll_webs_bid_calendar() -> list[dict]:
    """WEBS public bid calendar — VERIFIED public (no login). Parse logic untested;
    validate selectors on first live run. Parses raw HTML so collapsed rows are included."""
    from bs4 import BeautifulSoup

    resp = requests.get(
        "https://pr-webs-vendor.des.wa.gov/BidCalendar.aspx",
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0 (MonarchGrantWatch/0.1)"},
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    out = []
    kw_re = re.compile(
        r"camera|surveillance|access control|security|cctv|video|intrusion|alarm|door hardware",
        re.I,
    )
    # First-pass heuristic: scan every table row; refine selectors after inspecting
    # the real markup (ASP.NET WebForms, IDs likely like ctl00_...).
    for tr in soup.find_all("tr"):
        text = " ".join(tr.get_text(" ", strip=True).split())
        if not text or not kw_re.search(text):
            continue
        ref = re.search(r"Ref\s*#?:\s*(\S+)", text)
        item_id = ref.group(1) if ref else text[:80]
        out.append({
            "source": "webs",
            "item_id": item_id,
            "title": text[:200],
            "entity": "",  # org name lives in a parent group header; refine in v2
            "close_date": "",
            "amount": None,
            "url": "https://pr-webs-vendor.des.wa.gov/BidCalendar.aspx",
            "matched_keyword": kw_re.search(text).group(0),
            "raw": {"row_text": text[:500]},
        })
    return out


def poll_sam_gov(api_key: str) -> list[dict]:
    """SAM.gov opportunities — REQUIRES key (verified: keyless request rejected).
    UNVERIFIED details: rate limits and whether 'title' is the only text search field.
    Test with a real key before trusting."""
    resp = requests.get(
        "https://api.sam.gov/prod/opportunities/v2/search",
        params={
            "api_key": api_key,
            "limit": 100,
            "postedFrom": date.today().replace(day=1).strftime("%m/%d/%Y"),
            "postedTo": date.today().strftime("%m/%d/%Y"),
            "state": "WA",
            "title": "security",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    out = []
    for opp in data.get("opportunitiesData", []):
        out.append({
            "source": "sam.gov",
            "item_id": opp.get("noticeId", ""),
            "title": opp.get("title", ""),
            "entity": opp.get("fullParentPathName", ""),
            "close_date": opp.get("responseDeadLine", "") or "",
            "amount": None,
            "url": opp.get("uiLink", ""),
            "matched_keyword": "title:security",
            "raw": {k: opp.get(k) for k in ("noticeId", "title", "postedDate", "type")},
        })
    return out


# ---------------------------------------------------------------- main
def main():
    conn = init_db()
    new_items = []

    pollers = [
        ("Grants.gov", poll_grants_gov),
        ("USASpending SVPP awards", poll_usaspending_svpp_awards),
        ("WEBS bid calendar", poll_webs_bid_calendar),
    ]
    # SAM.gov: enable once key is in env
    import os
    sam_key = os.environ.get("SAM_API_KEY")
    if sam_key:
        pollers.append(("SAM.gov", lambda: poll_sam_gov(sam_key)))
    else:
        print("[skip] SAM.gov — set SAM_API_KEY to enable", file=sys.stderr)

    for name, fn in pollers:
        try:
            items = fn()
            fresh = [i for i in items if record_if_new(
                conn, i["source"], i["item_id"], i["title"], i["entity"],
                i["close_date"], i["amount"], i["url"], i["raw"])]
            print(f"[{name}] {len(items)} items, {len(fresh)} new")
            new_items.extend(fresh)
        except Exception as e:
            print(f"[{name}] ERROR: {e}", file=sys.stderr)

    if new_items:
        print("\n=== NEW ITEMS ===")
        for i in new_items:
            amt = f" ${i['amount']:,.0f}" if i.get("amount") else ""
            print(f"- [{i['source']}] {i['entity']}{amt} — {i['title'][:90]}"
                  f" (close/end: {i['close_date']}) {i['url']}")
    # TODO v2: Claude API relevance scoring, Slack webhook alert, district-page diff watcher


if __name__ == "__main__":
    main()
