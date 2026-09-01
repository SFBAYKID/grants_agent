"""Forward-only schema for lead claims — a rep saying "I'm taking this one".

WHY THIS EXISTS. On 2026-09-01 a rep wrote "@Grant I'm taking Gobles Public Schools"
and Grant answered, truthfully, that it had no claim mechanism and nothing to mark.
The sentence then went nowhere. Meanwhile `nudge_sources._unengaged_cards` selects
`posts` rows with no `engagement` row — and engagement is keyed on `post_id`, so a
claim made in a DIFFERENT thread leaves the card looking untouched. Grant would have
nudged the same rep about the same lead, and then told his MANAGER that the card went
to him and nothing came back. About a lead he had already said he was taking.

A LEDGER, NOT A FLAG, for the same reason `capability_asks` is one. A claim is not
private bookkeeping: Grant will later tell a THIRD party "Kerry has this one", and
that is an assertion about what a named colleague said on a date. Under rule 1 it
ships with the receipt — their words VERBATIM and the Slack coordinates to check them
— or it is not sayable at all. `claim_text` is never paraphrased or regenerated; a
summary that drifts is Grant putting words in someone's mouth.

ONE LIVE CLAIM PER LEAD, ENFORCED BY THE SCHEMA rather than by the tool. A partial
unique index on `lead_id WHERE released_at IS NULL` means two reps claiming the same
lead in the same minute is an IntegrityError the caller turns into "Kerry already has
that one", instead of a race the tool has to remember to check. Released rows stay,
so the history of who held a lead survives a hand-off.

WHY `leads.assigned_to` / `assigned_at` ARE LEFT ALONE. Those columns exist (added by
migration 1) and are read and written by nothing in the repo. Reviving them as a
denormalized "current owner" would put the same fact in two places, and the candidate
queries this feature gates already exclude rows by subquery against `posts` and
`notification_outbox` — one more `NOT IN (SELECT … FROM lead_claims …)` matches an
idiom that is already there. They stay dead, and are worth removing on their own
merits rather than half-revived here.

AND `leads.status` IS NOT TOUCHED EITHER. Parking a lead through the existing status
values looks attractive — `nudges._suppression_reason` already honours
{dead, snoozed, contacted, not_relevant}. But `db.CAMPAIGN_ELIGIBLE_STATUSES` is
{new, surfaced, contacted}, so writing a parked status would make the lead ineligible
for a Salesforce campaign — and the rep who just claimed it is precisely the person
about to work it. Parking must not disarm the claimer's own next step.
"""

from __future__ import annotations

import sqlite3


def migration_48_lead_claims(conn: sqlite3.Connection) -> None:
    """Create the append-only ledger of reps claiming individual leads."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS lead_claims (
              id INTEGER PRIMARY KEY,
              lead_id INTEGER NOT NULL REFERENCES leads(id),
              slack_user TEXT NOT NULL,
              audience TEXT NOT NULL,
              thread_ts TEXT NOT NULL,
              message_ts TEXT NOT NULL,
              claim_text TEXT NOT NULL,
              claimed_at TIMESTAMP NOT NULL,
              released_at TIMESTAMP,
              released_by TEXT,
              release_note TEXT
           )"""
    )
    # One LIVE claim per lead. Partial, so a released claim never blocks a re-claim
    # and the hand-off history is kept rather than overwritten.
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS ix_lead_claims_live
           ON lead_claims(lead_id) WHERE released_at IS NULL"""
    )
    # "What does this person hold?" — asked by the tool before every write, so it can
    # answer idempotently instead of filing a second row for the same rep.
    conn.execute(
        """CREATE INDEX IF NOT EXISTS ix_lead_claims_holder
           ON lead_claims(slack_user, released_at)"""
    )
