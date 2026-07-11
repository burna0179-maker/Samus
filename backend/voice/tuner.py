"""Post-batch voice tuner — analyse recent call transcripts and propose/apply
bounded changes to the Vapi assistant configuration.

Called automatically at the end of a dial run (when DialerConfig.auto_tune is
True and at least 5 calls were initiated) and exposed as POST /voice/tune for
on-demand operator use.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from backend.common import events, persistence
from backend.common.config import get_settings
from backend.common.dates import iso_now
from backend.common.llm_client import anthropic_messages

from .models import TuneChange, TuneResult


_LOG = logging.getLogger("samus.voice.tuner")

_AUDIT_PATH_DEFAULT = "/opt/samus/data/voice/voice_audit.jsonl"

_ALLOWED_FIELDS = frozenset(
    {"system_prompt", "first_message", "voice_speed", "voice_similarity_boost"}
)
_VOICE_SPEED_RANGE = (0.7, 1.1)
_VOICE_SIMILARITY_RANGE = (0.5, 1.0)
_MAX_PROMPT_GROWTH = 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _audit_ledger() -> persistence.JsonlLedger:
    return persistence.JsonlLedger(
        os.getenv("SAMUS_VOICE_AUDIT_PATH", _AUDIT_PATH_DEFAULT),
    )


def _append_audit(ev: dict[str, Any]) -> None:
    try:
        _audit_ledger().append(ev)
    except OSError as exc:
        _LOG.warning("tuner audit append failed: %s", exc)


def _extract_system_prompt(assistant_raw: dict[str, Any]) -> str:
    """Pull the system prompt from a raw Vapi assistant dict."""
    try:
        messages = assistant_raw.get("model", {}).get("messages", [])
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "system":
                return str(msg.get("content") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _build_call_digest(calls: list[Any]) -> list[dict[str, Any]]:
    """Build a compact per-call summary for the Claude prompt."""
    rows = []
    for call in calls:
        transcript_raw = call.transcript or ""
        rows.append({
            "call_id": call.id,
            "ended_reason": call.endedReason or "",
            "duration_sec": (
                _duration_seconds(call.startedAt, call.endedAt)
                if call.startedAt and call.endedAt
                else None
            ),
            "summary": (call.summary or "")[:400],
            "transcript_snippet": transcript_raw[:600],
        })
    return rows


def _duration_seconds(started: str, ended: str) -> int | None:
    from datetime import datetime, timezone
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
    fmt2 = "%Y-%m-%dT%H:%M:%SZ"
    for f in (fmt, fmt2):
        try:
            s = datetime.strptime(started, f).replace(tzinfo=timezone.utc)
            e = datetime.strptime(ended, f).replace(tzinfo=timezone.utc)
            return max(0, int((e - s).total_seconds()))
        except ValueError:
            continue
    return None


_TUNER_SYSTEM = (
    "You are a voice AI tuning assistant. You analyse sales call data and "
    "propose specific, bounded changes to a Vapi voice assistant configuration. "
    "You return ONLY valid JSON matching the schema provided — no prose outside "
    "the JSON object."
)

_TUNER_PROMPT_TEMPLATE = """\
Current assistant configuration
================================
First message:
{first_message}

System prompt (read-only reference — changes must be in the JSON response):
{system_prompt}

Recent calls digest ({n_calls} calls)
======================================
{calls_json}

Task
====
Analyse the calls and propose specific, bounded improvements. Focus on:
- Patterns in ended_reason (e.g. repeated hang-ups at the same point)
- Summary-level themes (e.g. objection patterns, confusion points)
- Pacing / engagement cues from transcript snippets

Return EXACTLY this JSON structure (no other text):
{{
  "observations": ["<string>", ...],
  "changes": [
    {{
      "field": "system_prompt|first_message|voice_speed|voice_similarity_boost",
      "reason": "<string>",
      "old_value": "<string>",
      "new_value": "<string>"
    }}
  ],
  "skip_reason": null
}}

Constraints:
- Only propose changes to the allowed fields listed above.
- For system_prompt / first_message: new_value must not exceed old_value length + 500 chars.
- For voice_speed: float in [{speed_min}, {speed_max}].
- For voice_similarity_boost: float in [{sim_min}, {sim_max}].
- If no changes are warranted, set changes to [] and populate skip_reason.
- Propose at most 3 changes total.
"""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_change(
    change: TuneChange,
    *,
    current_system_prompt: str,
    current_first_message: str,
) -> str | None:
    """Return a rejection reason string, or None if the change is valid."""
    if change.field not in _ALLOWED_FIELDS:
        return f"field '{change.field}' not in allowed set"

    if change.field == "system_prompt":
        max_len = len(current_system_prompt) + _MAX_PROMPT_GROWTH
        if len(change.new_value) > max_len:
            return (
                f"new system_prompt length {len(change.new_value)} exceeds "
                f"allowed max {max_len}"
            )

    if change.field == "first_message":
        max_len = len(current_first_message) + _MAX_PROMPT_GROWTH
        if len(change.new_value) > max_len:
            return (
                f"new first_message length {len(change.new_value)} exceeds "
                f"allowed max {max_len}"
            )

    if change.field == "voice_speed":
        try:
            v = float(change.new_value)
        except ValueError:
            return f"voice_speed new_value '{change.new_value}' is not a float"
        lo, hi = _VOICE_SPEED_RANGE
        if not (lo <= v <= hi):
            return f"voice_speed {v} outside [{lo}, {hi}]"

    if change.field == "voice_similarity_boost":
        try:
            v = float(change.new_value)
        except ValueError:
            return f"voice_similarity_boost new_value '{change.new_value}' is not a float"
        lo, hi = _VOICE_SIMILARITY_RANGE
        if not (lo <= v <= hi):
            return f"voice_similarity_boost {v} outside [{lo}, {hi}]"

    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def tune_assistant(
    *,
    assistant_id: str,
    window_calls: int = 20,
    min_calls: int = 5,
    dry_run: bool = True,
    events_path: str | None = None,  # unused today; kept for test override parity
) -> TuneResult:
    """Analyse recent Vapi calls and optionally patch the assistant.

    Never raises — all exceptions are caught and surfaced in TuneResult error
    fields so the dialer's auto-tune hook stays non-fatal.
    """
    ts = iso_now()

    def _empty(*, skip: str | None = None, llm_err: str | None = None,
                vapi_err: str | None = None) -> TuneResult:
        return TuneResult(
            assistant_id=assistant_id,
            ts=ts,
            calls_analyzed=0,
            observations=[],
            changes_proposed=[],
            changes_applied=0,
            changes_rejected=0,
            dry_run=dry_run,
            skip_reason=skip,
            llm_error=llm_err,
            vapi_error=vapi_err,
        )

    if not assistant_id:
        return _empty(skip="assistant_id is empty")

    settings = get_settings()
    vapi_key = (settings.vapi_api_key or "").strip()
    if not vapi_key:
        return _empty(vapi_err="vapi_api_key_unset")

    from .client import VapiClient, VapiError
    client = VapiClient(api_key=vapi_key)

    # --- 1. Fetch recent calls from Vapi -----------------------------------
    try:
        raw_calls = client.list_calls(limit=window_calls)
    except VapiError as exc:
        return _empty(vapi_err=f"list_calls failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        return _empty(vapi_err=f"list_calls unexpected: {exc}")

    calls_with_transcript = [c for c in raw_calls if c.transcript]
    if len(calls_with_transcript) < min_calls:
        return _empty(
            skip=(
                f"only {len(calls_with_transcript)} calls with transcripts "
                f"(min {min_calls})"
            ),
        )

    # --- 2. Fetch current assistant config ---------------------------------
    try:
        assistant_raw = client.get_assistant(assistant_id)
    except VapiError as exc:
        return _empty(vapi_err=f"get_assistant failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        return _empty(vapi_err=f"get_assistant unexpected: {exc}")

    current_system_prompt = _extract_system_prompt(assistant_raw)
    current_first_message = str(assistant_raw.get("firstMessage") or "")

    # --- 3. Build Claude prompt --------------------------------------------
    digest = _build_call_digest(calls_with_transcript)
    prompt = _TUNER_PROMPT_TEMPLATE.format(
        first_message=current_first_message or "(not set)",
        system_prompt=(current_system_prompt or "(not set)")[:2000],
        n_calls=len(digest),
        calls_json=json.dumps(digest, indent=2),
        speed_min=_VOICE_SPEED_RANGE[0],
        speed_max=_VOICE_SPEED_RANGE[1],
        sim_min=_VOICE_SIMILARITY_RANGE[0],
        sim_max=_VOICE_SIMILARITY_RANGE[1],
    )

    # --- 4. Call Claude ----------------------------------------------------
    try:
        text, _usage = anthropic_messages(
            workcell="voice",
            api_key="unused",
            prompt=prompt,
            system=_TUNER_SYSTEM,
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            allow_expensive_model=False,
            cache_system=True,
        )
    except Exception as exc:  # noqa: BLE001
        return _empty(llm_err=f"llm_call failed: {exc}")

    # --- 5. Parse + validate Claude response -------------------------------
    try:
        # Claude may wrap JSON in markdown fences
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            cleaned = "\n".join(
                ln for ln in lines if not ln.startswith("```")
            ).strip()
        llm_data = json.loads(cleaned)
    except (ValueError, AttributeError) as exc:
        return _empty(llm_err=f"llm_json_parse failed: {exc}; raw={text[:200]!r}")

    observations: list[str] = list(llm_data.get("observations") or [])
    skip_reason: str | None = llm_data.get("skip_reason") or None
    raw_changes: list[dict] = list(llm_data.get("changes") or [])

    proposed: list[TuneChange] = []
    for rc in raw_changes[:3]:  # hard cap at 3 changes
        try:
            change = TuneChange(
                field=str(rc.get("field") or ""),
                reason=str(rc.get("reason") or ""),
                old_value=str(rc.get("old_value") or ""),
                new_value=str(rc.get("new_value") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("tuner: skipping unparseable change %r: %s", rc, exc)
            continue
        rejection = _validate_change(
            change,
            current_system_prompt=current_system_prompt,
            current_first_message=current_first_message,
        )
        if rejection:
            change.rejected_reason = rejection
            _LOG.info("tuner: rejected change field=%s reason=%s", change.field, rejection)
        proposed.append(change)

    # --- 6-7. Apply changes (if not dry_run) --------------------------------
    applied = 0
    rejected = sum(1 for c in proposed if c.rejected_reason)

    if not dry_run:
        patch_kwargs: dict[str, Any] = {}
        for change in proposed:
            if change.rejected_reason:
                continue
            if change.field == "system_prompt":
                patch_kwargs["system_prompt"] = change.new_value
            elif change.field == "first_message":
                patch_kwargs["first_message"] = change.new_value
            elif change.field == "voice_speed":
                patch_kwargs["voice_speed"] = float(change.new_value)
            elif change.field == "voice_similarity_boost":
                patch_kwargs["voice_similarity_boost"] = float(change.new_value)

        if patch_kwargs:
            try:
                client.patch_assistant_config(assistant_id, **patch_kwargs)
                for change in proposed:
                    if not change.rejected_reason and change.field in {
                        "system_prompt", "first_message", "voice_speed",
                        "voice_similarity_boost",
                    }:
                        change.applied = True
                        applied += 1
            except VapiError as exc:
                vapi_err_str = f"patch_assistant_config failed: {exc}"
                _LOG.warning("tuner: %s", vapi_err_str)
                result = TuneResult(
                    assistant_id=assistant_id,
                    ts=ts,
                    calls_analyzed=len(calls_with_transcript),
                    observations=observations,
                    changes_proposed=proposed,
                    changes_applied=0,
                    changes_rejected=rejected,
                    dry_run=dry_run,
                    skip_reason=skip_reason,
                    vapi_error=vapi_err_str,
                )
                _append_audit(_build_audit_row(result))
                return result
            except Exception as exc:  # noqa: BLE001
                vapi_err_str = f"patch_assistant_config unexpected: {exc}"
                _LOG.warning("tuner: %s", vapi_err_str)
                result = TuneResult(
                    assistant_id=assistant_id,
                    ts=ts,
                    calls_analyzed=len(calls_with_transcript),
                    observations=observations,
                    changes_proposed=proposed,
                    changes_applied=0,
                    changes_rejected=rejected,
                    dry_run=dry_run,
                    skip_reason=skip_reason,
                    vapi_error=vapi_err_str,
                )
                _append_audit(_build_audit_row(result))
                return result
    else:
        _LOG.info(
            "tuner: dry_run=True; %d proposed changes not applied",
            len([c for c in proposed if not c.rejected_reason]),
        )

    # --- 8. Audit row --------------------------------------------------------
    result = TuneResult(
        assistant_id=assistant_id,
        ts=ts,
        calls_analyzed=len(calls_with_transcript),
        observations=observations,
        changes_proposed=proposed,
        changes_applied=applied,
        changes_rejected=rejected,
        dry_run=dry_run,
        skip_reason=skip_reason,
    )
    _append_audit(_build_audit_row(result))
    return result


def _build_audit_row(result: TuneResult) -> dict[str, Any]:
    return events.build_audit_event(
        service="voice",
        task_id=f"tune_{result.assistant_id}_{result.ts}",
        action="tune_assistant",
        input_payload={
            "assistant_id": result.assistant_id,
            "calls_analyzed": result.calls_analyzed,
            "dry_run": result.dry_run,
        },
        output_payload={
            "observations": result.observations,
            "changes_proposed": len(result.changes_proposed),
            "changes_applied": result.changes_applied,
            "changes_rejected": result.changes_rejected,
            "skip_reason": result.skip_reason,
            "llm_error": result.llm_error,
            "vapi_error": result.vapi_error,
        },
        status=(
            "completed" if not result.llm_error and not result.vapi_error
            else "degraded"
        ),
    )
