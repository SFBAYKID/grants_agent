"""What Grant remembers about a colleague between conversations.

Chase: "It has memory, it remembers, and it follows up. It should be able to say to a
user, 'Hey, I am following up on something from a few days ago.'… personal things, if
a user shares personal information, that should absolutely be captured. You are
trying to almost befriend the user and build a relationship with them."

Grant currently forgets everything when a thread ends. A rep who says "I only cover
Texas" says it again next week, and one who mentions their kid's game gets a blank
stare. That is the difference between an assistant and a search box.

THE RULE THAT MAKES A MEMORY ABOUT A PERSON SAFE. Every row carries the colleague's
OWN words, verbatim, and `remember` refuses without them — the quote must appear
literally in something they typed. A "fact" about a named colleague that nobody can
trace to something they said is not a memory, it is Grant inventing a claim about a
person, which is rule 1 aimed at the team instead of at a lead. Same mechanical check
as `thread_scanner`, for the same reason: an LLM asked to summarise what someone said
will happily produce something tidier and slightly false.

SIX MONTHS, AND IT EXPIRES WITHOUT ANYONE REMEMBERING TO PURGE. Chase set the
horizon. `expires_at` is stamped at insert and every read filters on it, so a memory
is invisible the day it lapses whether or not the purge job ever runs. `purge` only
reclaims space; it is not what enforces the policy.

CORRECTION BEATS ACCUMULATION. People change territory and job and mind. `supersede`
retires the old row rather than letting Grant hold two contradictory beliefs and pick
one by row order — the same defect that once let a rep see a Teacher instead of an
Assistant Superintendent.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Chase's number. Long enough to feel like a colleague who pays attention, short
# enough that Grant is not keeping a dossier.
RETENTION = timedelta(days=182)

# What a memory may be about. Deliberately small and human — these are the things a
# colleague would actually recall, not a CRM schema.
KINDS = ("personal", "preference", "territory", "context")

# Below this a "quote" is too short to prove anything; "ok" appears in every thread.
MIN_EVIDENCE = 8


@dataclass(frozen=True)
class Memory:
    """One thing Grant knows about a colleague, and the words that justify it."""

    memory_id: int
    slack_user: str
    kind: str
    fact: str
    evidence: str
    recorded_at: datetime


def _now() -> datetime:
    """Current UTC moment."""
    return datetime.now(timezone.utc)


def _norm(text: str) -> str:
    """Collapse whitespace so a quote survives Slack's wrapping."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


# Words that reverse the clause that follows them. A substring check cannot see
# them, which is how "I don't want you to email me" yields the quote "want you to
# email me" — copied character-for-character, and the opposite of what she said.
NEGATORS = frozenset(
    {
        "not",
        "no",
        "never",
        "dont",
        "don't",
        "doesnt",
        "doesn't",
        "didnt",
        "didn't",
        "cant",
        "can't",
        "cannot",
        "wont",
        "won't",
        "wouldnt",
        "wouldn't",
        "shouldnt",
        "shouldn't",
        "isnt",
        "isn't",
        "arent",
        "aren't",
        "stop",
        "avoid",
        "without",
        "unless",
        "rather",
    }
)

# How far back to look for a negator. Far enough to catch "I would never say we
# should drop Texas", short enough not to trip on an unrelated earlier clause.
NEGATION_WINDOW = 4


def _words(text: str) -> list[str]:
    """Lowercased word tokens, punctuation stripped."""
    return re.findall(r"[a-z0-9']+", _norm(text))


def inverts_meaning(evidence: str, said: str) -> bool:
    """Does the source negate the clause this quote was cut from?

    THE FAILURE A SUBSTRING CHECK CANNOT SEE. Every one of these passed the literal
    check while meaning the opposite of what the person said:

        "I don't want you to email me the weekly list" -> "want you to email me…"
        "not interested in cameras for Fairfax"        -> "interested in cameras…"
        "I would never say we should drop Texas"       -> "we should drop Texas"

    The model is not disobeying its instructions in any of those — a clause really
    was copied verbatim. The guard was simply measuring the wrong thing.
    """
    source = _words(said)
    quote = _words(evidence)
    if not source or not quote:
        return False
    for start in range(len(source) - len(quote) + 1):
        if source[start : start + len(quote)] != quote:
            continue
        window = source[max(0, start - NEGATION_WINDOW) : start]
        if any(word in NEGATORS for word in window):
            return True
    return False


def evidence_supports(evidence: str, said: str) -> bool:
    """Is this evidence literally something the person typed, and not its opposite?

    The guard that separates remembering from inventing. A paraphrase that means the
    same thing is not acceptable: Grant will later say "you mentioned" and attach the
    words to a named colleague, possibly months later, possibly in front of others.

    Nor is a true substring that reverses their meaning, which is the harder case and
    the one a literal check was blind to.
    """
    needle = _norm(evidence)
    if len(needle) < MIN_EVIDENCE:
        return False
    if needle not in _norm(said):
        return False
    return not inverts_meaning(evidence, said)


def fact_is_grounded(fact: str, evidence: str) -> bool:
    """Does the quote actually support the claim being stored beside it?

    `evidence` was validated against the message and `fact` against nothing at all —
    yet `fact` is what reaches the model and what Grant acts on, while the quote sits
    beside it as decoration. A 200-character message about a lead would admit
    fact="Is leaving the company in September" on the quote "about a lead".

    So the fact's content words have to appear in the quote. This deliberately allows
    ordinary rewording — "Covers Texas and Oklahoma" from "I only cover Texas and
    Oklahoma" — and refuses a claim the quote says nothing about.
    """
    stop = {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "to",
        "of",
        "in",
        "on",
        "for",
        "and",
        "or",
        "at",
        "by",
        "with",
        "their",
        "they",
        "them",
        "he",
        "she",
        "his",
        "her",
        "it",
        "its",
        "has",
        "have",
        "had",
        "only",
        "i",
    }
    claim = [w for w in _words(fact) if w not in stop and len(w) > 2]
    if not claim:
        return False
    quoted = {q.rstrip("s") for q in _words(evidence)}
    covered = sum(1 for word in claim if word.rstrip("s") in quoted)
    if not covered:
        return False
    # TWO WEAK SIGNALS RATHER THAN ONE STRICT ONE, because a strict overlap ratio
    # rejects ordinary rewording. "Also covers Louisiana" is a fair reading of "I
    # picked up Louisiana last month" and shares exactly one content word; demanding
    # more would force the fact to parrot the quote, which defeats the point of
    # storing a fact at all.
    #
    # What must not pass is a claim the quote says nothing about — the 200-character
    # message that admitted fact="Is leaving the company in September" on the quote
    # "about a lead". That fails BOTH: no shared content word, and a quote far
    # shorter than the claim it supposedly supports.
    if len(_norm(evidence)) < len(_norm(fact)) * 0.5:
        return False
    return covered / len(claim) >= 0.33


def _remember_in_transaction(
    conn: sqlite3.Connection,
    *,
    slack_user: str,
    kind: str,
    fact: str,
    evidence: str,
    said: str,
    audience: str = "",
    thread_ts: str = "",
    message_ts: str = "",
    now: datetime | None = None,
) -> int | None:
    """Validate and store one memory without committing the caller's transaction.

    `said` is the full message the person actually wrote; `evidence` must appear
    inside it verbatim. Passing them separately is what lets the check be mechanical
    rather than a promise in a prompt.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if not slack_user:
        raise ValueError("a memory needs the person it is about")
    if not fact.strip():
        raise ValueError("a memory needs something to remember")
    if not evidence_supports(evidence, said):
        raise ValueError(
            "a memory must quote the person verbatim and must not invert their "
            "meaning; this evidence does not appear in what they said, or the "
            "source negates it"
        )
    if not fact_is_grounded(fact, evidence):
        raise ValueError(
            "the quote does not support the fact being stored beside it; "
            "quote the sentence the claim actually came from"
        )
    moment = now or _now()
    try:
        cur = conn.execute(
            """INSERT INTO user_memory
                 (slack_user,kind,fact,evidence,audience,thread_ts,message_ts,
                  recorded_at,expires_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                slack_user,
                kind,
                fact.strip(),
                evidence.strip(),
                audience,
                thread_ts,
                message_ts,
                moment.isoformat(),
                (moment + RETENTION).isoformat(),
            ),
        )
    except sqlite3.IntegrityError:
        # Already on file — but "already" may mean an EXPIRED row. `UNIQUE(slack_user,
        # kind, fact)` outlives the retention horizon, so a lapsed memory was
        # invisible to `recall` AND could never be learned again: permanent amnesia
        # about a thing the person had just repeated. The docstring's claim that a
        # purge which never runs "costs disk, not correctness" was wrong.
        #
        # Re-arm the existing row instead. Same fact, same person, said again — that
        # is the row being true once more, not a second memory.
        existing = conn.execute(
            "SELECT id FROM user_memory WHERE slack_user=? AND kind=? AND fact=?",
            (slack_user, kind, fact.strip()),
        ).fetchone()
        if existing is None:
            # Do not relabel an unrelated trigger/CHECK/FK failure as a duplicate.
            raise
        existing_id = int(existing[0])
        cur = conn.execute(
            """UPDATE user_memory
                  SET recorded_at=?, expires_at=?, evidence=?, superseded_by=NULL
                WHERE id=? AND expires_at<=?""",
            (
                moment.isoformat(),
                (moment + RETENTION).isoformat(),
                evidence.strip(),
                existing_id,
                moment.isoformat(),
            ),
        )
        if cur.rowcount == 0:
            return None
        return existing_id
    return int(cur.lastrowid or 0)


def remember(
    conn: sqlite3.Connection,
    *,
    slack_user: str,
    kind: str,
    fact: str,
    evidence: str,
    said: str,
    audience: str = "",
    thread_ts: str = "",
    message_ts: str = "",
    now: datetime | None = None,
) -> int | None:
    """Record one thing about a colleague. Returns None if already known."""
    try:
        stored = _remember_in_transaction(
            conn,
            slack_user=slack_user,
            kind=kind,
            fact=fact,
            evidence=evidence,
            said=said,
            audience=audience,
            thread_ts=thread_ts,
            message_ts=message_ts,
            now=now,
        )
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return stored


def recall(
    conn: sqlite3.Connection, slack_user: str, *, now: datetime | None = None
) -> list[Memory]:
    """Everything Grant currently knows about one colleague, freshest first.

    Expiry and supersession are applied HERE rather than by a cleanup job, so a
    lapsed or corrected memory cannot reach a conversation even if no purge has run.
    """
    moment = (now or _now()).isoformat()
    rows = conn.execute(
        """SELECT id,slack_user,kind,fact,evidence,recorded_at
             FROM user_memory
            WHERE slack_user=?
              AND expires_at>?
              AND superseded_by IS NULL
            ORDER BY recorded_at DESC
            LIMIT 40""",
        (slack_user, moment),
    ).fetchall()
    out: list[Memory] = []
    for row in rows:
        recorded = datetime.fromisoformat(str(row["recorded_at"]))
        out.append(
            Memory(
                memory_id=int(row["id"]),
                slack_user=str(row["slack_user"]),
                kind=str(row["kind"]),
                fact=str(row["fact"]),
                evidence=str(row["evidence"]),
                recorded_at=recorded,
            )
        )
    return out


def _stored_timestamp(value: object) -> datetime | None:
    """Parse one stored timestamp as aware UTC, returning None for unsafe data."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _explicit_correction(evidence: str, new_fact: str, old_fact: str) -> bool:
    """Require one narrow correction clause tied to both old and new facts.

    The model may classify relationships, but it may not retire a colleague's prior
    words merely by saying ``action=replace``. Automatic replacement is allowed only
    for a single short clause whose complete-state ``only ... now`` wording supports
    the new fact and which names at least one distinctive content token from the old
    fact. Everything else remains additive or awaits an operator.
    """
    normalized = _norm(evidence).removesuffix(".")
    if len(normalized) > 180:
        return False
    match = re.fullmatch(
        r"i (?:(?P<now_prefix>now) )?only "
        r"(?P<current>[a-z0-9][a-z0-9' -]{1,80}), not "
        r"(?P<previous>[a-z0-9][a-z0-9' -]{1,80})"
        r"(?:, (?P<now_suffix>now))?",
        normalized,
    )
    if match is None or bool(match["now_prefix"]) == bool(match["now_suffix"]):
        return False
    current = str(match["current"])
    previous = str(match["previous"])
    current_words = _words(current)
    previous_words = _words(previous)
    if any(word in NEGATORS for word in (*current_words, *previous_words)):
        return False
    new_words = _words(new_fact)
    if not current_words or not new_words:
        return False

    def predicate(word: str) -> str:
        """Normalize the direct first-person/third-person predicate minimally."""
        irregular = {"has": "have", "is": "be", "does": "do"}
        normalized_word = irregular.get(word, word)
        return (
            normalized_word[:-1] if normalized_word.endswith("s") else normalized_word
        )

    current_predicate = predicate(current_words[0])
    if current_predicate != predicate(new_words[0]):
        return False
    new_state = [word for word in new_words[1:] if word != "only"]
    current_state = [word for word in current_words[1:] if word != "only"]
    if not new_state or current_state != new_state:
        return False
    old_words = _words(old_fact)
    if not old_words or predicate(old_words[0]) != current_predicate:
        return False
    ignored = {"and", "or", "only"}
    old_state = [word for word in old_words[1:] if word not in ignored]
    previous_state = list(previous_words)
    if previous_state and predicate(previous_state[0]) == current_predicate:
        previous_state = previous_state[1:]
    previous_state = [word for word in previous_state if word not in ignored]
    return bool(old_state) and previous_state == old_state


def _supersede_in_transaction(
    conn: sqlite3.Connection,
    old_id: int,
    new_id: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Retire one active memory with a newer peer, without committing.

    IDs originate in model output during automatic capture, so row identity alone is
    never authority. Both rows must still be active, unsuperseded, about the same
    Slack user and kind, and ordered forward in time. Those checks also make cycles
    impossible without guessing whether two same-kind facts contradict each other.
    """
    if old_id == new_id:
        raise ValueError("a memory cannot supersede itself")
    rows = conn.execute(
        """SELECT id,slack_user,kind,fact,evidence,recorded_at,expires_at,superseded_by
             FROM user_memory WHERE id IN (?,?) ORDER BY id""",
        (old_id, new_id),
    ).fetchall()
    by_id = {int(row[0]): row for row in rows}
    old = by_id.get(old_id)
    new = by_id.get(new_id)
    if old is None or new is None:
        return False
    moment = (now or _now()).astimezone(timezone.utc)
    old_recorded = _stored_timestamp(old[5])
    new_recorded = _stored_timestamp(new[5])
    old_expires = _stored_timestamp(old[6])
    new_expires = _stored_timestamp(new[6])
    if (
        old[1] != new[1]
        or old[2] != new[2]
        or not _explicit_correction(str(new[4]), str(new[3]), str(old[3]))
        or old[7] is not None
        or new[7] is not None
        or old_recorded is None
        or new_recorded is None
        or old_expires is None
        or new_expires is None
        or old_expires <= moment
        or new_expires <= moment
        or old_recorded > moment
        or new_recorded > moment
        or new_recorded < old_recorded
    ):
        return False
    active_same_kind = conn.execute(
        """SELECT COUNT(*) FROM user_memory
             WHERE slack_user=? AND kind=? AND superseded_by IS NULL
               AND expires_at>?""",
        (old[1], old[2], moment.isoformat()),
    ).fetchone()
    if active_same_kind is None or int(active_same_kind[0]) != 2:
        # With more than one prior fact of this kind, an ID chosen by a model or
        # caller cannot prove which independent fact the correction replaced.
        return False
    cur = conn.execute(
        """UPDATE user_memory SET superseded_by=?
             WHERE id=? AND superseded_by IS NULL AND expires_at>?""",
        (new_id, old_id, moment.isoformat()),
    )
    return cur.rowcount > 0


def supersede(
    conn: sqlite3.Connection,
    old_id: int,
    new_id: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Safely retire an active memory with a newer same-user, same-kind row."""
    try:
        applied = _supersede_in_transaction(conn, old_id, new_id, now=now)
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return applied


def forget(conn: sqlite3.Connection, slack_user: str) -> int:
    """Delete everything Grant knows about one colleague. Returns how many rows.

    Someone who asks Grant to forget them means it, and the answer has to be deletion
    rather than a flag — "I've stopped remembering" must be true, not a filter.
    """
    cur = conn.execute("DELETE FROM user_memory WHERE slack_user=?", (slack_user,))
    conn.commit()
    return int(cur.rowcount)


def purge(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """Delete memories past the retention horizon. Returns how many were removed.

    Housekeeping only. `recall` already hides expired rows, so a purge that never
    runs costs disk, not correctness.
    """
    cur = conn.execute(
        "DELETE FROM user_memory WHERE expires_at<=?", ((now or _now()).isoformat(),)
    )
    conn.commit()
    return int(cur.rowcount)


def as_prompt_context(memories: list[Memory]) -> str:
    """Render what Grant knows for the system prompt, or "" if it knows nothing.

    Says "they mentioned" rather than stating facts flatly, because every line here
    came from one thing a person said once and may since have changed. Grant should
    sound like a colleague who remembers, not a database reciting a record.
    """
    if not memories:
        return ""
    lines = [f"- {m.fact} (they mentioned: “{m.evidence}”)" for m in memories[:12]]
    return (
        "Things this person has told you before. Use them naturally, the way a "
        "colleague would; never recite them back as a list, and never treat one as "
        "still true if they say otherwise now:\n" + "\n".join(lines)
    )


# Below this a message is a "yes"/"ok"/"thanks" and contains nothing durable. The
# gate is also a cost control: it skips the extra model call on the majority of
# messages, which are short.
MIN_WORTH_READING = 60
MAX_CAPTURE_CANDIDATES = 20

CAPTURE_PROMPT = """A colleague just wrote this to Grant, an assistant that finds \
government grant leads for a security-camera reseller.

Pull out ONLY facts worth remembering months from now — the things a good colleague \
would still know next quarter. Their territory, how they like to work, a personal \
detail they volunteered, a standing constraint.

Ignore anything about the current request: what they want done right now, which \
lead, which state they are asking about today. That is the conversation, not memory.

Return JSON only: {{"facts": [{{"action": "add", "replaces_memory_id": null, \
"fact": "...", "kind": "...", "quote": "..."}}]}}

- "quote" MUST be copied character-for-character from their message. Do not tidy it, \
correct spelling, or merge sentences. If you cannot copy it exactly, leave it out.
- "kind" is one of: personal, preference, territory, context.
- "fact" is one short phrase in the third person, e.g. "Covers Texas and Oklahoma".
- Use action "replace" only when their exact quote is one complete clause in this \
form: "I [now] only <new state>, not <old state>, [now]" (use "now" exactly once). \
It must directly state the new fact and name something from the old memory. Set \
replaces_memory_id to that exact integer. For example, "I only cover California, not \
Texas or Oklahoma, now" may replace an old Texas/Oklahoma territory. Every other \
wording, including "I picked up Louisiana", is an addition or no memory change.
- Use action "add" for every ordinary independent fact and set replaces_memory_id \
to null. Same-kind facts are not automatically contradictions.
- Most messages contain nothing worth keeping. Returning {{"facts": []}} is the normal \
and correct answer.

Active memories for this colleague only (JSON):
{active_memories}

Their message:
{said}
"""


def _capture_prompt(said: str, memories: list[Memory]) -> str:
    """Render a bounded, user-scoped correction choice for the capture model."""
    candidates = [
        {
            "memory_id": memory.memory_id,
            "kind": memory.kind,
            "fact": memory.fact,
            "evidence": memory.evidence,
        }
        for memory in memories[:MAX_CAPTURE_CANDIDATES]
    ]
    return CAPTURE_PROMPT.format(
        active_memories=json.dumps(
            candidates, ensure_ascii=True, separators=(",", ":")
        ),
        said=said,
    )


class _InvalidReplacement(ValueError):
    """A model-proposed correction failed mechanical relationship checks."""


def _rollback_capture_item(conn: sqlite3.Connection) -> None:
    """Roll back one capture item without discarding earlier accepted facts."""
    try:
        conn.execute("ROLLBACK TO user_memory_capture_item")
        conn.execute("RELEASE user_memory_capture_item")
    except sqlite3.DatabaseError:
        # If SQLite invalidated the savepoint, a full rollback is the only honest
        # state. Earlier items were committed after their own release.
        conn.rollback()


def capture(
    conn: sqlite3.Connection,
    *,
    slack_user: str,
    said: str,
    ask_model: Callable[[str], object],
    audience: str = "",
    thread_ts: str = "",
    message_ts: str = "",
    now: datetime | None = None,
) -> int:
    """Notice anything durable in what someone just said. Returns how many were kept.

    This is the half that makes memory automatic rather than a table somebody fills
    in by hand — the same gap Chase found in the follow-up system, which had been fed
    by a JSON file I wrote after reading transcripts by eye.

    THE MODEL CHOOSES WHAT IS INTERESTING AND NEVER AUTHORS THE EVIDENCE. Every quote
    it returns is checked against the real message by `remember`, which raises if it
    does not appear verbatim; that raise is caught here and counted as a discard.
    Nothing reaches storage on the model's word alone.

    Silent by design: this runs after a reply has already been sent, so a failure here
    must never surface to the person or break the turn.
    """
    import re as _re

    if not slack_user or len(said.strip()) < MIN_WORTH_READING:
        return 0
    moment = now or _now()
    active = recall(conn, slack_user, now=moment)[:MAX_CAPTURE_CANDIDATES]
    offered = {memory.memory_id: memory for memory in active}
    try:
        raw = ask_model(_capture_prompt(said, active))
    except Exception:  # noqa: BLE001 — capture is an enhancement, never a dependency
        return 0
    match = _re.search(r"\{.*\}", str(raw or ""), _re.DOTALL)
    if not match:
        return 0
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return 0
    if not isinstance(parsed, dict):
        return 0
    facts = parsed.get("facts", [])
    if not isinstance(facts, list):
        return 0

    kept = 0
    for item in facts:
        if not isinstance(item, dict):
            continue
        action_value = item.get("action")
        replacement_value = item.get("replaces_memory_id")
        # Legacy model/test output without a relation is safely additive only when it
        # carries no replacement target. Any ambiguous correction is discarded.
        action = "add" if action_value is None else str(action_value).strip().lower()
        if action not in {"add", "replace"}:
            continue
        if action == "add" and replacement_value is not None:
            continue
        replacement_id: int | None = None
        kind = str(item.get("kind") or "context").strip().lower()
        if action == "replace":
            if not isinstance(replacement_value, int) or isinstance(
                replacement_value, bool
            ):
                continue
            replacement_id = replacement_value
            candidate = offered.get(replacement_id)
            same_kind_candidates = [memory for memory in active if memory.kind == kind]
            if (
                candidate is None
                or candidate.kind != kind
                or len(same_kind_candidates) != 1
                or not _explicit_correction(
                    str(item.get("quote") or ""),
                    str(item.get("fact") or ""),
                    candidate.fact,
                )
            ):
                continue
        try:
            conn.execute("SAVEPOINT user_memory_capture_item")
            stored = _remember_in_transaction(
                conn,
                slack_user=slack_user,
                kind=kind,
                fact=str(item.get("fact") or ""),
                evidence=str(item.get("quote") or ""),
                said=said,
                audience=audience,
                thread_ts=thread_ts,
                message_ts=message_ts,
                now=moment,
            )
            if stored is None:
                conn.execute("RELEASE user_memory_capture_item")
                conn.commit()
                continue
            if replacement_id is not None and not _supersede_in_transaction(
                conn, replacement_id, stored, now=moment
            ):
                raise _InvalidReplacement("replacement target is no longer eligible")
            conn.execute("RELEASE user_memory_capture_item")
            conn.commit()
        except (ValueError, sqlite3.DatabaseError):
            _rollback_capture_item(conn)
            continue  # unverifiable quote, unknown kind, or empty fact — dropped
        kept += 1
    return kept
