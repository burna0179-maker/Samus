"""Canonical call-outcome taxonomy — shared by every Samus calling path.

Samus records calls from two places: the operator working the morning list by
hand (``backend.crm.log_call`` <- ``scripts/Log-Call.ps1``) and the automated
Vapi agent "Morgan" (``backend.voice.service`` end-of-call webhook). Both must
speak the *same* outcome vocabulary — otherwise a deal booked on one path is
invisible to a brief query written for the other, and the CallState retry
machine sees two different languages.

This module is that single source of truth:

  * :data:`OUTCOME_TO_STATE` — every valid call outcome mapped to the
    :class:`~backend.crm.models.CallState` ``state`` it advances the prospect
    to.
  * :data:`VALID_OUTCOMES` — the outcomes tuple (CLI ``choices``, validation).
  * :func:`state_for_outcome` / :func:`is_valid_outcome` — the lookups.

Most connected-call outcomes (booked / follow_up / disqualified /
not_interested / hung_up) conclude the call and map to ``completed`` — the
call happened; the nuance rides in ``Conversation.outcome`` /
``CallState.last_outcome``. The exception is ``gatekeeper``: the call did NOT
conclude — the decision-maker was never reached — so it maps to its own
non-terminal state and the prospect stays in the retry pool. ``no_answer`` /
``voicemail`` / ``do_not_call`` carry their own mechanical states.

Why the granularity (added 2026-05-21 from real operator call data):
  - gatekeeper     vs follow_up    — follow_up is a warm decision-maker;
                                     gatekeeper never reached the DM and must
                                     be re-dialled.
  - not_interested vs disqualified — disqualified is structural ("not a fit");
                                     not_interested is a soft no, re-approachable.
  - hung_up        vs no_answer    — no_answer is a ring-out; hung_up is a
                                     pickup-then-rejection (a contact, not a
                                     miss) and must not feed no-answer retry.

History: this map lived privately inside ``log_call.py`` — operator-only by
construction — and was promoted here 2026-05-21 so the Vapi agent path records
into the same taxonomy.
"""
from __future__ import annotations

# Call outcome -> CallState.state. The state values are the
# ``CallStateValue`` literal in backend/crm/models.py. Insertion order is
# meaningful: it is the order the outcomes are offered as CLI choices.
OUTCOME_TO_STATE: dict[str, str] = {
    "booked": "completed",
    "follow_up": "completed",
    "disqualified": "completed",
    "gatekeeper": "gatekeeper",
    "not_interested": "completed",
    "hung_up": "completed",
    "no_answer": "no_answer",
    "voicemail": "voicemail",
    "do_not_call": "do_not_call",
    # Operator engaged with the prospect (read research, typed notes from
    # the call card) but did not explicitly classify the call. Treated as
    # ``completed`` for retry semantics — the prospect doesn't go back into
    # the no_answer / voicemail retry pools. Added 2026-06-22 to back the
    # forge-ui cc-notes "notes-only counts as a call" surface so operator
    # research effort actually de-prioritises the prospect on tomorrow's
    # morning call list.
    "noted": "completed",
}

VALID_OUTCOMES: tuple[str, ...] = tuple(OUTCOME_TO_STATE)

# CallState.state for a connected call whose outcome we don't recognise. The
# call still happened, so it concluded — "completed" — and any nuance rides in
# CallState.last_outcome, mirroring the booked / follow_up precedent.
_DEFAULT_STATE = "completed"


def is_valid_outcome(outcome: str) -> bool:
    """True iff ``outcome`` is a recognised call outcome."""
    return outcome in OUTCOME_TO_STATE


def state_for_outcome(outcome: str) -> str:
    """Return the ``CallState.state`` an outcome advances a prospect to.

    An empty or unrecognised outcome resolves to ``completed`` — a call that
    happened with no clearer signal still concluded.
    """
    return OUTCOME_TO_STATE.get(outcome, _DEFAULT_STATE)
