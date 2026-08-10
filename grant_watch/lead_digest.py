"""One renderer for lead results that go to a person WITHOUT a model in between.

WHY THIS MODULE EXISTS AT ALL. Two surfaces send search results straight to a human:
the reminder worker posts them into Slack, and `email_results` mails them. They were
written days apart and drifted immediately — the reminder worker learned to strip
model-facing coaching and to broaden a zero-result search, and `email_results`
learned neither. The result shipped: a rep received an email containing the literal
instruction "Offer these to the user (with counts) and ask which to run", followed by
nothing they could act on.

Fixing that in both places would have left the same trap for the third caller. The
honest fix is that there is only one place to fix.

TWO RULES, BOTH LEARNED FROM A REAL MESSAGE:
  1. Tool text is written FOR THE MODEL. Anything reaching a person unmediated goes
     through `presentation.for_human` first.
  2. A result that reports a miss and stops is a message people learn to ignore. When
     the exact search is empty, go and get the nearest real leads instead of merely
     counting them.
"""

from __future__ import annotations

from typing import Any

from .presentation import for_human
from .reminders import search_kwargs
from .slack.search import NO_MATCH_PREFIX, search_leads


def render(spec: dict[str, Any]) -> str:
    """Human-safe lead results for one frozen search spec.

    Returns "" when the spec carries no filters at all, so a caller can tell the
    difference between "nothing to search for" and "searched and found nothing".
    """
    kwargs = search_kwargs(spec)
    if not kwargs or set(kwargs) == {"limit"}:
        return ""
    text, _artifact = search_leads(**kwargs)
    body = for_human(text)
    if not body.startswith(NO_MATCH_PREFIX):
        return body

    # Empty. Widen by dropping the state — the narrowest filter and the one most
    # likely to be the reason — and hand over something workable rather than a count.
    broadened = {key: value for key, value in kwargs.items() if key != "state"}
    if broadened == kwargs:
        return body
    wider, _ = search_leads(**broadened)
    wider = for_human(wider)
    if wider.startswith(NO_MATCH_PREFIX):
        return body
    where = str(kwargs.get("state", "")).upper()
    return f"Nothing new in {where} today, but here's the closest I've got:\n\n{wider}"
