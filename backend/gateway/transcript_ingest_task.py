"""Gateway-internal transcript-ingest cadence — close the voice bandit learning loop.

Design rationale (operator directive; bandit loop dead since 2026-07-03):
  The dialer stamps a ``variant_arm_id`` on every placed call (arm_stamp),
  and the transcript analyzer flows a [0,1] reward to the UCB1 bandit via
  ``record_outcome``.  But the MIDDLE leg — pulling Vapi transcripts for
  completed calls and running them through the analyzer — was driven by a
  host cron that was removed 2026-07-03 with nothing in-container replacing
  it.  Result: calls complete, webhooks record ``end_of_call``, but
  transcripts are never analyzed and reward never flows — the bandit never
  learns.

  This module is the in-container replacement (same shape as
  ``control_tick_task`` / ``cold_dial_task``): a gateway lifespan loop that,
  on a configurable cadence (default 20 min), performs two steps:

    1. **Reconcile** — run ``reconcile_recent_calls()`` to backfill any
       ``end_of_call`` events whose webhook was dropped by Vapi.
    2. **Analyze** — find today's completed calls (from voice_events +
       dial_runs) whose transcripts have not yet been analyzed, fetch the
       full Vapi call object (transcript text), build a ``RawTranscript``,
       and run ``analyze_transcript()`` — which persists the analysis AND
       calls ``_flow_reward_to_bandit()`` to credit the arm.

  After a pass, the cadence updates the ``transcript_manifest.json``
  timestamp so staleness is externally observable.

Gates (all fail toward NOT analyzing — analysis is never load-bearing):
  1. loop enabled      — ``SAMUS_TRANSCRIPT_INGEST_ENABLED`` (default ON)
  2. Vapi key present  — ``settings.vapi_api_key`` must be set

Kill switches / knobs:
  * ``SAMUS_TRANSCRIPT_INGEST_ENABLED``     — master arm (default ON)
  * ``SAMUS_TRANSCRIPT_INGEST_INTERVAL_SEC``— cadence (default 1200 = 20 min)
  * ``SAMUS_TRANSCRIPT_INGEST_MAX_PER_PASS``— cap per pass (default 20)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

_LOG = logging.getLogger("samus.gateway.transcript_ingest_task")

_DEFAULT_INTERVAL_SEC = 1200.0
_INITIAL_DELAY_SEC = 300.0  # let boot + morning ritual + first control tick settle

ENV_ENABLED = "SAMUS_TRANSCRIPT_INGEST_ENABLED"
ENV_INTERVAL = "SAMUS_TRANSCRIPT_INGEST_INTERVAL_SEC"
ENV_MAX_PER_PASS = "SAMUS_TRANSCRIPT_INGEST_MAX_PER_PASS"

_DEFAULT_MAX_PER_PASS = 20


# ---------------------------------------------------------------------------
# Env helpers (same idiom as cold_dial_task / control_tick_task)
# ---------------------------------------------------------------------------


def _flag_on(name: str, default_on: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default_on
    return raw not in ("0", "false", "no", "off")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Vapi call fetch (injectable for tests)
# ---------------------------------------------------------------------------


def _default_fetch_call(call_id: str) -> dict[str, Any] | None:
    """Fetch a single Vapi call by id. Returns None on any error."""
    try:
        from backend.common.config import get_settings

        api_key = (get_settings().vapi_api_key or "").strip()
    except Exception:  # noqa: BLE001
        return None
    if not api_key:
        return None
    try:
        import httpx

        resp = httpx.get(
            f"https://api.vapi.ai/call/{call_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20.0,
        )
        if resp.status_code != 200:
            _LOG.warning("transcript_ingest: Vapi call %s HTTP %s", call_id, resp.status_code)
            return None
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("transcript_ingest: Vapi fetch %s failed: %s", call_id, exc)
        return None


# ---------------------------------------------------------------------------
# Core: find completed calls, analyze their transcripts, flow reward
# ---------------------------------------------------------------------------


def _utc_today() -> date:
    """Today's calendar date in UTC.

    The write side stamps every artifact in UTC — ``end_of_call`` events carry
    an ``iso_now()`` ``ts`` and dial-run files are named from an ``iso_now()``
    ``run_id`` (both ``...Z``). Filtering "today" with ``date.today()`` (the
    host's LOCAL date) therefore silently discovers ZERO calls on any host whose
    local date differs from UTC — the whole voice-bandit learning loop this
    module exists to keep alive goes dark for part of every day. Match the write
    side: bucket by the UTC calendar date.
    """
    return datetime.now(timezone.utc).date()


def _read_today_end_of_call_ids() -> dict[str, dict[str, Any]]:
    """Return {call_id: event_dict} for today's end_of_call events."""
    events_path = Path(
        os.getenv(
            "SAMUS_VOICE_EVENTS_PATH",
            "/opt/samus/data/voice/voice_events.jsonl",
        )
    )
    if not events_path.exists():
        return {}
    today = _utc_today().isoformat()
    out: dict[str, dict[str, Any]] = {}
    try:
        for line in events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") != "end_of_call":
                continue
            if not str(rec.get("ts", "")).startswith(today):
                continue
            cid = str(rec.get("call_id") or "")
            if cid:
                out[cid] = rec
    except OSError as exc:
        _LOG.warning("transcript_ingest: events read failed: %s", exc)
    return out


def _read_today_dial_run_call_ids() -> dict[str, dict[str, Any]]:
    """Return {call_id: {prospect_id, company, phone}} from today's dial runs."""
    try:
        from backend.common import storage

        runs_dir = storage.root() / "voice" / "dial_runs"
    except Exception:  # noqa: BLE001
        return {}
    if not runs_dir.exists():
        return {}
    today_str = _utc_today().strftime("%Y%m%d")
    out: dict[str, dict[str, Any]] = {}
    try:
        files = sorted(runs_dir.glob(f"dial_run_{today_str}*.json"))
    except OSError:
        return {}
    for fp in files:
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for att in data.get("attempts") or []:
            if not isinstance(att, dict):
                continue
            if att.get("outcome") != "initiated":
                continue
            cid = str(att.get("call_id") or "")
            if cid:
                out[cid] = {
                    "prospect_id": att.get("prospect_id"),
                    "company": att.get("company"),
                    "phone": att.get("phone"),
                }
    return out


def _already_analyzed_hashes() -> set[str]:
    """File hashes of already-persisted transcript analyses."""
    try:
        from backend.common import storage

        analyses_dir = storage.root() / "voice" / "analyses"
    except Exception:  # noqa: BLE001
        return set()
    if not analyses_dir.exists():
        return set()
    return {p.stem for p in analyses_dir.glob("*.json")}


def _vapi_file_hash(call_id: str, transcript: str) -> str:
    """Deterministic hash for a Vapi transcript, keyed on call_id + content."""
    content = f"vapi:{call_id}:{transcript}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _update_manifest() -> None:
    """Touch the transcript manifest so staleness is externally observable."""
    try:
        from backend.common import storage
        from backend.common.dates import iso_now

        manifest_dir = storage.root() / "voice"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest = manifest_dir / "transcript_manifest.json"
        data: dict[str, Any] = {}
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                data = {}
        data["last_ingest_pass"] = iso_now()
        data["source"] = "transcript_ingest_task"
        manifest.write_text(
            json.dumps(data, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        _LOG.warning("transcript_ingest: manifest update failed: %s", exc)


def run_ingest_pass(
    *,
    fetch_call: Callable[[str], dict[str, Any] | None] | None = None,
    skip_reconcile: bool = False,
) -> dict[str, Any]:
    """One transcript-ingest pass: reconcile -> find -> analyze -> reward.

    Synchronous. Never raises. ``fetch_call`` is an injection seam for tests.
    ``skip_reconcile`` skips the reconcile step (for tests that pre-populate).
    """
    from backend.common.dates import iso_now

    fetch = fetch_call or _default_fetch_call
    summary: dict[str, Any] = {
        "ts": iso_now(),
        "reconciled": 0,
        "candidates": 0,
        "analyzed": 0,
        "reward_flowed": 0,
        "skipped_already_analyzed": 0,
        "skipped_no_transcript": 0,
        "skipped_not_ended": 0,
        "fetch_failed": 0,
        "errors": [],
    }

    # Step 1: reconcile (backfill missing end_of_call events)
    if not skip_reconcile:
        try:
            from backend.voice.reconcile import reconcile_recent_calls

            rec_result = reconcile_recent_calls(fetch_call=fetch)
            summary["reconciled"] = rec_result.get("reconciled", 0)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("transcript_ingest: reconcile failed: %s", exc)
            summary["errors"].append(f"reconcile_failed: {exc}")

    # Step 2: collect candidates — end_of_call events + dial_run initiated calls
    eoc_calls = _read_today_end_of_call_ids()
    dial_calls = _read_today_dial_run_call_ids()
    all_call_ids = set(eoc_calls.keys()) | set(dial_calls.keys())
    summary["candidates"] = len(all_call_ids)

    if not all_call_ids:
        _update_manifest()
        return summary

    # Step 3: filter out already-analyzed
    analyzed = _already_analyzed_hashes()
    max_per_pass = _int_env(ENV_MAX_PER_PASS, _DEFAULT_MAX_PER_PASS)
    to_process: list[str] = []

    for cid in all_call_ids:
        if len(to_process) >= max_per_pass:
            break
        # We don't know the hash yet (need transcript), but we can check
        # if ANY analysis file references this call_id by a quick hash probe.
        # The definitive dedup happens after fetch.
        to_process.append(cid)

    # Step 4: fetch + analyze each
    for cid in to_process:
        try:
            call = fetch(cid)
            if call is None:
                summary["fetch_failed"] += 1
                continue

            status = str(call.get("status") or "")
            if status != "ended":
                summary["skipped_not_ended"] += 1
                continue

            transcript_text = str(call.get("transcript") or "")
            if not transcript_text.strip():
                summary["skipped_no_transcript"] += 1
                continue

            file_hash = _vapi_file_hash(cid, transcript_text)
            if file_hash in analyzed:
                summary["skipped_already_analyzed"] += 1
                continue

            # Build context from dial_run or end_of_call event metadata
            ctx = dial_calls.get(cid) or eoc_calls.get(cid) or {}
            prospect_id = str(ctx.get("prospect_id") or "")
            company = str(ctx.get("company") or "")
            phone = str(ctx.get("phone") or call.get("customer", {}).get("number", ""))

            started_at = call.get("startedAt") or call.get("createdAt") or ""
            call_ts = _parse_vapi_ts(started_at)

            # Build a RawTranscript from the Vapi call data
            from backend.voice.transcript_ingest import RawTranscript

            raw = RawTranscript(
                source_file=f"vapi_{cid}",
                file_hash=file_hash,
                call_ts=call_ts,
                direction="Outgoing",
                contact_phone=phone,
                contact_name=company,
                raw_text=transcript_text,
                turns=[],
                parse_format="vapi_api",
            )

            # Build a minimal prospect-like object for the analyzer
            prospect = _MinimalProspect(
                prospect_id=prospect_id,
                company_name=company,
                phone=phone,
            )

            from backend.voice.transcript_analyzer import analyze_transcript

            analysis = analyze_transcript(raw, prospect=prospect)

            summary["analyzed"] += 1
            analyzed.add(file_hash)

            if analysis.llm_error:
                summary["errors"].append(f"llm_error [{cid}]: {analysis.llm_error}")
            else:
                # _flow_reward_to_bandit runs inside analyze_transcript;
                # check if it credited an arm by looking at the reward
                if analysis.reward > 0.0:
                    summary["reward_flowed"] += 1

        except Exception as exc:  # noqa: BLE001
            _LOG.warning("transcript_ingest: analysis failed for %s: %s", cid, exc)
            summary["errors"].append(f"analysis_failed [{cid}]: {exc}")

    _update_manifest()

    try:
        from backend.common.business_events import emit_business_event

        emit_business_event(
            "TRANSCRIPT_INGEST_PASS",
            workcell="voice",
            metadata={
                "analyzed": summary["analyzed"],
                "reward_flowed": summary["reward_flowed"],
                "candidates": summary["candidates"],
                "reconciled": summary["reconciled"],
            },
        )
    except Exception:  # noqa: BLE001
        pass

    return summary


def _parse_vapi_ts(value: Any) -> datetime:
    """Parse a Vapi ISO-8601 timestamp to datetime, fallback to now."""
    if not isinstance(value, str) or not value.strip():
        return datetime.now(timezone.utc)
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)


class _MinimalProspect:
    """Lightweight prospect stand-in for the transcript analyzer."""

    __slots__ = (
        "prospect_id",
        "company_name",
        "phone",
        "industry",
        "city",
        "state",
        "call_priority",
        "lead_score",
        "seo_score",
        "callsheet_opener",
        "callsheet_pitch",
        "callsheet_offer",
        "callsheet_voicemail",
        "callsheet_objections",
    )

    def __init__(self, *, prospect_id: str = "", company_name: str = "", phone: str = ""):
        self.prospect_id = prospect_id
        self.company_name = company_name
        self.phone = phone
        self.industry = ""
        self.city = ""
        self.state = ""
        self.call_priority = ""
        self.lead_score = ""
        self.seo_score = ""
        self.callsheet_opener = ""
        self.callsheet_pitch = ""
        self.callsheet_offer = ""
        self.callsheet_voicemail = ""
        self.callsheet_objections = ""


# ---------------------------------------------------------------------------
# The asyncio loop + lifespan hooks (same shape as cold_dial_task)
# ---------------------------------------------------------------------------


async def _transcript_ingest_loop(interval: float) -> None:
    """Poll every ``interval`` seconds; run one ingest pass per tick."""
    try:
        await asyncio.sleep(_INITIAL_DELAY_SEC)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            result = await asyncio.to_thread(run_ingest_pass)
            analyzed = result.get("analyzed", 0)
            reward = result.get("reward_flowed", 0)
            if analyzed > 0:
                _LOG.info(
                    "transcript_ingest pass: analyzed=%d reward_flowed=%d "
                    "reconciled=%d candidates=%d errors=%d",
                    analyzed,
                    reward,
                    result.get("reconciled", 0),
                    result.get("candidates", 0),
                    len(result.get("errors", [])),
                )
            else:
                hour = datetime.now(timezone.utc).hour
                if hour != getattr(_transcript_ingest_loop, "_last_skip_hour", None):
                    _LOG.info(
                        "transcript_ingest pass: nothing to analyze "
                        "(candidates=%d, already_analyzed=%d)",
                        result.get("candidates", 0),
                        result.get("skipped_already_analyzed", 0),
                    )
                    _transcript_ingest_loop._last_skip_hour = hour  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            _LOG.exception("transcript_ingest_loop tick faulted; continuing")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


async def start_transcript_ingest_loop(app: Any) -> Optional[asyncio.Task]:
    """Schedule the in-container transcript-ingest cadence. Default ON."""
    if not _flag_on(ENV_ENABLED):
        _LOG.info("transcript_ingest loop disabled (%s)", ENV_ENABLED)
        return None
    existing = getattr(app.state, "transcript_ingest_task", None)
    if existing is not None and not existing.done():
        return existing
    interval = _float_env(ENV_INTERVAL, _DEFAULT_INTERVAL_SEC)
    task = asyncio.create_task(
        _transcript_ingest_loop(interval),
        name="samus.transcript_ingest_loop",
    )
    app.state.transcript_ingest_task = task
    _LOG.info("transcript_ingest loop started (interval=%.0fs)", interval)
    return task


async def stop_transcript_ingest_loop(app: Any) -> None:
    """Cancel + await the loop. Idempotent + best-effort."""
    task = getattr(app.state, "transcript_ingest_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    app.state.transcript_ingest_task = None
    _LOG.info("transcript_ingest loop stopped")


__all__ = [
    "start_transcript_ingest_loop",
    "stop_transcript_ingest_loop",
    "run_ingest_pass",
    "ENV_ENABLED",
    "ENV_INTERVAL",
    "ENV_MAX_PER_PASS",
]
