"""Morning autodialer — walks today's prospect CSV + initiates Vapi calls.

Force-multiplier loop: instead of the operator working through 30 callsheets
by hand, this module reads the machine-readable
``<artifact_root>/daily_calls/call_list_YYYY-MM-DD.csv`` produced by the
prospecting workcell, sorts by priority + score, and initiates Vapi calls
in batches honoring three safety gates:

  1. **max_calls cap** — operator-tunable per-run blast radius (default 5).
  2. **TCPA call-hours gate** — per-prospect local tz derived from the
     ``state`` column via ``backend.common.us_timezones``. Hard refuses
     outside 8am-9pm prospect-local.
  3. **dry_run by default** — the operator must opt in to live calls.

Each prospect is passed to Vapi with:
  - ``customer.number`` / ``customer.name`` — basic identification
  - ``metadata.prospect_id`` — for webhook → CSV-row correlation
  - ``metadata.run_id`` — for "which morning push did this come from"
  - ``assistantOverrides.variableValues`` — company, callsheet_opener,
    callsheet_pitch, etc., so Morgan's template can interpolate them

Persistence: every attempt (including skipped ones) is written to
``voice_audit.jsonl`` as ``action='dial_attempt'`` so the morning briefing
can render the picture. The aggregate DialRunResult is also written to
``<artifact_root>/voice/dial_runs/<run_id>.json`` for later inspection.

The module is pure-Python and synchronous; the HTTP endpoint
(``POST /voice/dial_call_list``) wraps it. Tests inject an in-process
``initiate_fn`` to avoid touching Vapi.
"""

from __future__ import annotations

import csv
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from backend.common import events, persistence, storage
from backend.common.config import get_settings
from backend.common.dates import iso_now
from backend.common.us_timezones import is_within_call_hours, state_to_timezone
from backend.voice.business_hours import is_open_now
from backend.prospecting.csv_export import CSV_COLUMNS
from backend.prospecting.models import ProspectRecord

from .models import (
    DialAttempt,
    DialerConfig,
    DialRunResult,
    InitiateCallRequest,
    InitiateCallResult,
)


_LOG = logging.getLogger("samus.voice.dialer")

_AUDIT_PATH_DEFAULT = "/opt/samus/data/voice/voice_audit.jsonl"
_EVENTS_PATH_DEFAULT = "/opt/samus/data/voice/voice_events.jsonl"
_DIAL_RUN_SUBDIR = "voice/dial_runs"

# Loose phone validation: must contain >=10 digits after stripping non-digits.
# Vapi expects E.164 (+1...) but the prospecting CSV often holds raw US 10-digit;
# the dialer normalizes here so the operator doesn't have to fix the CSV first.
_MIN_PHONE_DIGITS = 10

# Abort the dial run after this many consecutive Vapi errors (possible outage).
_VAPI_ABORT_THRESHOLD = 3


# ---------------------------------------------------------------------------
# DNC / suppression check
# ---------------------------------------------------------------------------


def _is_suppressed(phone: str) -> bool:
    """Return True if ``phone`` is on the samus_suppression DynamoDB table.

    Query: GetItem(Key={"phone": phone}). FAIL-CLOSED: on any lookup error we
    return True (treat as suppressed → skip the call). A DNC list we cannot
    verify must never be assumed empty — dialing an unverifiable number is a
    TCPA/compliance exposure. A persistent DDB outage will skip the whole run
    (operator sees the skips and investigates), which is the safe failure. This
    was a red-team finding (2026-06-30): the prior behavior returned False on
    error and would dial DNC numbers during an outage.
    """
    if not phone:
        return False
    try:
        import boto3  # lazy import — keeps module importable without AWS creds

        region = get_settings().aws_region or "us-west-1"
        table = boto3.resource("dynamodb", region_name=region).Table("samus_suppression")
        resp = table.get_item(Key={"phone": phone})
        return "Item" in resp
    except Exception as exc:  # noqa: BLE001 — fail CLOSED (suppress) on any error
        _LOG.warning("suppression check failed — FAIL-CLOSED (skipping %s): %s", phone, exc)
        return True


# ---------------------------------------------------------------------------
# Warm-prospect exclusion (mirrors prospecting/text_export cold-list filter)
# ---------------------------------------------------------------------------


def _active_warm_prospect_ids() -> set[str]:
    """Prospect IDs in an active buying_signal enrollment.

    Soft-imported so the dialer keeps no hard dependency on the outreach
    package's runtime state. Best-effort: any import / read fault returns
    an empty set so a storage glitch never blocks the whole dial run.
    Single source of truth shared with prospecting/text_export's cold-list
    exclusion — one place defines "warm", both the call list TXT and the
    autodialer honour it.
    """
    try:
        from backend.outreach.buying_signal_route import active_warm_prospect_ids

        return active_warm_prospect_ids()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("warm-prospect lookup failed (non-fatal): %s", exc)
        return set()


def _filter_warm_prospects(
    prospects: Sequence[ProspectRecord],
    run_id: str,
    started: str,
) -> list[ProspectRecord]:
    """Return the prospects that should be EXCLUDED from this dial run because
    they are in an active warm sequence (replied email / booked follow-up).

    Each exclusion is audit-logged (action='dial_skip_warm') so the operator
    sees *why* a name was held back rather than silently vanishing. The
    caller removes the returned records from the dial pool.
    """
    warm_ids = _active_warm_prospect_ids()
    if not warm_ids:
        return []
    excluded = [p for p in prospects if (p.prospect_id or "") in warm_ids]
    for p in excluded:
        ev = events.build_audit_event(
            service="voice",
            task_id=run_id,
            action="dial_skip_warm",
            input_payload={"prospect_id": p.prospect_id, "company": p.company_name},
            output_payload={
                "reason": "active_buying_signal_enrollment",
                "detail": "prospect is mid-conversation on a warmer track; "
                "cold-dialing would cross wires",
            },
            status="skipped",
        )
        _append_audit(ev)
    if excluded:
        _LOG.info(
            "dial_run %s: excluded %d warm-enrolled prospect(s) from dial pool: %s",
            run_id,
            len(excluded),
            ", ".join((p.company_name or p.prospect_id or "?") for p in excluded),
        )
    return excluded


# ---------------------------------------------------------------------------
# Vapi phone-number existence preflight (Gap-5)
# ---------------------------------------------------------------------------


def _validate_phone_numbers_exist(
    configured_ids: Sequence[str],
    settings: Any,
) -> dict[str, Any] | None:
    """Confirm every configured phone-number id exists in the Vapi account.

    Returns None when all configured ids exist (the run proceeds), or a dict
    ``{"missing": [...], "available": [...]}`` when one or more is stale.

    Fail-OPEN: any transport / auth / parse fault returns None (treat as "can't
    verify, proceed") so a Vapi ``/phone-number`` outage never blocks a run
    whose number is in fact valid. The cost of a false-negative here is just
    the pre-existing behaviour (fail at dial time); the win is catching the
    common stale-id case fast with an actionable message.
    """
    ids = [i for i in configured_ids if i]
    if not ids:
        return None
    api_key = (getattr(settings, "vapi_api_key", "") or "").strip()
    if not api_key:
        return None  # the non-empty preflight already gates on creds elsewhere
    try:
        import httpx  # lazy — keeps the module importable without httpx present

        resp = httpx.get(
            "https://api.vapi.ai/phone-number",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15.0,
        )
        if resp.status_code != 200:
            _LOG.warning(
                "phone-number preflight: Vapi list HTTP %s — skipping check (fail-open)",
                resp.status_code,
            )
            return None
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 — fail-open on any list error
        _LOG.warning("phone-number preflight skipped (fail-open): %s", exc)
        return None

    available = {
        str(n.get("id"))
        for n in (data if isinstance(data, list) else [])
        if isinstance(n, dict) and n.get("id")
    }
    missing = [i for i in ids if i not in available]
    if not missing:
        return None
    return {"missing": missing, "available": sorted(available)}


# ---------------------------------------------------------------------------
# Per-day call-cap check
# ---------------------------------------------------------------------------


def _already_called_today(prospect_id: str, run_id: str) -> bool:  # noqa: ARG001
    """Return True if ``prospect_id`` already has an 'initiated' attempt today.

    Scans ``<storage_root>/voice/dial_runs/dial_run_<YYYYMMDD>*.json`` files
    written on ``date.today()``. Fails False on any read error so a corrupt
    file never blocks dialing.
    """
    if not prospect_id:
        return False
    today_str = date.today().strftime("%Y%m%d")
    dial_runs_dir = storage.root() / "voice" / "dial_runs"
    try:
        matching = list(dial_runs_dir.glob(f"dial_run_{today_str}*.json"))
    except Exception:  # noqa: BLE001
        return False
    for fpath in matching:
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
            for attempt in data.get("attempts", []):
                if (
                    attempt.get("prospect_id") == prospect_id
                    and attempt.get("outcome") == "initiated"
                ):
                    return True
        except Exception:  # noqa: BLE001
            continue
    return False


# ---------------------------------------------------------------------------
# Per-number cooldown (7-14 days, operator-tunable)
# ---------------------------------------------------------------------------


def _in_dial_cooldown(
    prospect_id: str,
    phone: str,
    cooldown_days: int,
) -> tuple[bool, str]:
    """Return (True, last_attempt_date) if this prospect/phone was actually
    dialled within the last ``cooldown_days`` days.

    Scans ``<storage_root>/voice/dial_runs/dial_run_<YYYYMMDD>*.json`` files
    over the window. A row counts as a real dial when ``outcome`` is anything
    other than a ``skipped_*`` reason or ``dry_run`` (those did not place a
    call). Matches by prospect_id OR phone — phone catches the case where the
    same number rolls up under a new prospect_id.

    Fails closed-open (returns False) on any read error: a corrupt dial_run
    file must not block dialing entirely.
    """
    if cooldown_days <= 0:
        return (False, "")
    if not prospect_id and not phone:
        return (False, "")
    today = date.today()
    dial_runs_dir = storage.root() / "voice" / "dial_runs"
    # Glob each day in the window. cooldown_days=7 means today + 6 prior days.
    for offset in range(cooldown_days):
        day = today - timedelta(days=offset)
        day_str = day.strftime("%Y%m%d")
        try:
            matching = list(dial_runs_dir.glob(f"dial_run_{day_str}*.json"))
        except Exception:  # noqa: BLE001
            continue
        for fpath in matching:
            try:
                data = json.loads(fpath.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            for attempt in data.get("attempts", []):
                outcome = (attempt.get("outcome") or "").lower()
                if outcome.startswith("skipped_") or outcome == "dry_run":
                    continue
                a_pid = attempt.get("prospect_id") or ""
                a_phone = attempt.get("phone") or ""
                if (prospect_id and a_pid == prospect_id) or (phone and a_phone == phone):
                    return (True, day.isoformat())
    return (False, "")


# ---------------------------------------------------------------------------
# CSV loading + normalization
# ---------------------------------------------------------------------------


def default_csv_path_for_today(today: date | None = None) -> Path:
    """Return the canonical call_list path for ``today`` (or date.today())."""
    today = today or date.today()
    return storage.root() / "daily_calls" / f"call_list_{today.isoformat()}.csv"


def load_prospects_from_csv(path: Path) -> list[ProspectRecord]:
    """Parse the daily call CSV into typed records.

    Rows that fail validation are skipped + logged at WARNING; the loader
    never raises on a bad row — the dialer always returns *some* result.
    Missing columns default to empty strings (ProspectRecord tolerates).
    """
    if not path.exists():
        raise FileNotFoundError(f"call list CSV not found: {path}")
    out: list[ProspectRecord] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row_num, row in enumerate(reader, start=2):  # header is line 1
            # ProspectRecord has more fields than CSV_COLUMNS; the CSV is
            # the canonical export subset. Only carry columns that have a
            # value — an empty cell (a missing column, or a pre-this-feature
            # CSV with no llm_cost_usd column) is dropped so the model's typed
            # default applies. Forcing "" would break the non-string columns
            # (lead_score / seo_score / llm_cost_usd).
            payload = {col: row[col] for col in CSV_COLUMNS if (row.get(col) or "") != ""}
            try:
                out.append(ProspectRecord.model_validate(payload))
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("skipping malformed row %d: %s", row_num, exc)
    # Deduplicate by (prospect_id or phone); keep first occurrence.
    prospects = out
    seen: set[str] = set()
    deduped: list[ProspectRecord] = []
    dupes = 0
    for p in prospects:
        key = p.prospect_id or p.phone or ""
        if key and key in seen:
            dupes += 1
            continue
        if key:
            seen.add(key)
        deduped.append(p)
    if dupes:
        _LOG.warning("load_prospects_from_csv: dropped %d duplicate rows", dupes)
    return deduped


def _normalize_phone(raw: str) -> str | None:
    """Return an E.164-ish string ('+1XXXXXXXXXX') or None if unusable.

    Strip every non-digit, then:
      - 10 digits -> assume US, prefix '+1'
      - 11 digits starting with '1' -> prefix '+'
      - 12+ digits OR already-prefixed '+' -> trust the input
      - anything else -> None
    """
    if not raw:
        return None
    s = raw.strip()
    has_plus = s.startswith("+")
    digits = "".join(c for c in s if c.isdigit())
    if len(digits) < _MIN_PHONE_DIGITS:
        return None
    if has_plus:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) >= 11:
        return "+" + digits
    return None


# ---------------------------------------------------------------------------
# Priority sort + filter
# ---------------------------------------------------------------------------

_PRIORITY_RANK: dict[str, int] = {"hot": 0, "warm": 1, "low": 2}


def _safe_int(value: Any, *, default: int) -> int:
    try:
        if isinstance(value, str):
            value = value.strip()
            return int(float(value)) if value else default
        return int(value)
    except (TypeError, ValueError):
        return default


def _sort_prospects(prospects: Sequence[ProspectRecord]) -> list[ProspectRecord]:
    """Mirror prospecting/text_export.py sort: priority -> lead_score desc -> seo_score asc."""

    def key(p: ProspectRecord) -> tuple[int, int, int]:
        priority = (p.call_priority or "low").lower()
        return (
            _PRIORITY_RANK.get(priority, 99),
            -_safe_int(p.lead_score, default=0),
            _safe_int(p.seo_score, default=100),
        )

    return sorted(prospects, key=key)


# ---------------------------------------------------------------------------
# Audit + run-record persistence
# ---------------------------------------------------------------------------


def _audit_ledger() -> persistence.JsonlLedger:
    import os

    return persistence.JsonlLedger(
        os.getenv("SAMUS_VOICE_AUDIT_PATH", _AUDIT_PATH_DEFAULT),
    )


def _events_ledger() -> persistence.JsonlLedger:
    import os

    return persistence.JsonlLedger(
        os.getenv("SAMUS_VOICE_EVENTS_PATH", _EVENTS_PATH_DEFAULT),
    )


def _append_audit(ev: dict[str, Any]) -> None:
    try:
        _audit_ledger().append(ev)
    except OSError as exc:
        _LOG.warning("dialer audit append failed: %s", exc)


def _append_event(rec: dict[str, Any]) -> None:
    try:
        _events_ledger().append(rec)
    except OSError as exc:
        _LOG.warning("dialer event ledger append failed: %s", exc)


def _persist_run_record(result: DialRunResult) -> Path | None:
    """Write the run aggregate to <artifact_root>/voice/dial_runs/.

    Returns the written path, or None on failure (logs a warning).
    """
    try:
        target_dir = storage.root() / _DIAL_RUN_SUBDIR
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{result.run_id}.json"
        path.write_text(
            json.dumps(result.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return path
    except OSError as exc:
        _LOG.warning("dial run persist failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Per-prospect variableValues for Morgan's template
# ---------------------------------------------------------------------------


def _fill_callsheet_placeholders(text: str, *, rep_name: str, callback: str) -> str:
    """Substitute the human-callsheet placeholders before the text reaches Vapi.

    The prospecting templates carry ``[NAME]`` (the caller) and ``[PHONE]``
    (the callback number) — readable on a printed callsheet, but Vapi reads
    them VERBATIM, so the assistant says "this is name... call me at phone".
    Case-insensitive on the token, tolerant of the common ``[NAME]``/``[name]``
    variants. Returns the text unchanged when no placeholder is present.
    """
    if not text:
        return text
    import re as _re

    out = _re.sub(r"\[\s*name\s*\]", rep_name, text, flags=_re.IGNORECASE)
    out = _re.sub(r"\[\s*phone\s*\]", callback, out, flags=_re.IGNORECASE)
    return out


def _build_variable_values(p: ProspectRecord) -> dict[str, str]:
    """Project the ProspectRecord onto the variables Morgan's template reads.

    Vapi silently drops keys the assistant doesn't reference, so over-
    inclusion is harmless. Names mirror what an SDR script would naturally
    interpolate. Keep keys snake_case + ASCII so they're prompt-safe.

    The opener / voicemail / pitch get ``[NAME]`` + ``[PHONE]`` filled with
    the configured rep name + callback number so the assistant never speaks a
    raw placeholder (Gap-6 from the first live call).
    """
    settings = get_settings()
    rep = (getattr(settings, "samus_voice_rep_name", "") or "Morgan").strip()
    callback = (getattr(settings, "samus_voice_callback_number", "") or "(530) 418-5105").strip()

    def _fill(text: str | None) -> str:
        return _fill_callsheet_placeholders(text or "", rep_name=rep, callback=callback)

    values = {
        "company_name": p.company_name or "",
        "industry": p.industry or "",
        "city": p.city or "",
        "state": p.state or "",
        "website_url": p.website_url or "",
        "owner_name": p.owner_name or "",
        "lead_score": str(p.lead_score or ""),
        "seo_score": str(p.seo_score or ""),
        "callsheet_opener": _fill(p.callsheet_opener),
        "callsheet_pitch": _fill(p.callsheet_pitch),
        "callsheet_voicemail": _fill(p.callsheet_voicemail),
        "callsheet_offer": p.callsheet_offer or "",
        "callsheet_issues": p.callsheet_issues or "",
        "callsheet_objections": p.callsheet_objections or "",
        # The specific audit finding the gatekeeper-aware opener WITHHELDS —
        # Morgan speaks it only once the owner is confirmed (prompt Step 2).
        "callsheet_finding": p.callsheet_finding or "",
        # Expose the raw values too so an assistant prompt can reference
        # {{rep_name}} / {{callback_number}} directly if it prefers.
        "rep_name": rep,
        "callback_number": callback,
        # Recycle-pass context (2026-07-03): "true" + a compact CRM history
        # digest when this prospect has been through the pipeline before —
        # Morgan's prompt can open as a follow-up and reference the prior
        # exchange instead of introducing us cold. Empty for first-touch.
        "recycled": str(getattr(p, "recycled", "") or ""),
        "prior_touch_summary": str(getattr(p, "prior_touch_summary", "") or ""),
    }

    # Prior-call context (flag-gated, best-effort): give Morgan the operator's
    # last logged outcome + notes so the dial is INFORMED. build_prospect_context
    # never raises and returns all-empty keys when nothing is known, so a merge
    # is always safe; we still wrap defensively so a dial is never blocked.
    if getattr(settings, "samus_voice_profile_context_enabled", False):
        try:
            from backend.voice.prospect_profile import build_prospect_context

            values.update(build_prospect_context(p.prospect_id))
        except Exception:  # noqa: BLE001 — never let context lookup block a dial.
            pass

    return values


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def dial_call_list(
    csv_path: Path,
    config: DialerConfig,
    *,
    initiate_fn: Callable[[InitiateCallRequest, dict[str, str]], InitiateCallResult] | None = None,
    now: datetime | None = None,
) -> DialRunResult:
    """Walk the CSV and dial prospects per ``config``. Always returns a result.

    ``initiate_fn(request, variable_values)`` is the injection seam for tests
    and for routing through the live Vapi service in production. When None,
    we resolve the real ``initiate_call`` from service.py at first call (lazy
    import keeps the dialer importable from environments that lack the full
    Vapi client setup).

    ``now`` lets tests pin the clock for the TCPA gate; production passes None.
    """
    started = iso_now()
    run_id = "dial_run_" + started.replace(":", "").replace("-", "")

    if initiate_fn is None:
        # Use the typed service entrypoint so we get audit logging + degraded-
        # mode handling for free (vapi_api_key_unset etc).
        from .service import initiate_call as _real_initiate

        initiate_fn = _wrap_service_initiate(_real_initiate)

    settings = get_settings()
    assistant_id = (settings.vapi_assistant_id or "").strip()
    # Round-robin pool: config overrides the single env-var default.
    phone_number_pool = [p.strip() for p in config.phone_number_ids if p.strip()] or (
        [(settings.vapi_phone_number_id or "").strip()]
    )
    phone_number_id = phone_number_pool[0]  # preflight check uses first entry
    if not assistant_id or not phone_number_id:
        # Refuse to run rather than degrade silently — the dialer's whole
        # point is that calls land; if we can't dial, the operator needs to
        # know loudly. Persist a single 'failed_preflight' audit row.
        ev = events.build_audit_event(
            service="voice",
            task_id=run_id,
            action="dial_run_preflight",
            input_payload={"csv_path": str(csv_path)},
            output_payload={
                "error": "vapi_assistant_or_phone_unset",
                "assistant_id_present": bool(assistant_id),
                "phone_number_id_present": bool(phone_number_id),
            },
            status="failed",
        )
        _append_audit(ev)
        raise RuntimeError(
            "vapi_assistant_or_phone_unset: set Samus/VapiAssistantId and "
            "Samus/VapiPhoneNumberId via Set-HfSecret before running the dialer",
        )

    # Existence preflight: a NON-EMPTY phone_number_id can still be stale /
    # deleted (a number removed or re-imported in the Vapi dashboard changes
    # its id). Without this check, every prospect fails at dial time with a
    # cryptic ``phoneNumber <id> does not exist`` and the circuit breaker trips
    # after 3 wasted attempts. Query Vapi ONCE up front and fail fast with the
    # configured id + the list of ids that DO exist, so the operator can fix
    # the secret in one step. Fail-OPEN on a transport/list error — a Vapi
    # /phone-number outage must not block a run whose number is actually valid.
    missing = _validate_phone_numbers_exist(
        [p for p in phone_number_pool if p],
        settings,
    )
    if missing:
        ev = events.build_audit_event(
            service="voice",
            task_id=run_id,
            action="dial_run_preflight",
            input_payload={"csv_path": str(csv_path)},
            output_payload={
                "error": "vapi_phone_number_not_found",
                "configured_missing": missing.get("missing"),
                "available": missing.get("available"),
            },
            status="failed",
        )
        _append_audit(ev)
        raise RuntimeError(
            "vapi_phone_number_not_found: configured phone number id(s) "
            f"{missing.get('missing')} do not exist in the Vapi account. "
            f"Available numbers: {missing.get('available')}. "
            "Reseal Samus/VapiPhoneNumberId to a valid id and recreate samus-voice.",
        )

    prospects = load_prospects_from_csv(csv_path)

    # Warm-prospect exclusion — a prospect in an active buying_signal
    # enrollment is mid-conversation on a hotter track (a replied email, a
    # booked follow-up). Cold-dialing them crosses wires. This mirrors the
    # cold-list exclusion already applied to the human-readable
    # morning_call_list TXT in prospecting/text_export, using the SAME
    # single source of truth (active_warm_prospect_ids). Applied to the raw
    # prospect list BEFORE eligible/retry construction so a warm prospect
    # is removed from every downstream path. Each excluded prospect gets an
    # audit row so the operator sees WHY a name was skipped, not silently.
    warm_excluded = _filter_warm_prospects(prospects, run_id, started)
    warm_excluded_count = len(warm_excluded)
    if warm_excluded:
        excluded_ids = {(p.prospect_id or "") for p in warm_excluded}
        prospects = [p for p in prospects if (p.prospect_id or "") not in excluded_ids]

    # Scheduled-defer gate: honour a future-dated callback (operator noted
    # "closed until July 6th", or an auto-detected closure). The note distiller
    # routes such constraints into the callback queue; here we DON'T dial a
    # prospect before its callback_date. This is the fix for "I logged that the
    # office was closed but Samus dialed it anyway" — operator-captured timing
    # intel now actually gates the dial. (The complementary prepend below
    # re-surfaces the same callback once it's DUE.) Best-effort + fail-open.
    deferred_count = 0
    try:
        from .callback_queue import pending_future_ids

        future = pending_future_ids()
        if future:
            deferred = [p for p in prospects if (p.prospect_id or "") in future]
            for p in deferred:
                ev = events.build_audit_event(
                    service="voice",
                    task_id=run_id,
                    action="dial_skip_scheduled",
                    input_payload={"prospect_id": p.prospect_id, "company": p.company_name},
                    output_payload={
                        "reason": "callback_scheduled",
                        "callback_date": future.get(p.prospect_id or ""),
                    },
                    status="skipped",
                )
                _append_audit(ev)
            if deferred:
                deferred_count = len(deferred)
                _LOG.info(
                    "dial_run %s: deferring %d prospect(s) with future callbacks",
                    run_id,
                    deferred_count,
                )
                prospects = [p for p in prospects if (p.prospect_id or "") not in future]
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("dial_run %s: scheduled-defer gate skipped: %s", run_id, exc)

    eligible_set = set(config.only_priorities)
    eligible = [p for p in prospects if (p.call_priority or "").lower() in eligible_set]
    eligible_sorted = _sort_prospects(eligible)

    # Prepend retry candidates from previous no-answer/voicemail attempts.
    # They appear at the front of the list so they're dialed first, ahead of
    # fresh prospects, before hitting max_calls.
    try:
        from .retry import get_retry_entries

        retry_entries = get_retry_entries(max_age_hours=36)
        if retry_entries:
            _LOG.info("dial_run %s: prepending %d retry candidates", run_id, len(retry_entries))
    except Exception as exc:  # noqa: BLE001
        retry_entries = []
        _LOG.warning("dial_run %s: failed to load retry queue: %s", run_id, exc)

    # Build a lookup from the full eligible+ineligible list for retry matching.
    all_by_id: dict[str, ProspectRecord] = {
        (p.prospect_id or ""): p for p in prospects if p.prospect_id
    }
    retry_prospects: list[ProspectRecord] = []
    for entry in retry_entries:
        pid = entry.get("prospect_id") or ""
        if pid in all_by_id:
            retry_prospects.append(all_by_id[pid])

    # Dedup: don't add a retry record that's already in eligible_sorted.
    eligible_ids = {(p.prospect_id or "") for p in eligible_sorted}
    retry_prepend = [p for p in retry_prospects if (p.prospect_id or "") not in eligible_ids]
    eligible_sorted = retry_prepend + eligible_sorted

    # Set of prospect_ids that came from the retry queue — used below to clear
    # their entries after a successful initiated outcome.
    _retry_ids = {(p.prospect_id or "") for p in retry_prepend}

    # Date-anchored callbacks (deferred leads — e.g. a business that was closed
    # for a vacation and announced a reopen date). DUE callbacks (callback_date
    # <= today) are re-surfaced at the very front of the queue. A callback's
    # prospect may not be in TODAY's CSV (it was deferred from an earlier list),
    # so reconstruct a minimal record from the queued company/phone when absent.
    _callback_ids: set[str] = set()
    try:
        from .callback_queue import get_due_callbacks

        due = get_due_callbacks()
    except Exception as exc:  # noqa: BLE001
        due = []
        _LOG.warning("dial_run %s: failed to load callback queue: %s", run_id, exc)
    callback_prepend: list[ProspectRecord] = []
    already = {(p.prospect_id or "") for p in eligible_sorted}
    for cb in due:
        pid = str(cb.get("prospect_id") or "")
        if not pid or pid in already:
            continue
        rec = all_by_id.get(pid)
        if rec is None:
            try:
                rec = ProspectRecord(
                    prospect_id=pid,
                    company_name=str(cb.get("company") or ""),
                    phone=str(cb.get("phone") or ""),
                    call_priority="hot",  # a returning lead is worth front-of-line
                )
            except Exception:  # noqa: BLE001
                continue
        callback_prepend.append(rec)
        already.add(pid)
        _callback_ids.add(pid)
    if callback_prepend:
        _LOG.info("dial_run %s: prepending %d due callback(s)", run_id, len(callback_prepend))
        eligible_sorted = callback_prepend + eligible_sorted

    # Assign each PLACED call to a phone number ROUND-ROBIN over the pool,
    # keyed on the count of calls actually initiated so far (NOT the raw
    # eligible position). Contiguous-chunk assignment (the prior scheme) put
    # every early position on pool[0]; because prospects are skipped
    # (cooldown/hours/DNC/phone-valid) BEFORE placement and the batch cap is
    # hit inside pool[0]'s chunk, pool[1..] was never reached — every call in
    # the 2026-07-02 morning batch landed on one caller ID. Round-robin over
    # the placement index guarantees consecutive PLACED calls alternate
    # numbers (call 1 -> pool[0], call 2 -> pool[1], call 3 -> pool[0], ...),
    # exercising every line from the first two calls and spreading caller-ID
    # load evenly. Skips do NOT advance the rotation — only real placements do.
    _n_pool = len(phone_number_pool)

    def _assigned_number(placement_index: int) -> str:
        return phone_number_pool[placement_index % _n_pool]

    # T-5: Emit an audit row when the TCPA call-hours gate is disabled so the
    # operator has a paper trail for every run that bypassed it.
    _tcpa_disabled = config.call_hours_start == 0 and config.call_hours_end == 0
    if _tcpa_disabled:
        _LOG.warning(
            "dial_run %s: TCPA call-hours gate is DISABLED. "
            "Operator owns compliance for all calls in this run.",
            run_id,
        )
        _append_event(
            {
                "ts": started,
                "kind": "dial_run_tcpa_disabled",
                "run_id": run_id,
                "csv_path": str(csv_path),
            }
        )

    attempts: list[DialAttempt] = []
    initiated = 0
    # Round-robin cursor over phone_number_pool. Advances ONLY when a call is
    # actually placed (dry-run or real success) — skips/errors do not consume
    # a rotation slot, so a run that skips 13 of 15 still alternates the 15
    # placed calls across both numbers. See _assigned_number above.
    placement_index = 0
    errors = 0
    # Warm-excluded prospects (filtered above, before eligible-build) count
    # as skips in the run total so the briefing reflects the true picture.
    skipped = warm_excluded_count + deferred_count
    consecutive_vapi_errors = 0

    for _pos, p in enumerate(eligible_sorted):
        if initiated >= config.max_calls:
            break

        priority = (p.call_priority or "").lower()
        phone = _normalize_phone(p.phone)
        if phone is None:
            attempts.append(
                DialAttempt(
                    prospect_id=p.prospect_id or "",
                    company=p.company_name or "",
                    phone=p.phone or "",
                    state=p.state or "",
                    priority=priority,
                    outcome="skipped_phone",
                    detail=f"unusable phone: {p.phone!r}",
                    initiated_at=iso_now(),
                )
            )
            skipped += 1
            continue

        # DNC / suppression check (P0 — before TCPA logging)
        if _is_suppressed(phone):
            attempts.append(
                DialAttempt(
                    prospect_id=p.prospect_id or "",
                    company=p.company_name or "",
                    phone=phone,
                    state=p.state or "",
                    priority=priority,
                    outcome="skipped_suppressed",
                    detail="phone on DNC suppression list",
                    initiated_at=iso_now(),
                )
            )
            skipped += 1
            continue

        # Per-number cooldown (7-14 day floor). Subsumes the same-day check
        # when cooldown_days >= 1; the legacy _already_called_today path stays
        # as a safety net for cooldown_days == 0 (operator-disabled).
        # EXEMPTION: a same-day voicemail retry — when the retry queue holds
        # a voicemail entry for this prospect/phone within the exempt window
        # — bypasses the cooldown so the short-window retry can fire.
        cd_days = int(getattr(get_settings(), "samus_dialer_cooldown_days", 7) or 0)
        if cd_days > 0:
            in_cd, last_dial_date = _in_dial_cooldown(
                p.prospect_id or "",
                phone,
                cd_days,
            )
            if in_cd:
                vm_hours = int(
                    getattr(
                        get_settings(),
                        "samus_dialer_voicemail_exempt_hours",
                        36,
                    )
                    or 0
                )
                vm_exempt = False
                if vm_hours > 0:
                    from .retry import has_active_voicemail_entry  # noqa: PLC0415

                    vm_exempt = has_active_voicemail_entry(
                        prospect_id=p.prospect_id or "",
                        prospect_phone=phone,
                        max_age_hours=vm_hours,
                    )
                if not vm_exempt:
                    attempts.append(
                        DialAttempt(
                            prospect_id=p.prospect_id or "",
                            company=p.company_name or "",
                            phone=phone,
                            state=p.state or "",
                            priority=priority,
                            outcome="skipped_cooldown",
                            detail=(
                                f"dialled {last_dial_date}; {cd_days}d per-number cooldown active"
                            ),
                            initiated_at=iso_now(),
                        )
                    )
                    skipped += 1
                    continue

        # Per-day call cap: skip if this prospect was already initiated today.
        if _already_called_today(p.prospect_id or "", run_id):
            attempts.append(
                DialAttempt(
                    prospect_id=p.prospect_id or "",
                    company=p.company_name or "",
                    phone=phone,
                    state=p.state or "",
                    priority=priority,
                    outcome="skipped_already_called",
                    detail="already initiated today",
                    initiated_at=iso_now(),
                )
            )
            skipped += 1
            continue

        # TCPA gate (skip if either bound is non-default 0)
        gate_active = not (config.call_hours_start == 0 and config.call_hours_end == 0)
        if gate_active and not is_within_call_hours(
            state=p.state,
            now=now,
            hours=(config.call_hours_start, config.call_hours_end),
        ):
            tz_name = state_to_timezone(p.state).key
            attempts.append(
                DialAttempt(
                    prospect_id=p.prospect_id or "",
                    company=p.company_name or "",
                    phone=phone,
                    state=p.state or "",
                    priority=priority,
                    outcome="skipped_hours",
                    detail=(
                        f"outside {config.call_hours_start:02d}:00-{config.call_hours_end:02d}:00"
                        f" {tz_name} (state={p.state!r})"
                    ),
                    initiated_at=iso_now(),
                )
            )
            skipped += 1
            continue

        # Business-hours gate (flag-gated; fail-OPEN on None). Mirrors the
        # TCPA gate: prospect-local now via state_to_timezone(p.state).
        if getattr(get_settings(), "samus_dialer_business_hours_gate", False):
            bh_now = now if now is not None else datetime.now(timezone.utc)
            if bh_now.tzinfo is None:
                bh_now = bh_now.replace(tzinfo=timezone.utc)
            bh_local = bh_now.astimezone(state_to_timezone(p.state))
            if is_open_now(p.business_hours, now_local=bh_local) is False:
                attempts.append(
                    DialAttempt(
                        prospect_id=p.prospect_id or "",
                        company=p.company_name or "",
                        phone=phone,
                        state=p.state or "",
                        priority=priority,
                        outcome="skipped_closed",
                        detail=(
                            f"business closed at {bh_local:%a %H:%M} "
                            f"{state_to_timezone(p.state).key} (state={p.state!r})"
                        ),
                        initiated_at=iso_now(),
                    )
                )
                skipped += 1
                continue

        attempt_ts = iso_now()
        # Select the caller-ID for this placement via round-robin on the
        # placement cursor, then advance the cursor. Done here so dry-run and
        # real calls assign numbers identically, and so a placement (not a
        # skip) is what advances the rotation.
        outbound_number_id = _assigned_number(placement_index)
        placement_index += 1
        if config.dry_run:
            attempt = DialAttempt(
                prospect_id=p.prospect_id or "",
                company=p.company_name or "",
                phone=phone,
                state=p.state or "",
                priority=priority,
                outcome="dry_run",
                detail="dry_run=True; no Vapi call placed",
                outbound_number_id=outbound_number_id,
                initiated_at=attempt_ts,
            )
            attempts.append(attempt)
            _record_attempt(run_id, attempt)
            initiated += 1
            continue
        request = InitiateCallRequest(
            assistant_id=assistant_id,
            phone_number_id=outbound_number_id,
            customer_number=phone,
            customer_name=p.company_name or None,
            metadata={
                "prospect_id": p.prospect_id or "",
                "run_id": run_id,
                "priority": priority,
                "source": "morning_dialer",
                "phone_number_id": outbound_number_id,
                "prospect_phone": phone,
                "owner_name": p.owner_name or "",
                "offer_summary": p.callsheet_pitch or "",
                # Carried so the post-call remediate loop (wired-DORMANT, gated
                # by SAMUS_POSTCALL_REMEDIATE_ENABLED) can deliver the SEO
                # remediation deliverable back to this prospect without a
                # second CRM/DDB lookup from inside the container. Empty when
                # unknown — the loop fail-soft-skips delivery.
                "website_url": p.website_url or "",
                "owner_email": p.owner_email or "",
                "company_name": p.company_name or "",
            },
        )
        variable_values = _build_variable_values(p)
        try:
            result = initiate_fn(request, variable_values)
        except Exception as exc:  # noqa: BLE001
            attempt = DialAttempt(
                prospect_id=p.prospect_id or "",
                company=p.company_name or "",
                phone=phone,
                state=p.state or "",
                priority=priority,
                outcome="vapi_error",
                vapi_error=f"unexpected: {exc!r}",
                outbound_number_id=outbound_number_id,
                initiated_at=attempt_ts,
            )
            attempts.append(attempt)
            _record_attempt(run_id, attempt)
            errors += 1
            consecutive_vapi_errors += 1
            if consecutive_vapi_errors >= _VAPI_ABORT_THRESHOLD:
                _LOG.error(
                    "dial_run %s: aborting after %d consecutive Vapi errors — "
                    "possible Vapi outage or misconfigured assistant",
                    run_id,
                    consecutive_vapi_errors,
                )
                break
        else:
            if result.vapi_error:
                attempt = DialAttempt(
                    prospect_id=p.prospect_id or "",
                    company=p.company_name or "",
                    phone=phone,
                    state=p.state or "",
                    priority=priority,
                    outcome="vapi_error",
                    call_id=result.call_id or None,
                    vapi_error=result.vapi_error,
                    outbound_number_id=outbound_number_id,
                    initiated_at=attempt_ts,
                )
                attempts.append(attempt)
                _record_attempt(run_id, attempt)
                errors += 1
                consecutive_vapi_errors += 1
                if consecutive_vapi_errors >= _VAPI_ABORT_THRESHOLD:
                    _LOG.error(
                        "dial_run %s: aborting after %d consecutive Vapi errors — "
                        "possible Vapi outage or misconfigured assistant",
                        run_id,
                        consecutive_vapi_errors,
                    )
                    break
            else:
                attempt = DialAttempt(
                    prospect_id=p.prospect_id or "",
                    company=p.company_name or "",
                    phone=phone,
                    state=p.state or "",
                    priority=priority,
                    outcome="initiated",
                    call_id=result.call_id,
                    detail=result.status or "",
                    outbound_number_id=outbound_number_id,
                    initiated_at=attempt_ts,
                )
                attempts.append(attempt)
                _record_attempt(run_id, attempt)
                initiated += 1
                consecutive_vapi_errors = 0
                # If this was a retry candidate, clear it from the queue.
                if (p.prospect_id or "") in _retry_ids:
                    try:
                        from .retry import clear_entry as _ce

                        _ce(p.prospect_id)
                    except Exception:  # noqa: BLE001
                        pass
                # If this was a due callback, mark it done so it doesn't re-surface.
                if (p.prospect_id or "") in _callback_ids:
                    try:
                        from .callback_queue import mark_done as _md

                        _md(p.prospect_id)
                    except Exception:  # noqa: BLE001
                        pass

        # Inter-call pacing — only after a real attempt (not skipped/dry-run).
        if initiated < config.max_calls and config.delay_between_calls_s > 0:
            time.sleep(config.delay_between_calls_s)

    result = DialRunResult(
        run_id=run_id,
        ts=started,
        csv_path=str(csv_path),
        config=config,
        eligible_count=len(eligible_sorted),
        initiated_count=initiated,
        skipped_count=skipped,
        error_count=errors,
        attempts=attempts,
    )
    _persist_run_record(result)
    if config.auto_tune and result.initiated_count >= 5:
        try:
            from .tuner import tune_assistant

            settings = get_settings()
            tune_assistant(
                assistant_id=(settings.vapi_assistant_id or "").strip(),
                window_calls=20,
                min_calls=5,
                dry_run=False,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("post-run auto-tune failed (non-fatal): %s", exc)
    return result


def _wrap_service_initiate(real_initiate):
    """Adapter: service.initiate_call() takes only InitiateCallRequest; we
    also need to pass variable_values through to the Vapi client. Build a
    thin shim that uses the VapiClient directly when variable injection is
    requested, so the operator's per-call interpolation lands.
    """

    def _impl(request: InitiateCallRequest, variable_values: dict[str, str]) -> InitiateCallResult:
        # The path used today (service.initiate_call) audit-logs + handles
        # degraded mode but does NOT forward variable_values. For dialer
        # we bypass it and use the client directly, then audit-log here.
        from .client import VapiClient, VapiError

        settings = get_settings()
        key = (settings.vapi_api_key or "").strip()
        if not key:
            ev = events.build_audit_event(
                service="voice",
                task_id="dialer_initiate_call",
                action="initiate_call",
                input_payload={
                    "assistant_id": request.assistant_id,
                    "phone_number_id": request.phone_number_id,
                    "via": "dialer",
                },
                output_payload={"vapi_error": "vapi_api_key_unset"},
                status="degraded",
            )
            _append_audit(ev)
            return InitiateCallResult(
                call_id="",
                status=None,
                vapi_error="vapi_api_key_unset",
            )
        client = VapiClient(api_key=key)
        # Per-prospect voicemail: the callsheet pipeline pre-generates a
        # voicemail script enriched with the prospect's profile + sales angle
        # (``callsheet_voicemail``, threaded into variable_values by
        # _build_variable_values). Pass it as a per-call voicemailMessage
        # override so Morgan leaves THAT when Vapi detects voicemail, instead
        # of the assistant's static fallback. Empty (a pre-callsheet CSV row)
        # -> override omitted, the assistant's own voicemailMessage is used.
        voicemail_message = (variable_values or {}).get("callsheet_voicemail") or None
        # Per-prospect spoken opener (OUTBOUND only): the callsheet pipeline
        # pre-generates a hook-first, personalized opener (``callsheet_opener``,
        # already [NAME]/[PHONE]-filled by _build_variable_values). Pass it as a
        # per-call firstMessage override so Morgan SPEAKS that as the first line
        # instead of the shared assistant's static, generic firstMessage (which
        # is shared with the inbound receptionist and must stay untouched).
        # Empty (a pre-callsheet CSV row) -> override omitted, assistant default.
        first_message = (variable_values or {}).get("callsheet_opener") or None
        # Tranche 3: stamp the variant arm (assistant config identity/version)
        # onto the call so the transcript reward can credit the bandit arm
        # that placed it. Fail-soft — stamping never blocks a dial.
        try:
            from .arm_stamp import current_arm_id

            variant_arm_id = current_arm_id(request.assistant_id)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("arm stamp derivation failed: %s", exc)
            variant_arm_id = ""
        call_metadata = dict(request.metadata or {})
        if variant_arm_id:
            call_metadata["variant_arm_id"] = variant_arm_id
        try:
            call = client.create_call(
                assistant_id=request.assistant_id,
                phone_number_id=request.phone_number_id,
                customer_number=request.customer_number,
                customer_name=request.customer_name,
                metadata=call_metadata or None,
                variable_values=variable_values or None,
                voicemail_message=voicemail_message,
                first_message=first_message,
            )
        except VapiError as exc:
            ev = events.build_audit_event(
                service="voice",
                task_id="dialer_initiate_call",
                action="initiate_call",
                input_payload={
                    "assistant_id": request.assistant_id,
                    "phone_number_id": request.phone_number_id,
                    "customer_number_tail": request.customer_number[-4:],
                    "via": "dialer",
                },
                output_payload={"vapi_error": str(exc)},
                status="failed",
            )
            _append_audit(ev)
            return InitiateCallResult(
                call_id="",
                status=None,
                vapi_error=str(exc),
            )
        if variant_arm_id:
            try:
                from .arm_stamp import record_dispatch

                record_dispatch(
                    call_id=call.id,
                    prospect_id=(request.metadata or {}).get("prospect_id"),
                    phone=request.customer_number,
                    arm_id=variant_arm_id,
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("arm dispatch record failed: %s", exc)
        ev = events.build_audit_event(
            service="voice",
            task_id=call.id,
            action="initiate_call",
            input_payload={
                "assistant_id": request.assistant_id,
                "phone_number_id": request.phone_number_id,
                "customer_number_tail": request.customer_number[-4:],
                "via": "dialer",
                "variable_values_keys": sorted((variable_values or {}).keys()),
                "custom_voicemail": bool(voicemail_message),
                "variant_arm_id": variant_arm_id,
            },
            output_payload={"call_id": call.id, "status": call.status},
            status="completed",
        )
        _append_audit(ev)
        return InitiateCallResult(
            call_id=call.id,
            status=call.status,
            vapi_error=None,
        )

    return _impl


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: ``python -m backend.voice.dialer [--live] [--max N] ...``.

    Defaults to dry_run + max_calls=5 — the operator must pass --live to
    actually place calls. Exit code 0 on success, 2 on preflight failure,
    1 on dial-time errors (some calls succeeded, some hit Vapi 5xx, etc.).
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Walk today's prospect CSV and place Vapi calls in priority order",
    )
    parser.add_argument(
        "--csv", type=str, default=None, help="path to call_list_YYYY-MM-DD.csv (default: today's)"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="actually place calls (default: dry-run, no calls placed)",
    )
    parser.add_argument(
        "--max", type=int, default=5, help="max calls per run (default 5, hard ceiling 50)"
    )
    parser.add_argument(
        "--delay", type=float, default=30.0, help="seconds between calls (default 30)"
    )
    parser.add_argument(
        "--priority",
        action="append",
        choices=("hot", "warm", "low"),
        default=None,
        help="priority bucket to include (repeatable; default: hot+warm)",
    )
    parser.add_argument("--start-hour", type=int, default=8, help="TCPA start hour (default 8)")
    parser.add_argument(
        "--end-hour", type=int, default=21, help="TCPA end hour exclusive (default 21)"
    )
    parser.add_argument(
        "--disable-tcpa", action="store_true", help="disable TCPA hours gate (operator owns risk)"
    )
    parser.add_argument(
        "--phone-number-ids",
        type=str,
        default=None,
        help="comma-separated Vapi phone_number_ids to rotate (default: VAPI_PHONE_NUMBER_ID)",
    )
    args = parser.parse_args(argv)

    csv_path = Path(args.csv) if args.csv else default_csv_path_for_today()
    if not csv_path.exists():
        print(f"ERROR: call list CSV not found at {csv_path}", flush=True)
        return 2

    phone_number_ids = (
        [p.strip() for p in args.phone_number_ids.split(",") if p.strip()]
        if args.phone_number_ids
        else []
    )
    config = DialerConfig(
        max_calls=args.max,
        delay_between_calls_s=args.delay,
        dry_run=not args.live,
        only_priorities=args.priority or ["hot", "warm"],
        call_hours_start=0 if args.disable_tcpa else args.start_hour,
        call_hours_end=0 if args.disable_tcpa else args.end_hour,
        phone_number_ids=phone_number_ids,
    )
    pool_display = f"{len(phone_number_ids)} numbers" if phone_number_ids else "single (env)"
    print(
        f"==> Dialer run: csv={csv_path.name} dry_run={config.dry_run} "
        f"max={config.max_calls} priorities={config.only_priorities} pool={pool_display}",
        flush=True,
    )

    try:
        result = dial_call_list(csv_path, config)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", flush=True)
        return 2
    except RuntimeError as exc:
        print(f"ERROR: {exc}", flush=True)
        return 2

    print(
        f"==> Eligible: {result.eligible_count}  "
        f"Initiated: {result.initiated_count}  "
        f"Skipped: {result.skipped_count}  "
        f"Errors: {result.error_count}",
        flush=True,
    )
    for a in result.attempts:
        tag = a.outcome.upper()
        ext = ""
        if a.call_id:
            ext = f" call_id={a.call_id}"
        elif a.vapi_error:
            ext = f" err={a.vapi_error}"
        elif a.detail:
            ext = f" ({a.detail})"
        print(f"   [{tag:14s}] {a.company[:32]:32s} {a.phone}{ext}", flush=True)
    return 0 if result.error_count == 0 else 1


def _dial_attempt_audit_event(run_id: str, attempt: DialAttempt) -> dict[str, Any]:
    """Build the dial_attempt audit row shape."""
    return events.build_audit_event(
        service="voice",
        task_id=attempt.call_id or f"{run_id}:{attempt.prospect_id}",
        action="dial_attempt",
        input_payload={
            "run_id": run_id,
            "prospect_id": attempt.prospect_id,
            "company": attempt.company,
            "phone_tail": attempt.phone[-4:] if attempt.phone else "",
            "state": attempt.state,
            "priority": attempt.priority,
            "outbound_number_id": attempt.outbound_number_id,
        },
        output_payload={
            "outcome": attempt.outcome,
            "call_id": attempt.call_id,
            "vapi_error": attempt.vapi_error,
            "detail": attempt.detail,
        },
        status="completed" if attempt.outcome in ("initiated", "dry_run") else "degraded",
    )


def _record_attempt(run_id: str, attempt: DialAttempt) -> None:
    """Persist a single attempt to BOTH the audit ledger (hashed) and the
    rich-payload events ledger (for the briefing rollup)."""
    _append_audit(_dial_attempt_audit_event(run_id, attempt))
    _append_event(
        {
            "ts": attempt.initiated_at,
            "kind": "dial_attempt",
            "run_id": run_id,
            "prospect_id": attempt.prospect_id,
            "company": attempt.company,
            "phone_tail": attempt.phone[-4:] if attempt.phone else "",
            "state": attempt.state,
            "priority": attempt.priority,
            "outcome": attempt.outcome,
            "call_id": attempt.call_id,
            "vapi_error": attempt.vapi_error,
            "detail": attempt.detail,
            "outbound_number_id": attempt.outbound_number_id,
        }
    )


if __name__ == "__main__":
    import sys

    sys.exit(main())
