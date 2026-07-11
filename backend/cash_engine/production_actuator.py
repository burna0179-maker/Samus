"""Production actuator — what the idle-production drive DOES when it decides to
produce (ADR-016 / ADR-017 phase 3).

Two channels, both through governed paths only:

  - EMAIL: a paced, stake-gated outreach batch (CAN-SPAM-compliant; the Codex
    stake guard fires inside ``morning_batch``). Always safe to run.
  - VOICE: per-prospect routing under the consent gate (ADR-017):
      * no consent basis (cold Apollo number) -> VOICEMAIL DRAFT artifact for
        Alex (ADR-002). Never a live call.
      * consent basis + all fences pass       -> one governed live DIAL via
        ``place_governed_dial`` (Codex ratifies).
      * consent basis + a fence not satisfied -> SKIP this tick (retry later).

The routing is pure (:func:`_route_voice`); the orchestration
(:func:`run_production`) takes an injected :class:`ProductionDeps` so every real
binding (candidate source, consent classifier, fence computation, live dialer,
voicemail drafter, email batch) is swappable in tests and no live send/call fires
under test. The default deps classify consent CONSERVATIVELY (absent signal =>
no consent => voicemail), so the cold dial list can only ever produce drafts —
a live dial requires an explicit consent signal that the cold list never carries.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

from backend.voice.governed_dial import _REQUIRED_FENCES, place_governed_dial

_LOG = logging.getLogger("samus.cash_engine.production_actuator")

# Per-tick ceilings. Deliberately small: the drive fires every control tick, so
# a low cap keeps volume paced and monitorable rather than bursty.
_DEFAULT_CAP_VOICE = 5
_DEFAULT_CAP_EMAIL = 10

_CAMPAIGN = "idle_production_drive"


def _route_voice(fences: dict[str, Any], *, live_dial_allowed: bool = True) -> str:
    """Pure per-prospect routing. ``fences`` carries the 5 governed fences incl.
    ``consent_ok``. Returns ``"voicemail"`` | ``"dial"`` | ``"skip"``.

    ``live_dial_allowed`` is the CODB / affordability ceiling (ADR-018): a live
    call is a PAID action, so when the reasoner's posture doesn't permit paid
    spend (conserve/lean), even a fully-consented+fenced prospect is routed to a
    free voicemail draft rather than a live dial. The ceiling on call VOLUME is
    thus the same headroom the CODB reasoner enforces — not a separate cap."""
    if fences.get("consent_ok") is not True:
        return "voicemail"  # ADR-002 — no consent basis, never a live call
    if all(fences.get(k) is True for k in _REQUIRED_FENCES):
        return "dial" if live_dial_allowed else "voicemail"
    return "skip"  # consented but a timing/frequency fence is not satisfied now


@dataclass
class ProductionDeps:
    """Injected bindings for the actuator. Defaults (``_default_production_deps``)
    wire the real, defensive implementations; tests pass fakes."""
    voice_candidates: Callable[[], list[Any]]
    classify_consent: Callable[[Any], bool]
    compute_fences: Callable[[Any], dict[str, Any]]
    draft_voicemail: Callable[[Any], dict[str, Any]]
    email_batch: Callable[[int], dict[str, Any]]
    dial_fn: Optional[Callable[..., Any]] = None
    stake_of: Callable[[Any], str] = lambda p: str(getattr(p, "callsheet_opener", "") or "")
    id_of: Callable[[Any], str] = lambda p: str(getattr(p, "prospect_id", "") or "")
    phone_of: Callable[[Any], str] = lambda p: str(getattr(p, "phone", "") or "")


def run_email_channel(deps: ProductionDeps, cap: int = _DEFAULT_CAP_EMAIL) -> dict[str, Any]:
    """One paced, stake-gated outreach batch. Never raises."""
    out = {"sent": 0, "failed": 0}
    try:
        eb = deps.email_batch(cap) or {}
        out = {"sent": int(eb.get("sent", 0)), "failed": int(eb.get("failed", 0))}
    except Exception as exc:  # noqa: BLE001 — a channel fault never sinks the pass
        _LOG.warning("email channel failed: %s", exc)
        out["error"] = str(exc)
    return out


def run_voice_channel(deps: ProductionDeps, cap: int = _DEFAULT_CAP_VOICE,
                      *, live_dial_allowed: bool = True) -> dict[str, Any]:
    """Consent-routed voice pass (cold => voicemail draft; consented + fenced =>
    governed live dial). Never raises. ``dialed + drafted`` is bounded by ``cap``.

    ``live_dial_allowed`` (CODB / affordability ceiling, ADR-018): False forces
    every dial-eligible prospect to a voicemail draft, so live call volume is
    bounded by the reasoner's paid-spend headroom, not a separate cap."""
    v: dict[str, Any] = {"dialed": 0, "drafted": 0, "skipped": 0, "blocked": 0}
    try:
        candidates = list(deps.voice_candidates() or [])
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("voice candidate load failed: %s", exc)
        v["error"] = str(exc)
        return v

    for prospect in candidates:
        if v["dialed"] + v["drafted"] >= cap:
            break
        try:
            fences = dict(deps.compute_fences(prospect) or {})
            fences["consent_ok"] = bool(deps.classify_consent(prospect))
            route = _route_voice(fences, live_dial_allowed=live_dial_allowed)

            if route == "voicemail":
                if (deps.draft_voicemail(prospect) or {}).get("drafted"):
                    v["drafted"] += 1
                continue
            if route == "skip":
                v["skipped"] += 1
                continue
            # route == "dial": consented + fully fenced -> governed live dial.
            if deps.dial_fn is None:
                # Consented + ready, but no live dialer bound. Do NOT actuate;
                # count as skipped so the operator sees latent consented demand.
                v["skipped"] += 1
                continue
            # Build Morgan's per-prospect variables (opener, voicemail,
            # prior-touch context) so the governed dial is INFORMED. Best-effort
            # — a builder fault just yields a barer call, never a block.
            variable_values: dict[str, Any] = {}
            customer_name = ""
            try:
                from backend.voice.dialer import _build_variable_values
                variable_values = _build_variable_values(prospect)
                customer_name = str(getattr(prospect, "company_name", "") or "")
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("voice variable-values build failed: %s", exc)
            result = place_governed_dial(
                prospect_id=deps.id_of(prospect),
                stake_sentence=deps.stake_of(prospect),
                fences=fences,
                dial_fn=deps.dial_fn,
                phone=deps.phone_of(prospect),
                campaign=_CAMPAIGN,
                variable_values=variable_values,
                customer_name=customer_name,
            )
            v["dialed" if getattr(result, "dialed", False) else "blocked"] += 1
        except Exception as exc:  # noqa: BLE001 — one bad prospect never sinks the pass
            _LOG.warning("voice actuation error: %s", exc)
            v["skipped"] += 1
    return v


def run_production(
    decision: Any,
    *,
    deps: ProductionDeps,
    cap_voice: int = _DEFAULT_CAP_VOICE,
    cap_email: int = _DEFAULT_CAP_EMAIL,
) -> dict[str, Any]:
    """Actuate one bounded production pass (email + voice). Never raises; every
    channel is best-effort and tallied. ``decision`` is the IdleDriveDecision
    that triggered this (used only for logging context)."""
    email = run_email_channel(deps, cap_email)
    voice = run_voice_channel(deps, cap_voice)
    summary: dict[str, Any] = {
        "channel": "governed", "reason": getattr(decision, "reason", ""),
        "voice": voice, "email": email,
        "initiated": voice["dialed"] + voice["drafted"] + email["sent"],
    }
    _LOG.info(
        "idle-drive produced: dialed=%d drafted=%d email_sent=%d (skipped=%d blocked=%d)",
        voice["dialed"], voice["drafted"], email["sent"], voice["skipped"], voice["blocked"],
    )
    return summary


# ---------------------------------------------------------------------------
# Default (real) bindings. Every one is defensive: a fault degrades to the
# safest outcome (no consent / fence closed / nothing produced), never a raise.
# ---------------------------------------------------------------------------

def _default_voice_candidates() -> list[Any]:
    """Today's cold call-list prospects, or []. These carry NO consent basis."""
    try:
        from backend.common import storage
        from backend.common.us_timezones import business_today
        from backend.voice.dialer import load_prospects_from_csv

        csv = storage.root() / "daily_calls" / f"call_list_{business_today().isoformat()}.csv"
        if not csv.is_file():
            return []
        return list(load_prospects_from_csv(csv))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("idle-drive candidate load failed: %s", exc)
        return []


def _default_classify_consent(prospect: Any) -> bool:
    """Consent classifier for the idle-drive's GOVERNED CONSENT dial lane.
    Fail-safe: False on doubt (→ voicemail draft, ADR-002/017).

    Only an EXPLICIT per-prospect opt-in marker (warm/inbound/customer source)
    clears it; a cold Google-Places prospect never does — it is routed to a
    voicemail draft in this lane.

    NOTE (2026-07-08): operator-authorized cold B2B LIVE dialing is NOT done
    here. It is a SEPARATE lane — ``backend/gateway/cold_dial_task.py`` — which
    restores the operator's proven ``dialer.dial_call_list`` path, gated by the
    daily pre-shift attestation (ADR-018). Keeping cold prospects at "no consent"
    in THIS classifier is deliberate: it prevents a second, overlapping live-dial
    path (the two would double-dial the same business, and this lane's /voice/call
    hop does not share the dialer's already-called-today gate). One cold lane,
    no parallel dialer."""
    try:
        for attr in ("consent_ok", "has_consent", "opted_in"):
            if getattr(prospect, attr, None) is True:
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


def _default_compute_fences(prospect: Any) -> dict[str, Any]:
    """The 4 operational fences (consent is added by run_production). Every fence
    fails CLOSED on error."""
    fences = {"within_call_hours": False, "cooldown_ok": False,
              "under_daily_cap": False, "dnc_ok": False}
    pid = str(getattr(prospect, "prospect_id", "") or "")
    phone = str(getattr(prospect, "phone", "") or "")
    state = getattr(prospect, "state", "") or ""
    try:
        from backend.common.us_timezones import is_within_call_hours
        fences["within_call_hours"] = bool(is_within_call_hours(state=state))
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.voice.dialer import _in_dial_cooldown
        in_cd, _ = _in_dial_cooldown(pid, phone, 7)
        fences["cooldown_ok"] = not in_cd
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.voice.dialer import _already_called_today
        # Per-prospect daily proxy for the cap: not yet called today.
        fences["under_daily_cap"] = not _already_called_today(pid, "")
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.voice.dialer import _is_suppressed
        fences["dnc_ok"] = not _is_suppressed(phone)
    except Exception:  # noqa: BLE001
        pass
    return fences


def _default_draft_voicemail(prospect: Any) -> dict[str, Any]:
    """Persist the ADR-002 voicemail-draft artifact for Alex. Best-effort."""
    try:
        from backend.common import storage
        from backend.common.us_timezones import business_today

        text = str(getattr(prospect, "callsheet_voicemail", "") or "")
        if not text:
            from backend.prospecting.callsheet import _voicemail
            text = _voicemail(prospect)
        # Recycle pass (2026-07-03): a returning prospect's voicemail opens
        # as a follow-up — continuity compounds warmth; a cold re-open burns
        # it. The internal touch digest stays off the customer-facing script;
        # it is appended below as an operator-only note instead.
        if str(getattr(prospect, "recycled", "") or "").lower() == "true":
            head, sep, rest = text.partition(". ")
            if sep and rest:
                # Weave the clause after the greeting sentence so the script
                # keeps ONE natural greeting: "Hi, this is X from HustleForge.
                # Following up on my earlier message — <original pitch>"
                text = f"{head}. Following up on my earlier message — {rest}"
            else:
                text = "Following up on my earlier message — " + text.lstrip()
            digest = str(getattr(prospect, "prior_touch_summary", "") or "")
            if digest:
                text += f"\n\n[operator context — do not read aloud] {digest}"
        pid = str(getattr(prospect, "prospect_id", "") or "unknown")
        out_dir = storage.root() / "voice" / "voicemail_drafts"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{pid}_{business_today().isoformat()}.txt"
        if path.is_file():
            # Already drafted this business day. drafted=False makes the
            # channel loop move on WITHOUT consuming cap, so successive ticks
            # advance down the call list instead of re-drafting the same top-N
            # forever (observed 2026-07-03: three ticks, same 3 drafts, 40
            # prospects untouched).
            return {"drafted": False, "already_drafted": True, "path": str(path)}
        path.write_text(text, encoding="utf-8")
        return {"drafted": True, "path": str(path)}
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("idle-drive voicemail draft failed: %s", exc)
        return {"drafted": False, "error": str(exc)}


def _default_email_batch(cap: int) -> dict[str, Any]:
    """Run a paced, stake-gated outreach batch via morning_batch (its own Codex
    stake guard + suppression + dedup apply). Best-effort; dormant until the
    drive is armed. morning_batch.main returns an rc, not counts, so we surface
    the run status rather than a fabricated tally."""
    try:
        from backend.common import storage
        from backend.common.us_timezones import business_today
        from backend.outreach import morning_batch

        csv = storage.root() / "daily_calls" / f"call_list_{business_today().isoformat()}.csv"
        if not csv.is_file():
            return {"sent": 0, "failed": 0, "note": "no call-list csv"}
        args = ["--csv", str(csv), "--max-send", str(cap)]
        # Hot threshold is OPERATOR POLICY, surfaced as an env knob so tuning
        # it never needs a code change. Default stays morning_batch's own 70.
        # Context (7/03): a thin small-market ring topped out at score 66 —
        # zero rows cleared 70 and the email channel was structurally dry;
        # the operator can trade lead quality for volume here deliberately.
        min_score = (os.environ.get("SAMUS_EMAIL_HOT_MIN_SCORE", "") or "").strip()
        if min_score:
            args += ["--min-score", min_score]
        rc = morning_batch.main(args)
        # Surface the REAL day tallies from the batch ledger. The old
        # hardcoded sent=0 made "all hot prospects suppressed" (pool
        # exhausted — the actual 7/03 production stall) indistinguishable
        # from a send failure on the tick record.
        tally: dict[str, int] = {}
        try:
            import json as _json

            ledger = (storage.root() / "artifacts" / "outreach" /
                      f"morning_batch_{business_today().isoformat()}.jsonl")
            if not ledger.is_file():
                ledger = (storage.root() / "outreach" /
                          f"morning_batch_{business_today().isoformat()}.jsonl")
            if ledger.is_file():
                for line in ledger.read_text(encoding="utf-8").splitlines():
                    try:
                        status = str(_json.loads(line).get("status") or "unknown")
                    except Exception:  # noqa: BLE001
                        continue
                    tally[status] = tally.get(status, 0) + 1
        except Exception as exc:  # noqa: BLE001 — tally is telemetry, never fatal
            _LOG.warning("email batch ledger tally failed: %s", exc)
        return {"sent": int(tally.get("sent", 0)), "failed": int(tally.get("failed", 0)),
                "ran": rc == 0, "rc": rc, "day_tally": tally,
                "note": "counts are day-cumulative from the batch ledger"}
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("idle-drive email batch failed: %s", exc)
        return {"sent": 0, "failed": 0, "error": str(exc)}


def default_production_deps() -> ProductionDeps:
    """Wire the real, defensive bindings.

    ``dial_fn=None`` by design (2026-07-08): the idle-drive's voice lane is
    consent-routed and, with only cold Google-Places prospects in the pipeline,
    every prospect routes to a voicemail DRAFT here — no live call originates
    from this lane. Operator-authorized cold B2B LIVE dialing is the SEPARATE
    ``cold_dial_task`` lane (reuses ``dialer.dial_call_list``, attestation-gated,
    ADR-018). Leaving dial_fn None keeps this from becoming a second live-dial
    path that would race + double-dial the cold lane. If a genuinely-consented
    (warm/inbound/opt-in) source is ever wired, bind a dial_fn then — a
    deliberate, separate step."""
    return ProductionDeps(
        voice_candidates=_default_voice_candidates,
        classify_consent=_default_classify_consent,
        compute_fences=_default_compute_fences,
        draft_voicemail=_default_draft_voicemail,
        email_batch=_default_email_batch,
        dial_fn=None,
    )
