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
from .spreadsheets import GeneratedArtifact


def render(spec: dict[str, Any]) -> str:
    """Human-safe lead results for one frozen search spec.

    Returns "" when the spec carries no filters at all, so a caller can tell the
    difference between "nothing to search for" and "searched and found nothing".
    """
    kwargs = search_kwargs(spec)
    if not kwargs or set(kwargs) == {"limit"}:
        return ""
    # `for_chat=False`: this text is going into an EMAIL. The chat renderer answers a
    # large result set by offering a spreadsheet, which is a question, and nobody can
    # answer a question that arrives in their inbox from a no-reply sender.
    text, _artifact = search_leads(**kwargs, for_chat=False)
    body = for_human(text)
    if not body.startswith(NO_MATCH_PREFIX):
        return body

    # Empty. Widen by dropping the state — the narrowest filter and the one most
    # likely to be the reason — and hand over something workable rather than a count.
    broadened = {key: value for key, value in kwargs.items() if key != "state"}
    if broadened == kwargs:
        return body
    wider, _ = search_leads(**broadened, for_chat=False)
    wider = for_human(wider)
    if wider.startswith(NO_MATCH_PREFIX):
        return body
    where = str(kwargs.get("state", "")).upper()
    # "Nothing new … today" is DRIP phrasing and this renderer feeds an inbox. An
    # email is read hours or days after it is sent, so "today" is either meaningless
    # or wrong, and "nothing new" implies a daily cadence the reader never subscribed
    # to. Same class as the spreadsheet offer and the refine-your-search trailer: a
    # string carrying an assumption about where it would be read.
    scope = f" in {where}" if where else ""
    return (
        f"No exact matches{scope} for those filters. Here are the closest I have:"
        f"\n\n{wider}"
    )


def render_with_spreadsheet(
    spec: dict[str, Any],
) -> tuple[str, GeneratedArtifact | None]:
    """Render email-safe results and create a matching Excel attachment when found.

    The frozen search still passes through the reminder allowlist, so the email tool
    cannot smuggle identity, filesystem, or connector arguments into ``search_leads``.
    When a state-scoped search is empty, the same documented broadening used by
    :func:`render` supplies both the body and the workbook; the attachment can never
    silently describe a different query from the accompanying text.
    """
    kwargs = search_kwargs(spec)
    if not kwargs or set(kwargs) == {"limit"}:
        return "", None
    text, artifact = search_leads(**kwargs, for_chat=False, export="excel")
    body = for_human(text)
    if artifact is not None or not body.startswith(NO_MATCH_PREFIX):
        return body, artifact

    broadened = {key: value for key, value in kwargs.items() if key != "state"}
    if broadened == kwargs:
        return body, None
    wider, wider_artifact = search_leads(**broadened, for_chat=False, export="excel")
    wider_body = for_human(wider)
    if wider_artifact is None or wider_body.startswith(NO_MATCH_PREFIX):
        return body, None
    where = str(kwargs.get("state", "")).upper()
    scope = f" in {where}" if where else ""
    return (
        f"No exact matches{scope} for those filters. I attached the closest "
        f"results I have instead.\n\n{wider_body}",
        wider_artifact,
    )
