"""Post-batch call auditor — turn the MANUAL per-round call audit into a
PERMANENT, self-improving Samus capability.

Every dial batch has been audited BY HAND each round: pull the batch's Vapi
calls, read transcripts, bucket outcomes, catch the P0 reasoning-leak, catch
the gatekeeper mis-targeting, measure time-to-hangup, and compare to the prior
batch. This module makes that a SYSTEM CAPABILITY that runs after every batch,
compounds knowledge across batches, and surfaces the next thing to fix.

It DOES NOT duplicate the existing learning loop — it BUILDS ON it:

  * outcome bucketing reuses the exact ``endedReason`` regex families that
    ``scripts/Watch-DialRun.ps1`` already streams live;
  * gatekeeper detection reuses :mod:`backend.voice.gatekeeper_detector`
    signal definitions (``LIVE_HUMAN_HINTS`` / ``_parse_turns``);
  * the reasoning-leak detector reuses the EXACT banned-phrase list from the
    P0 prompt-hardening test (``tests.test_voice_morgan_prompt_hardening
    .BANNED_SELF_NARRATION``) so it is a true REGRESSION GUARD — the same list
    the prompt bans is the list we assert never leaks;
  * recurring failure phrases are SURFACED (append-only) for the existing
    ``callsheet_updater`` feedback loop — never overwriting its intel.

Read-only Vapi: only ``GET /call`` is used (via
:meth:`backend.voice.client.VapiClient.list_calls`). No call is placed,
modified, or ended.

Durable cross-batch store: each batch's metrics append to
``<artifact_root>/voice/batch_analyses.jsonl`` so "is this cycle measurably
better than the last?" is answerable from disk, and the next-remediation
signal is computed from the trend.

CLI::

    python -m backend.voice.call_batch_analyzer [--since ISO] [--dial-run PATH]

PowerShell wrapper: ``scripts/Analyze-DialBatch.ps1`` (DPAPI VapiApiKey pull).
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import statistics
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.common import storage
from backend.common.dates import iso_now

from . import gatekeeper_detector as gk

_LOG = logging.getLogger("samus.voice.call_batch_analyzer")

# Durable cross-batch trend store — one JSON object per line, append-only.
_BATCH_STORE_FILE = "voice/batch_analyses.jsonl"

# How many Vapi calls to pull when no dial-run is supplied (list_calls clamps
# to [1, 100]). A batch is ~15 calls; 100 gives generous headroom.
_VAPI_LIST_LIMIT = 100

# ---------------------------------------------------------------------------
# Outcome bucketing — reuse the exact families Watch-DialRun.ps1 streams live.
# Watch-DialRun.ps1 switch:
#   'voicemail'                -> voicemail
#   'customer-ended'           -> live_hangup
#   'assistant-ended|silence'  -> engaged
#   'did-not-answer|no-answer' -> no_answer
# We extend it with two audit-specific buckets the manual audit tracks:
#   gatekeeper (detected human after a machine greeting), and machine_menu
#   (an IVR / press-1 menu Vapi's AMD missed) — both derived from the
#   transcript, not the ended-reason.
# ---------------------------------------------------------------------------
_OUTCOME_ORDER: tuple[str, ...] = (
    "engaged",
    "gatekeeper",
    "live_hangup",
    "machine_menu",
    "voicemail",
    "no_answer",
    "other",
)


def _bucket_from_ended_reason(ended_reason: str) -> str:
    """Map a Vapi ``endedReason`` to a coarse outcome bucket.

    Mirrors the ``switch -Regex`` in scripts/Watch-DialRun.ps1 so the offline
    audit and the live monitor agree. Order matters: 'voicemail' is checked
    before the machine/no-answer families.
    """
    r = (ended_reason or "").lower()
    if not r:
        return "other"
    if "voicemail" in r:
        return "voicemail"
    # no-answer family (incl. 'customer-did-not-answer') — checked before the
    # 'customer-ended' hangup so the former's 'customer-' prefix can't be
    # mistaken for a live hangup.
    if "did-not-answer" in r or "no-answer" in r or "no_answer" in r:
        return "no_answer"
    # A LIVE human who ended the call — the opener metric's population.
    if "customer-ended" in r:
        return "live_hangup"
    # The assistant carried the call to its own end, or the call went silent
    # after connecting — treat as an engaged conversation.
    if "assistant-ended" in r or "silence" in r:
        return "engaged"
    return "other"


# A call that Vapi bucketed as machine-ish (voicemail / no-answer) but whose
# transcript reveals a live human is the classic gatekeeper reclassification
# (Gap-19). We ask gatekeeper_detector, exactly as backend.voice.reconcile does.
_MACHINE_MENU_HINTS: tuple[str, ...] = (
    "press 1",
    "press one",
    "press 2",
    "press two",
    "menu options",
    "listen carefully as our menu",
    "para espanol",
    "para español",
    "for english",
)


# ---------------------------------------------------------------------------
# Reasoning-leak regression guard — reuse the EXACT P0 banned-phrase list.
# ---------------------------------------------------------------------------
def _load_banned_self_narration() -> tuple[str, ...]:
    """Return the canonical banned self-narration phrase list.

    Single source of truth: ``tests.test_voice_morgan_prompt_hardening
    .BANNED_SELF_NARRATION`` — the same list the prompt-hardening guard asserts
    the prompt bans. Importing it here (rather than re-typing it) is what makes
    this a true REGRESSION GUARD: add a phrase to the prompt ban list and the
    batch analyzer starts flagging it automatically, with zero drift.

    Falls back to an empty tuple only if the test module can't be imported
    (e.g. tests dir stripped from a prod image) — fail-soft, never raises.
    """
    try:
        from tests.test_voice_morgan_prompt_hardening import (  # noqa: PLC0415
            BANNED_SELF_NARRATION,
        )
        return tuple(BANNED_SELF_NARRATION)
    except Exception as exc:  # noqa: BLE001 — fail-soft
        _LOG.warning("could not import BANNED_SELF_NARRATION: %s", exc)
        return ()


def detect_reasoning_leak(
    transcript: str,
    *,
    banned: tuple[str, ...] | None = None,
) -> list[str]:
    """Scan Morgan's (AI) turns for banned self-narration / meta-reasoning.

    Returns the list of banned phrases that leaked (empty == clean). Only AI /
    assistant turns are scanned — a PROSPECT saying "I need to think about it"
    is not a leak; Morgan narrating "I need to check my protocol" is. Uses the
    same turn parser as gatekeeper_detector so AI/User attribution is
    consistent. Fail-soft: never raises.
    """
    if banned is None:
        banned = _load_banned_self_narration()
    if not banned:
        return []
    try:
        turns = gk._parse_turns(transcript or "")
    except Exception:  # noqa: BLE001
        return []
    ai_text = " \n ".join(t for spk, t in turns if spk == "ai").lower()
    if not ai_text.strip():
        return []
    hits: list[str] = []
    for phrase in banned:
        # Word-boundary-ish match so "I should" doesn't fire inside "I shouldn't"
        # is NOT desired here — the ban is on the substring as the prompt states
        # it. But guard the very short phrases ("I need to", "I should") against
        # matching mid-word by requiring a boundary on each side.
        low = phrase.lower()
        if _phrase_present(ai_text, low):
            hits.append(phrase)
    return hits


def _phrase_present(haystack: str, needle: str) -> bool:
    """Boundary-aware substring test. Short phrases must not match mid-word."""
    if not needle:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])"
    return re.search(pattern, haystack) is not None


# ---------------------------------------------------------------------------
# Gatekeeper-aware-opener regression guard: did Morgan LEAK the specific
# withheld finding to a gatekeeper? The round-2 P0 fix withholds
# {{callsheet_finding}} until the owner is confirmed. If the finding text shows
# up in an AI turn AND the call reads as a gatekeeper (no owner confirmed),
# that's the regression.
# ---------------------------------------------------------------------------
def detect_finding_leaked_to_gatekeeper(
    transcript: str,
    finding: str,
    *,
    was_gatekeeper: bool,
) -> bool:
    """True iff Morgan spoke the withheld ``finding`` while on with a gatekeeper.

    Conservative: requires (a) a non-trivial finding string, (b) the call
    classified as a gatekeeper interaction, and (c) a meaningful chunk of the
    finding appearing in an AI turn. Fail-soft: never raises; returns False on
    any error or when the finding is empty.
    """
    try:
        f = (finding or "").strip()
        if len(f) < 12 or not was_gatekeeper:
            return False
        turns = gk._parse_turns(transcript or "")
        ai_text = " ".join(t for spk, t in turns if spk == "ai").lower()
        if not ai_text:
            return False
        # Match on the most-distinctive slice of the finding to avoid a false
        # positive from a common word. Use the longest word run available.
        needle = _distinctive_slice(f).lower()
        return bool(needle) and needle in ai_text
    except Exception:  # noqa: BLE001
        return False


def _distinctive_slice(text: str, *, min_len: int = 12) -> str:
    """Return a distinctive substring of ``text`` for leak-matching.

    Prefers the first clause up to ~60 chars; strips leading filler. Keeps it
    long enough that an incidental collision with a common phrase is unlikely.
    """
    clause = re.split(r"[.!?;\n]", text.strip(), maxsplit=1)[0].strip()
    if len(clause) >= min_len:
        return clause[:60]
    return text.strip()[:60]


# ---------------------------------------------------------------------------
# Per-call analysis
# ---------------------------------------------------------------------------
@dataclass
class CallAudit:
    """The per-call audit row — everything the manual audit computed by hand."""

    call_id: str
    outcome_bucket: str
    ended_reason: str
    reached_human: bool
    was_gatekeeper: bool
    reached_owner: bool
    time_to_hangup_s: float | None
    reasoning_leak_detected: bool
    reasoning_leak_phrases: list[str] = field(default_factory=list)
    finding_leaked_to_gatekeeper: bool = False
    cost: float = 0.0
    caller_number_id: str = ""
    company: str = ""


def _duration_s(started_at: str | None, ended_at: str | None) -> float | None:
    """Seconds between two ISO timestamps, or None. Mirrors Watch-DialRun Dur()."""
    if not started_at or not ended_at:
        return None
    try:
        s = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        e = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
        return round((e - s).total_seconds(), 1)
    except (ValueError, TypeError):
        return None


def analyze_call(
    call: Any,
    *,
    finding: str = "",
    banned: tuple[str, ...] | None = None,
) -> CallAudit:
    """Audit one Vapi call object (``VapiCall`` or a dict-like row).

    ``finding`` is the per-prospect withheld ``callsheet_finding`` (from the
    dial-run artifact / call metadata) used only for the finding-leak guard.
    Never raises — a malformed call yields a best-effort 'other' row.
    """
    def _g(name: str, default: Any = None) -> Any:
        if isinstance(call, dict):
            return call.get(name, default)
        return getattr(call, name, default)

    call_id = str(_g("id", "") or "")
    ended_reason = str(_g("endedReason", "") or "")
    transcript = str(_g("transcript", "") or "")
    started_at = _g("startedAt")
    ended_at = _g("endedAt")
    cost = _g("cost") or 0.0
    caller_number_id = str(_g("phoneNumberId", "") or "")
    customer = _g("customer")
    company = ""
    if isinstance(customer, dict):
        company = str(customer.get("name") or "")
    elif customer is not None:
        company = str(getattr(customer, "name", "") or "")

    bucket = _bucket_from_ended_reason(ended_reason)

    # Gatekeeper reclassification (Gap-19): ask the shared detector whether a
    # live human engaged after a machine/hold greeting. Only machine-ish
    # ended-reasons are eligible — exactly the detector's own contract.
    was_gatekeeper, _gk_reason = gk.detect_human_engagement(
        transcript, ended_reason=ended_reason,
    )

    # machine_menu: an IVR / press-key menu Vapi's AMD didn't flag as voicemail.
    low_tx = transcript.lower()
    is_machine_menu = any(h in low_tx for h in _MACHINE_MENU_HINTS)

    # reached_human: engaged conversation OR a detected gatekeeper.
    reached_human = bucket == "engaged" or was_gatekeeper
    # reached_owner: engaged AND not merely a gatekeeper handoff. Conservative —
    # a true "engaged" bucket means the assistant/customer conversed to the end.
    reached_owner = bucket == "engaged" and not was_gatekeeper

    # Refine the outcome bucket with transcript-derived signals.
    if was_gatekeeper:
        bucket = "gatekeeper"
    elif is_machine_menu and bucket in ("voicemail", "no_answer", "other"):
        bucket = "machine_menu"

    # time_to_hangup: the opener metric — only meaningful for a LIVE human who
    # ended the call (customer-ended). That's the "how fast did we lose them"
    # signal the manual audit tracks for opener quality.
    time_to_hangup: float | None = None
    if "customer-ended" in ended_reason.lower():
        time_to_hangup = _duration_s(started_at, ended_at)

    leak_phrases = detect_reasoning_leak(transcript, banned=banned)
    finding_leak = detect_finding_leaked_to_gatekeeper(
        transcript, finding, was_gatekeeper=was_gatekeeper,
    )

    return CallAudit(
        call_id=call_id,
        outcome_bucket=bucket,
        ended_reason=ended_reason,
        reached_human=reached_human,
        was_gatekeeper=was_gatekeeper,
        reached_owner=reached_owner,
        time_to_hangup_s=time_to_hangup,
        reasoning_leak_detected=bool(leak_phrases),
        reasoning_leak_phrases=leak_phrases,
        finding_leaked_to_gatekeeper=finding_leak,
        cost=round(float(cost or 0.0), 4),
        caller_number_id=caller_number_id,
        company=company,
    )


# ---------------------------------------------------------------------------
# Batch metrics
# ---------------------------------------------------------------------------
@dataclass
class BatchMetrics:
    """Aggregate metrics for one dial batch — the compounding trend record."""

    batch_id: str
    analyzed_ts: str
    total_calls: int
    outcome_distribution: dict[str, int] = field(default_factory=dict)
    engagement_rate: float = 0.0          # engaged / total
    live_contact_rate: float = 0.0        # reached_human / total
    gatekeeper_count: int = 0
    gatekeeper_handoff_success_rate: float = 0.0  # reached_owner / gatekeepers-seen
    median_time_to_hangup_s: float | None = None
    mean_time_to_hangup_s: float | None = None
    reasoning_leak_count: int = 0
    reasoning_leak_phrases: dict[str, int] = field(default_factory=dict)
    finding_leaked_to_gatekeeper_count: int = 0
    caller_id_distribution: dict[str, int] = field(default_factory=dict)
    caller_id_skew: float = 0.0           # 0 == perfectly even, 1 == all on one
    total_cost: float = 0.0


def aggregate_batch(
    audits: list[CallAudit],
    *,
    batch_id: str,
    analyzed_ts: str | None = None,
) -> BatchMetrics:
    """Roll per-call audits into one batch metric record."""
    ts = analyzed_ts or iso_now()
    total = len(audits)
    if total == 0:
        return BatchMetrics(batch_id=batch_id, analyzed_ts=ts, total_calls=0)

    dist: Counter[str] = Counter(a.outcome_bucket for a in audits)
    engaged = sum(1 for a in audits if a.outcome_bucket == "engaged")
    reached_human = sum(1 for a in audits if a.reached_human)

    gatekeepers = [a for a in audits if a.was_gatekeeper]
    gk_count = len(gatekeepers)
    gk_to_owner = sum(1 for a in gatekeepers if a.reached_owner)
    gk_success = round(gk_to_owner / gk_count, 3) if gk_count else 0.0

    hangups = [a.time_to_hangup_s for a in audits if a.time_to_hangup_s is not None]
    median_hangup = round(statistics.median(hangups), 1) if hangups else None
    mean_hangup = round(statistics.mean(hangups), 1) if hangups else None

    leak_phrase_counts: Counter[str] = Counter()
    for a in audits:
        for p in a.reasoning_leak_phrases:
            leak_phrase_counts[p] += 1
    leak_count = sum(1 for a in audits if a.reasoning_leak_detected)
    finding_leak_count = sum(1 for a in audits if a.finding_leaked_to_gatekeeper)

    caller_dist: Counter[str] = Counter(
        a.caller_number_id or "(unknown)" for a in audits
    )
    skew = _distribution_skew(caller_dist)

    total_cost = round(sum(a.cost for a in audits), 4)

    return BatchMetrics(
        batch_id=batch_id,
        analyzed_ts=ts,
        total_calls=total,
        outcome_distribution=dict(dist),
        engagement_rate=round(engaged / total, 3),
        live_contact_rate=round(reached_human / total, 3),
        gatekeeper_count=gk_count,
        gatekeeper_handoff_success_rate=gk_success,
        median_time_to_hangup_s=median_hangup,
        mean_time_to_hangup_s=mean_hangup,
        reasoning_leak_count=leak_count,
        reasoning_leak_phrases=dict(leak_phrase_counts),
        finding_leaked_to_gatekeeper_count=finding_leak_count,
        caller_id_distribution=dict(caller_dist),
        caller_id_skew=skew,
        total_cost=total_cost,
    )


def _distribution_skew(counts: Counter[str]) -> float:
    """Round-robin health: 0.0 == perfectly even across numbers, 1.0 == all on
    one number. For a single number the skew is 0 (nothing to balance).
    """
    n = len(counts)
    total = sum(counts.values())
    if n <= 1 or total == 0:
        return 0.0
    max_share = max(counts.values()) / total
    even_share = 1.0 / n
    # Normalize: max_share ranges [even_share, 1.0]; map to [0, 1].
    return round((max_share - even_share) / (1.0 - even_share), 3)


# ---------------------------------------------------------------------------
# Cross-batch trend — the compounding part.
# ---------------------------------------------------------------------------
@dataclass
class TrendDelta:
    metric: str
    previous: float | None
    current: float | None
    direction: str          # "improved" | "regressed" | "flat"
    note: str = ""


@dataclass
class TrendReport:
    has_prior: bool
    prior_batch_id: str | None
    deltas: list[TrendDelta] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)


# Metric direction map: True == higher is better.
_HIGHER_IS_BETTER: dict[str, bool] = {
    "engagement_rate": True,
    "live_contact_rate": True,
    "gatekeeper_handoff_success_rate": True,
    "median_time_to_hangup_s": True,   # longer before hangup == better opener
    "reasoning_leak_count": False,
    "finding_leaked_to_gatekeeper_count": False,
    "caller_id_skew": False,
}


def compute_trend(current: BatchMetrics, prior: BatchMetrics | None) -> TrendReport:
    """Compare the current batch to the immediately-prior batch.

    Flags regressions (any hard-guard leak > 0 is ALWAYS a regression; other
    metrics regress when they move the wrong way beyond a small epsilon) and
    improvements. Deterministic — no LLM.
    """
    if prior is None:
        # First batch: still flag absolute leak failures as regressions so a
        # broken first batch isn't silently "fine".
        regressions: list[str] = []
        if current.reasoning_leak_count > 0:
            regressions.append(
                f"reasoning_leak_count={current.reasoning_leak_count} "
                f"(P0 regression guard — must be 0)"
            )
        if current.finding_leaked_to_gatekeeper_count > 0:
            regressions.append(
                f"finding_leaked_to_gatekeeper_count="
                f"{current.finding_leaked_to_gatekeeper_count} "
                f"(gatekeeper-opener regression guard — must be 0)"
            )
        return TrendReport(
            has_prior=False, prior_batch_id=None,
            deltas=[], regressions=regressions, improvements=[],
        )

    deltas: list[TrendDelta] = []
    regressions = []
    improvements: list[str] = []
    eps = 1e-6

    for metric, higher_better in _HIGHER_IS_BETTER.items():
        cur = getattr(current, metric)
        prev = getattr(prior, metric)
        if cur is None or prev is None:
            continue
        diff = cur - prev
        if abs(diff) <= eps:
            direction = "flat"
        elif (diff > 0) == higher_better:
            direction = "improved"
        else:
            direction = "regressed"
        note = ""
        deltas.append(TrendDelta(metric, prev, cur, direction, note))
        label = f"{metric}: {prev} -> {cur}"
        if direction == "regressed":
            regressions.append(label)
        elif direction == "improved":
            improvements.append(label)

    # Absolute hard-guards ALWAYS regress if non-zero, even if unchanged.
    if current.reasoning_leak_count > 0:
        msg = (
            f"reasoning_leak_count={current.reasoning_leak_count} "
            f"(P0 regression guard — must be 0)"
        )
        if msg not in regressions:
            regressions.append(msg)
    if current.finding_leaked_to_gatekeeper_count > 0:
        msg = (
            f"finding_leaked_to_gatekeeper_count="
            f"{current.finding_leaked_to_gatekeeper_count} "
            f"(gatekeeper-opener regression guard — must be 0)"
        )
        if msg not in regressions:
            regressions.append(msg)

    return TrendReport(
        has_prior=True,
        prior_batch_id=prior.batch_id,
        deltas=deltas,
        regressions=regressions,
        improvements=improvements,
    )


# ---------------------------------------------------------------------------
# Next-remediation signal — reasoning-becomes-capability.
# ---------------------------------------------------------------------------
@dataclass
class Remediation:
    metric: str
    severity: int          # higher == more urgent
    what_to_fix: str


def next_remediations(
    current: BatchMetrics,
    trend: TrendReport,
    *,
    limit: int = 5,
) -> list[Remediation]:
    """Rank the weakest / most-regressed metrics with a one-line fix.

    Deterministic heuristics — the machine's own suggestion for what to work on
    next. Hard-guard failures dominate; then regressions; then weak absolutes.
    """
    out: list[Remediation] = []

    # 1. Hard-guard leaks — always top priority.
    if current.reasoning_leak_count > 0:
        phrases = ", ".join(sorted(current.reasoning_leak_phrases)) or "banned phrases"
        out.append(Remediation(
            metric="reasoning_leak_count",
            severity=100,
            what_to_fix=(
                f"Morgan narrated protocol aloud ({current.reasoning_leak_count}x: "
                f"{phrases}). Re-run Sync-MorganAssistant.ps1 -Live to push the "
                f"hardened OUTPUT CONTRACT; verify the prompt still bans these."
            ),
        ))
    if current.finding_leaked_to_gatekeeper_count > 0:
        out.append(Remediation(
            metric="finding_leaked_to_gatekeeper_count",
            severity=95,
            what_to_fix=(
                f"The withheld finding leaked to a gatekeeper "
                f"({current.finding_leaked_to_gatekeeper_count}x). Reinforce the "
                f"'reveal {{{{callsheet_finding}}}} only after owner confirmed' "
                f"gate in the GATEKEEPER HANDLING section."
            ),
        ))

    # 2. Regressed metrics from the trend.
    for delta in trend.deltas:
        if delta.direction != "regressed":
            continue
        out.append(Remediation(
            metric=delta.metric,
            severity=70,
            what_to_fix=_fix_hint(delta.metric, delta.current, delta.previous),
        ))

    # 3. Weak absolutes (independent of trend) — surface even on the first batch.
    if current.total_calls > 0:
        if current.live_contact_rate < 0.25:
            out.append(Remediation(
                metric="live_contact_rate",
                severity=50,
                what_to_fix=(
                    f"Only {current.live_contact_rate:.0%} of dials reached a human. "
                    f"Check call-hours windowing + number reputation; consider "
                    f"tightening the dial list to answered-before segments."
                ),
            ))
        if (
            current.median_time_to_hangup_s is not None
            and current.median_time_to_hangup_s < 15.0
        ):
            out.append(Remediation(
                metric="median_time_to_hangup_s",
                severity=45,
                what_to_fix=(
                    f"Median live hangup at {current.median_time_to_hangup_s}s — "
                    f"the opener is losing people fast. Iterate firstMessage "
                    f"(hook + honesty) and A/B the first 10 seconds."
                ),
            ))
        if current.gatekeeper_count > 0 and current.gatekeeper_handoff_success_rate < 0.5:
            out.append(Remediation(
                metric="gatekeeper_handoff_success_rate",
                severity=40,
                what_to_fix=(
                    f"Gatekeeper -> owner handoff succeeded only "
                    f"{current.gatekeeper_handoff_success_rate:.0%} of the time. "
                    f"Strengthen the 'who handles the website' ask + owner_name use."
                ),
            ))
        if current.caller_id_skew > 0.5:
            out.append(Remediation(
                metric="caller_id_skew",
                severity=35,
                what_to_fix=(
                    f"Caller-ID rotation is skewed ({current.caller_id_skew:.0%}). "
                    f"One number is carrying most of the volume — verify all "
                    f"phone_number_ids in the pool are active (GET /phone-number)."
                ),
            ))

    # De-dup by metric (keep highest severity), sort, cap.
    best: dict[str, Remediation] = {}
    for r in out:
        if r.metric not in best or r.severity > best[r.metric].severity:
            best[r.metric] = r
    ranked = sorted(best.values(), key=lambda r: r.severity, reverse=True)
    return ranked[:limit]


def _fix_hint(metric: str, cur: float | None, prev: float | None) -> str:
    hints = {
        "engagement_rate": "Engagement dropped — review recent transcripts for a "
                           "new objection or a script change that stopped landing.",
        "live_contact_rate": "Fewer humans reached — check number reputation + "
                            "call-hours windowing.",
        "gatekeeper_handoff_success_rate": "Handoffs converting worse — refine the "
                            "gatekeeper ask for the decision-maker.",
        "median_time_to_hangup_s": "People hanging up faster — the opener regressed; "
                            "revert or re-tune firstMessage.",
        "caller_id_skew": "Rotation got lumpier — confirm all pool numbers are live.",
    }
    base = hints.get(metric, f"{metric} regressed.")
    return f"{base} (was {prev}, now {cur})"


# ---------------------------------------------------------------------------
# Durable store — append-only JSONL, cross-batch.
# ---------------------------------------------------------------------------
def _store_path() -> Path:
    return storage.root() / _BATCH_STORE_FILE


def append_batch(metrics: BatchMetrics) -> Path:
    """Append this batch's metrics to the durable JSONL trend store.

    One JSON object per line. Never raises — a store failure is logged and the
    (in-memory) report is still returned to the caller.
    """
    path = _store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(metrics), ensure_ascii=False, default=str))
            fh.write("\n")
        _LOG.info("appended batch %s to %s", metrics.batch_id, path.name)
    except OSError as exc:
        _LOG.warning("append_batch failed: %s", exc)
    return path


def load_batches(limit: int | None = None) -> list[BatchMetrics]:
    """Load persisted batch metrics oldest-first. Skips corrupt lines."""
    path = _store_path()
    if not path.exists():
        return []
    out: list[BatchMetrics] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                out.append(BatchMetrics(**data))
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("skipping corrupt batch line: %s", exc)
    except OSError as exc:
        _LOG.warning("load_batches read failed: %s", exc)
    if limit is not None:
        return out[-limit:]
    return out


def load_prior_batch(before_batch_id: str | None = None) -> BatchMetrics | None:
    """Return the most recent persisted batch (optionally strictly before a id)."""
    batches = load_batches()
    if not batches:
        return None
    if before_batch_id is None:
        return batches[-1]
    for b in reversed(batches):
        if b.batch_id != before_batch_id:
            return b
    return None


# ---------------------------------------------------------------------------
# Batch assembly — pull calls, correlate with the dial-run, analyze.
# ---------------------------------------------------------------------------
def _load_dial_run(path: str | Path) -> dict[str, Any] | None:
    """Load a dial_run_<id>.json artifact. Returns None on any failure."""
    try:
        p = Path(path)
        if not p.exists():
            _LOG.warning("dial-run artifact not found: %s", p)
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("failed to load dial-run %s: %s", path, exc)
        return None


def _placed_call_ids(dial_run: dict[str, Any]) -> set[str]:
    """Extract the call_ids the dial-run actually PLACED (outcome=initiated)."""
    ids: set[str] = set()
    for att in dial_run.get("attempts", []) or []:
        if att.get("outcome") == "initiated" and att.get("call_id"):
            ids.add(str(att["call_id"]))
    return ids


def _findings_by_call_id(dial_run: dict[str, Any]) -> dict[str, str]:
    """Map placed call_id -> the prospect's withheld finding, if the artifact
    carries it. Dial-run attempts don't include the finding today, so this is
    best-effort and returns {} unless a future artifact adds it. The analyzer
    also accepts findings pulled from Vapi call metadata (see analyze_batch).
    """
    out: dict[str, str] = {}
    for att in dial_run.get("attempts", []) or []:
        cid = att.get("call_id")
        finding = att.get("callsheet_finding") or att.get("finding")
        if cid and finding:
            out[str(cid)] = str(finding)
    return out


def _since_epoch(since_ts: str) -> float | None:
    try:
        return datetime.fromisoformat(
            since_ts.replace("Z", "+00:00")
        ).timestamp()
    except (ValueError, TypeError):
        return None


def _call_created_epoch(call: Any) -> float | None:
    val = call.get("createdAt") if isinstance(call, dict) else getattr(call, "createdAt", None)
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _finding_from_call(call: Any) -> str:
    """Pull the withheld finding out of a Vapi call's metadata / overrides.

    The dialer forwards ``callsheet_finding`` in assistantOverrides.variableValues
    and may echo it in metadata. Vapi returns these on GET /call, so the audit
    can recover the withheld finding without the dial-run carrying it. Best-
    effort; returns "" when absent.
    """
    def _g(name: str) -> Any:
        return call.get(name) if isinstance(call, dict) else getattr(call, name, None)

    for container_name in ("metadata", "assistantOverrides"):
        container = _g(container_name)
        if isinstance(container, dict):
            if container.get("callsheet_finding"):
                return str(container["callsheet_finding"])
            vv = container.get("variableValues")
            if isinstance(vv, dict) and vv.get("callsheet_finding"):
                return str(vv["callsheet_finding"])
    return ""


def analyze_batch(
    *,
    since_ts: str | None = None,
    dial_run_id: str | None = None,
    client: Any = None,
    persist: bool = True,
) -> "BatchResult":
    """Analyze one dial batch end-to-end.

    Pull the batch's calls (read-only ``GET /call``), scope to the batch
    (the dial-run's placed set, or a ``since_ts`` window), audit each call,
    aggregate, compare to the prior persisted batch, rank the next remediation,
    surface recurring failure phrases for the callsheet learning loop, and
    (by default) append the batch metrics to the durable trend store.

    ``client`` is an injectable Vapi client (must expose ``list_calls``); when
    None a :class:`backend.voice.client.VapiClient` is built from settings.
    Tests inject a fake — no network.
    """
    dial_run: dict[str, Any] | None = None
    placed_ids: set[str] | None = None
    findings: dict[str, str] = {}
    batch_id = ""

    if dial_run_id:
        dial_run = _load_dial_run(dial_run_id)
        if dial_run:
            placed_ids = _placed_call_ids(dial_run)
            findings = _findings_by_call_id(dial_run)
            batch_id = str(dial_run.get("run_id") or Path(str(dial_run_id)).stem)

    if not batch_id:
        batch_id = f"batch_{since_ts or iso_now()}"

    # Pull the calls — read-only.
    if client is None:
        client = _build_client()
    try:
        calls = client.list_calls(limit=_VAPI_LIST_LIMIT)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("Vapi list_calls failed: %s", exc)
        calls = []

    # Scope to the batch.
    scoped = _scope_calls(calls, placed_ids=placed_ids, since_ts=since_ts)

    banned = _load_banned_self_narration()
    audits: list[CallAudit] = []
    for call in scoped:
        cid = call.get("id") if isinstance(call, dict) else getattr(call, "id", "")
        finding = findings.get(str(cid), "") or _finding_from_call(call)
        audits.append(analyze_call(call, finding=finding, banned=banned))

    metrics = aggregate_batch(audits, batch_id=batch_id)
    prior = load_prior_batch(before_batch_id=batch_id)
    trend = compute_trend(metrics, prior)
    remediations = next_remediations(metrics, trend)
    learning_notes = surface_learning_notes(audits)

    if persist:
        append_batch(metrics)

    return BatchResult(
        batch_id=batch_id,
        metrics=metrics,
        audits=audits,
        trend=trend,
        remediations=remediations,
        learning_notes=learning_notes,
    )


def _scope_calls(
    calls: list[Any],
    *,
    placed_ids: set[str] | None,
    since_ts: str | None,
) -> list[Any]:
    """Restrict the pulled calls to the batch under audit."""
    def _cid(c: Any) -> str:
        return str(c.get("id") if isinstance(c, dict) else getattr(c, "id", "") or "")

    if placed_ids:
        return [c for c in calls if _cid(c) in placed_ids]
    if since_ts:
        floor = _since_epoch(since_ts)
        if floor is not None:
            out = []
            for c in calls:
                ep = _call_created_epoch(c)
                if ep is None or ep >= floor:
                    out.append(c)
            return out
    return list(calls)


def _build_client() -> Any:
    """Construct a read-only Vapi client from settings. Never places calls."""
    from backend.common.config import get_settings
    from .client import VapiClient

    settings = get_settings()
    api_key = getattr(settings, "vapi_api_key", "") or ""
    if not api_key:
        raise RuntimeError("VAPI_API_KEY is not set — cannot pull the batch.")
    return VapiClient(api_key)


# ---------------------------------------------------------------------------
# Learning-loop feed — surface (append-only) recurring failure phrases.
# ---------------------------------------------------------------------------
def surface_learning_notes(audits: list[CallAudit]) -> list[str]:
    """Non-destructively surface recurring failure signals for callsheet_updater.

    We do NOT write dynamic_callsheet_intel.json here — the existing
    pattern_aggregator -> callsheet_updater path owns that store. This returns
    human-readable notes the operator (or a downstream call) can feed in, matching
    the existing 'surface/append, never overwrite' contract.
    """
    notes: list[str] = []
    leaked = Counter()
    for a in audits:
        for p in a.reasoning_leak_phrases:
            leaked[p] += 1
    for phrase, n in leaked.most_common():
        notes.append(f"[REASONING-LEAK x{n}] Morgan spoke banned phrase: '{phrase}'")

    gk_leaks = sum(1 for a in audits if a.finding_leaked_to_gatekeeper)
    if gk_leaks:
        notes.append(
            f"[FINDING-LEAK x{gk_leaks}] withheld finding revealed to a gatekeeper"
        )

    fast_hangups = [
        a for a in audits
        if a.time_to_hangup_s is not None and a.time_to_hangup_s < 15.0
    ]
    if len(fast_hangups) >= 2:
        notes.append(
            f"[OPENER-WEAK x{len(fast_hangups)}] live humans hung up under 15s — "
            f"opener needs iteration"
        )
    return notes


# ---------------------------------------------------------------------------
# Result container + report rendering
# ---------------------------------------------------------------------------
@dataclass
class BatchResult:
    batch_id: str
    metrics: BatchMetrics
    audits: list[CallAudit]
    trend: TrendReport
    remediations: list[Remediation]
    learning_notes: list[str]


def render_report(result: BatchResult) -> str:
    """Human-readable batch report + trend + next-remediation signal."""
    m = result.metrics
    lines: list[str] = []
    lines.append(f"=== CALL-BATCH AUDIT: {result.batch_id} ===")
    lines.append(f"analyzed: {m.analyzed_ts}   calls: {m.total_calls}")
    if m.total_calls == 0:
        lines.append("(no calls in this batch — nothing to audit)")
        return "\n".join(lines)

    lines.append("")
    lines.append("-- outcomes --")
    for bucket in _OUTCOME_ORDER:
        if bucket in m.outcome_distribution:
            lines.append(f"  {bucket:<14} {m.outcome_distribution[bucket]}")
    lines.append("")
    lines.append("-- metrics --")
    lines.append(f"  engagement_rate            {m.engagement_rate:.0%}")
    lines.append(f"  live_contact_rate          {m.live_contact_rate:.0%}")
    lines.append(f"  gatekeeper_count           {m.gatekeeper_count}")
    lines.append(f"  gatekeeper_handoff_success {m.gatekeeper_handoff_success_rate:.0%}")
    lines.append(f"  median_time_to_hangup_s    {m.median_time_to_hangup_s}")
    lines.append(f"  mean_time_to_hangup_s      {m.mean_time_to_hangup_s}")
    lines.append(f"  reasoning_leak_count       {m.reasoning_leak_count}"
                 + ("  <-- P0 REGRESSION" if m.reasoning_leak_count else "  (clean)"))
    lines.append(f"  finding_leaked_to_gk       {m.finding_leaked_to_gatekeeper_count}"
                 + ("  <-- REGRESSION" if m.finding_leaked_to_gatekeeper_count else "  (clean)"))
    lines.append(f"  caller_id_skew             {m.caller_id_skew:.0%}")
    lines.append(f"  total_cost                 ${m.total_cost:.2f}")
    if m.caller_id_distribution:
        lines.append("  caller-id distribution:")
        for num, cnt in sorted(m.caller_id_distribution.items()):
            lines.append(f"    {num[:12]:<12} {cnt}")

    lines.append("")
    lines.append("-- trend vs prior batch --")
    if not result.trend.has_prior:
        lines.append("  (no prior batch on record — this is the baseline)")
    else:
        lines.append(f"  prior: {result.trend.prior_batch_id}")
        for d in result.trend.deltas:
            arrow = {"improved": "^", "regressed": "v", "flat": "="}[d.direction]
            lines.append(f"    {arrow} {d.metric}: {d.previous} -> {d.current} "
                         f"({d.direction})")
    if result.trend.improvements:
        lines.append(f"  improvements: {len(result.trend.improvements)}")
    if result.trend.regressions:
        lines.append("  REGRESSIONS:")
        for r in result.trend.regressions:
            lines.append(f"    ! {r}")

    lines.append("")
    lines.append("-- next remediation (machine's own suggestion) --")
    if not result.remediations:
        lines.append("  nothing flagged — batch is healthy.")
    else:
        for i, rem in enumerate(result.remediations, 1):
            lines.append(f"  {i}. [{rem.metric}] {rem.what_to_fix}")

    if result.learning_notes:
        lines.append("")
        lines.append("-- learning-loop notes (surface to callsheet_updater) --")
        for note in result.learning_notes:
            lines.append(f"  {note}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Autonomous audit — the capability that removes the operator from the loop.
# Called from the post-call reconcile sweep (backend.voice.reconcile_cli), which
# already runs on a schedule (Samus-CallReconcile), so every dial batch is
# audited, trended, and regression-alerted WITHOUT anyone invoking the analyzer.
# ---------------------------------------------------------------------------
def _today_start_iso() -> str:
    """Midnight-UTC ISO for 'today', the reconcile sweep's own call window."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")


def _write_report_artifact(result: "BatchResult") -> None:
    try:
        d = storage.root() / "voice" / "batch_audits"
        d.mkdir(parents=True, exist_ok=True)
        (d / "latest.txt").write_text(render_report(result), encoding="utf-8")
    except OSError as exc:
        _LOG.warning("audit report artifact write failed: %s", exc)


def _write_alert_artifact(result: "BatchResult") -> None:
    """Persist an operator-visible alert on a hard-guard regression.

    A prompt leak needs a human decision to re-push (operator-gated), so the
    autonomous audit SURFACES it rather than acting — the directive's 'surface
    issues immediately' with governance intact.
    """
    try:
        d = storage.root() / "voice" / "audit_alerts"
        d.mkdir(parents=True, exist_ok=True)
        m = result.metrics
        # The offending calls make the alert ACTIONABLE and let a reader tell a
        # stale historical leak (already-fixed call still in today's window) from
        # a fresh regression — without re-deriving it by hand.
        offending = [
            {
                "call_id": a.call_id,
                "company": a.company,
                "reasoning_leak_phrases": a.reasoning_leak_phrases,
                "finding_leaked_to_gatekeeper": a.finding_leaked_to_gatekeeper,
            }
            for a in result.audits
            if a.reasoning_leak_detected or a.finding_leaked_to_gatekeeper
        ]
        payload = {
            "ts": iso_now(),
            "batch_id": m.batch_id,
            "severity": "P0",
            "reasoning_leak_count": m.reasoning_leak_count,
            "reasoning_leak_phrases": m.reasoning_leak_phrases,
            "finding_leaked_to_gatekeeper_count": m.finding_leaked_to_gatekeeper_count,
            "offending_calls": offending,
            "next_remediation": [r.what_to_fix for r in result.remediations[:3]],
        }
        # Sanitize the batch_id for a filename (ISO batch ids carry ':' which is
        # illegal on Windows).
        safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", m.batch_id)
        (d / f"alert_{safe_id}.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        _LOG.warning("audit alert artifact write failed: %s", exc)


def autonomous_audit(
    *,
    since_ts: str | None = None,
    client: Any = None,
    alert: bool = True,
) -> "BatchResult | None":
    """Self-audit the current day's call batch with NO operator in the loop.

    Runs from the reconcile sweep: computes the audit, appends the metrics to the
    durable trend store ONLY when the call count changed (dedup — the sweep runs
    often), writes the human report artifact, and on a hard-guard regression
    (reasoning leak / finding-leaked-to-gatekeeper > 0) writes an operator alert
    artifact + WARNING. Never raises — a self-audit failure must not disturb
    reconcile. Returns the BatchResult (or None on any failure).
    """
    try:
        window = since_ts or _today_start_iso()
        result = analyze_batch(since_ts=window, client=client, persist=False)
        # Dedup: the reconcile sweep runs every ~20 min; only append a new trend
        # point when the batch actually grew, so "is this cycle better than the
        # last" stays meaningful instead of filling with identical rows.
        last = load_prior_batch()
        if last is None or result.metrics.total_calls != last.total_calls:
            append_batch(result.metrics)
        _write_report_artifact(result)
        m = result.metrics
        if alert and (m.reasoning_leak_count > 0
                      or m.finding_leaked_to_gatekeeper_count > 0):
            _write_alert_artifact(result)
            _LOG.warning(
                "AUTONOMOUS AUDIT P0 REGRESSION: batch=%s reasoning_leaks=%d "
                "finding_leaks=%d — see audit_alerts/",
                m.batch_id, m.reasoning_leak_count,
                m.finding_leaked_to_gatekeeper_count,
            )
        return result
    except Exception as exc:  # noqa: BLE001 — never disturb the reconcile sweep
        _LOG.warning("autonomous_audit failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.voice.call_batch_analyzer",
        description="Post-batch call auditor — outcomes, leaks, trend, and the "
                    "next thing to fix. Read-only Vapi (GET /call).",
    )
    parser.add_argument("--since", metavar="ISO",
                        help="Only audit calls created at/after this ISO timestamp.")
    parser.add_argument("--dial-run", metavar="PATH",
                        help="Path to a dial_run_<id>.json artifact to scope the "
                             "batch to its placed call_ids.")
    parser.add_argument("--no-persist", action="store_true",
                        help="Do not append to the durable trend store (preview).")
    parser.add_argument("--json", action="store_true",
                        help="Emit the batch metrics as JSON instead of a report.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        result = analyze_batch(
            since_ts=args.since,
            dial_run_id=args.dial_run,
            persist=not args.no_persist,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(asdict(result.metrics), indent=2, default=str))
    else:
        print(render_report(result))

    # Non-zero exit on a hard-guard regression so a scheduled run can alert.
    if (result.metrics.reasoning_leak_count > 0
            or result.metrics.finding_leaked_to_gatekeeper_count > 0):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
