"""Gateway-internal voice transcript-ingest cadence -- turns the bandit loop ON.

Design rationale (voice -> UCB1 bandit learning loop, dead middle; 2026-07-07)
------------------------------------------------------------------------------
The dialer stamps a ``variant_arm_id`` on every placed call, and
:func:`backend.voice.transcript_analyzer.analyze_transcript` already flows the
analyzed call's [0,1] reward back to the UCB1 bandit
(:func:`backend.attribution.engine.record_outcome`). Both halves work. The
MIDDLE was dead: ``analyze_transcript`` only runs inside
:func:`backend.voice.ingest_pipeline.run_ingest_pipeline`, and nothing in the
container pulled completed Vapi transcripts into the staging directory that
pipeline reads. So calls completed, the webhook wrote ``end_of_call`` (200 OK),
but no transcript was ever analyzed -> reward never flowed -> the bandit never
learned. Same root cause as the cold-dial outage: the host cron that drove the
post-call sweep was removed 2026-07-03 and nothing in-container replaced it.

This module is the in-container replacement -- the same shape as
``control_tick_task`` / ``morning_ritual_task`` / ``production_health_task`` /
``cold_dial_task``: an asyncio loop bound to the gateway lifespan that, on a
cadence, (1) pulls recently-completed Vapi transcripts into the pipeline's
staging dir (:func:`backend.voice.vapi_transcript_ingest.pull_and_stage_recent`)
and (2) -- only when there is NEW data to analyze -- runs
``run_ingest_pipeline`` over the newly-staged files, which flows each call's
reward to the bandit. The stack keeps ONE boot-time scheduled task; every
recurring behaviour lives inside the always-on container where the state is.

Why gate the pipeline on ``staged > 0``: ``run_ingest_pipeline`` unconditionally
runs its tail (pattern aggregation + a day-over-day strategy LLM pass + callsheet
update) every invocation. Firing that every cadence with zero new calls would
burn an LLM call for nothing. When the pull stages nothing, the tick is a pure
no-op (one read-only Vapi ``GET`` + a dedup check).

This is READ-MOSTLY and safe to run anytime -- it pulls completed calls
(``GET /call``) and writes local analysis + bandit stats. Unlike the cold-dial
loop it does NOT place calls, so it needs no dial-window / armed / attested gate;
its only arm is its own enable flag. A completed call therefore results in a
persisted transcript analysis + a ``record_outcome`` on its arm within one
cadence, independent of the business day.

Kill switch / knobs (composable; sane, bounded defaults):
  * ``SAMUS_VOICE_INGEST_LOOP_ENABLED``     -- master arm for THIS loop (default ON)
  * ``SAMUS_VOICE_INGEST_INTERVAL_SEC``     -- poll cadence (default 1200 = 20 min)
  * ``SAMUS_VOICE_INGEST_LIMIT``            -- Vapi calls pulled per pass (default 100, clamped [1,100])
  * ``SAMUS_VOICE_INGEST_LOOKBACK_HOURS``   -- bound a fresh-boot backfill (default 72)

The pull + pipeline are synchronous (the pipeline does file IO + an LLM pass),
so the loop offloads the whole tick to a worker thread (``asyncio.to_thread``)
-- the gateway event loop is never blocked while a batch is analyzed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from backend.common import storage
from backend.common.dates import iso_now

_LOG = logging.getLogger("samus.gateway.voice_ingest_task")

_DEFAULT_INTERVAL_SEC = 1200.0  # 20 min -- reward flows within one cadence
_INITIAL_DELAY_SEC = 180.0  # let boot churn settle + a dial batch complete

ENV_ENABLED = "SAMUS_VOICE_INGEST_LOOP_ENABLED"
ENV_INTERVAL = "SAMUS_VOICE_INGEST_INTERVAL_SEC"
ENV_LIMIT = "SAMUS_VOICE_INGEST_LIMIT"
ENV_LOOKBACK = "SAMUS_VOICE_INGEST_LOOKBACK_HOURS"

_DEFAULT_LIMIT = 100
_DEFAULT_LOOKBACK_HOURS = 72

# Per-pass telemetry ledger -- one JSON row per ingest tick, so a reader can see
# the loop is alive + how many rewards flowed without grepping logs.
_PASS_LEDGER = "voice/ingest_passes.jsonl"


# ---------------------------------------------------------------------------
# Env helpers (same idiom as cold_dial_task / morning_ritual_task)
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
# Per-pass telemetry
# ---------------------------------------------------------------------------


def _emit_pass_telemetry(result: dict[str, Any]) -> None:
    """Append one ingest-pass row to the telemetry ledger. Fail-soft -- a
    telemetry miss must never disturb the loop or the reward flow it drives."""
    try:
        path = storage.root() / _PASS_LEDGER
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": iso_now(),
            "pulled": result.get("pulled", 0),
            "eligible": result.get("eligible", 0),
            "staged": result.get("staged", 0),
            "analyzed": result.get("analyzed", 0),
            "outcomes": result.get("outcomes", {}),
            "stage_error": result.get("stage_error"),
        }
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        _LOG.warning("voice_ingest telemetry append failed: %s", exc)


# ---------------------------------------------------------------------------
# One tick -- pull, and (only on new data) analyze -> reward. Never raises.
# ---------------------------------------------------------------------------


def run_once(
    *,
    client: Any = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """One ingest tick: pull recent Vapi transcripts -> stage -> (if new) analyze.

    Synchronous + fully injectable so the asyncio loop can offload it to a thread
    and tests can drive it deterministically with a fake Vapi client. Never
    raises. ``ran`` is False when the loop is disabled or the tick faults;
    ``analyzed`` is the count of transcripts the pipeline analyzed this pass
    (each of which flows its reward to the bandit if its arm was stamped).
    """
    if not _flag_on(ENV_ENABLED):
        return {"ran": False, "reason": "disabled"}
    try:
        from backend.voice.vapi_transcript_ingest import pull_and_stage_recent

        stage = pull_and_stage_recent(
            client=client,
            now=now,
            limit=_int_env(ENV_LIMIT, _DEFAULT_LIMIT),
            lookback_hours=_int_env(ENV_LOOKBACK, _DEFAULT_LOOKBACK_HOURS),
        )
        staged = int(stage.get("staged") or 0)
        result: dict[str, Any] = {
            "ran": True,
            "pulled": stage.get("pulled", 0),
            "eligible": stage.get("eligible", 0),
            "staged": staged,
            "stage_error": stage.get("error"),
            "analyzed": 0,
            "outcomes": {},
        }

        # Only run the (heavier) ingest pipeline when the pull produced new work.
        # skip_sync: no phone MTP sync -- Vapi is the source. skip_transcribe:
        # Vapi transcripts are already text; there is no audio to transcribe.
        if staged > 0:
            from backend.voice.ingest_pipeline import run_ingest_pipeline

            ingest = run_ingest_pipeline(skip_sync=True, skip_transcribe=True)
            result["analyzed"] = ingest.get("files_analyzed", 0)
            result["outcomes"] = ingest.get("outcomes", {})
            errs = ingest.get("errors") or []
            if errs:
                result["ingest_errors"] = errs[:5]

        _emit_pass_telemetry(result)
        return result
    except Exception as exc:  # noqa: BLE001 -- a tick must never crash the loop
        _LOG.exception("voice_ingest run_once faulted")
        return {"ran": False, "reason": f"run_once-error: {exc}"}


# ---------------------------------------------------------------------------
# The asyncio loop + lifespan hooks (same shape as cold_dial_task)
# ---------------------------------------------------------------------------


async def _voice_ingest_loop(interval: float) -> None:
    """Poll every ``interval`` seconds; pull + analyze completed Vapi calls.

    Structure mirrors ``cold_dial_task._cold_dial_loop`` /
    ``production_health_task._production_health_loop`` so operators reading any
    of the drivers see one shape. The tick is blocking (file IO + an LLM pass on
    new data), so it runs in a worker thread -- the gateway event loop stays
    responsive while a batch is analyzed.
    """
    try:
        await asyncio.sleep(_INITIAL_DELAY_SEC)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            result = await asyncio.to_thread(run_once)
            if result.get("analyzed"):
                _LOG.info(
                    "voice_ingest: analyzed %s new transcript(s) "
                    "(pulled=%s eligible=%s staged=%s outcomes=%s)",
                    result.get("analyzed"),
                    result.get("pulled"),
                    result.get("eligible"),
                    result.get("staged"),
                    result.get("outcomes"),
                )
            elif result.get("staged"):
                # Staged but nothing analyzed (e.g. all gated pending / llm error).
                _LOG.info(
                    "voice_ingest: staged %s, analyzed 0 (stage_error=%s)",
                    result.get("staged"),
                    result.get("stage_error"),
                )
            else:
                # Steady-state no-op -- log INFO at most once per hour so the ops
                # timeline shows the loop alive without spamming every cadence.
                hour = datetime.now(timezone.utc).hour
                if hour != getattr(_voice_ingest_loop, "_last_idle_hour", None):
                    reason = (
                        result.get("reason")
                        or result.get("stage_error")
                        or "no new completed calls"
                    )
                    _LOG.info("voice_ingest: idle (%s)", reason)
                    _voice_ingest_loop._last_idle_hour = hour  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 -- a tick fault never kills the loop
            _LOG.exception("voice_ingest_loop tick faulted; continuing")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


async def start_voice_ingest_loop(app: Any) -> Optional[asyncio.Task]:
    """Schedule the in-container voice transcript-ingest loop. Idempotent. Default ON."""
    if not _flag_on(ENV_ENABLED):
        _LOG.info("voice_ingest loop disabled (%s)", ENV_ENABLED)
        return None
    existing = getattr(app.state, "voice_ingest_task", None)
    if existing is not None and not existing.done():
        return existing
    interval = _float_env(ENV_INTERVAL, _DEFAULT_INTERVAL_SEC)
    task = asyncio.create_task(
        _voice_ingest_loop(interval),
        name="samus.voice_ingest_loop",
    )
    app.state.voice_ingest_task = task
    _LOG.info("voice_ingest loop started (interval=%.0fs)", interval)
    return task


async def stop_voice_ingest_loop(app: Any) -> None:
    """Cancel + await the loop. Idempotent + best-effort."""
    task = getattr(app.state, "voice_ingest_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 -- teardown swallows
        pass
    app.state.voice_ingest_task = None
    _LOG.info("voice_ingest loop stopped")


__all__ = [
    "start_voice_ingest_loop",
    "stop_voice_ingest_loop",
    "run_once",
    "ENV_ENABLED",
    "ENV_INTERVAL",
    "ENV_LIMIT",
    "ENV_LOOKBACK",
]
