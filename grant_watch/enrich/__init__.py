"""Phase 2 — contact enrichment: find WHO runs technology/the funding at an awardee.

One module: finder.py (Firecrawl search + scrape -> Claude extraction -> code-level
verification). The Constitution's hardest rule lives here: a contact email is stored
ONLY if it appears verbatim in a page we actually fetched. not_found is a first-class
honest outcome.
"""
