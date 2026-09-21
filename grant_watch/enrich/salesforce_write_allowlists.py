"""What Grant is permitted to write in Salesforce, as data rather than as code.

These three constants are the whole write surface: which objects may be created at
all, which blank Lead fields may be filled, and the one fixed sentence the
do-not-call primitive appends. They live apart from the HTTP gateway so that
reading the permission boundary does not mean reading a thousand lines of
transport, and so that widening it is a visible edit to a file that contains
nothing else.
"""

from __future__ import annotations

# Grant never creates Salesforce activity Tasks (Chase, 2026-07-18: "we don't use
# tasks — log it as a note"). Task is deliberately absent from the allowlist so a
# future bug cannot create one; the grant context is logged as a ContentNote.
_ALLOWED_CREATE_OBJECTS = {
    "Campaign",
    "CampaignMemberStatus",
    "Lead",
    "CampaignMember",
    "Note",
    "ContentNote",
    "ContentDocumentLink",
}
# THE ONLY Lead fields Grant may write into an EXISTING record, and only while they
# are EMPTY. This is the narrowest possible breach of the create-only rule, shaped so
# it cannot destroy anything: no field here is ever overwritten and nothing is ever
# cleared. Name, Company, OwnerId and Status are deliberately ABSENT — those are
# identity and workflow, and filling them would change what a record IS and who owns
# it, rather than what is known about it.
_ALLOWED_LEAD_FILL_FIELDS = frozenset(
    {
        "Street",
        "City",
        "State",
        "PostalCode",
        "Phone",
        "MobilePhone",
        "Email",
        "Title",
        "Website",
        "Industry",
        "Number_of_Students__c",
    }
)

# The exact sentence written onto an existing Lead. Code-owned and fixed: the
# marking primitive takes no caller text at all, which is what keeps "mark this lead
# do-not-call" from becoming "append anything to this record".
DO_NOT_CALL_MARKER = (
    "DO NOT CALL: this person is flagged do-not-call; any number for them must "
    "not be dialled, including the main line below."
)
