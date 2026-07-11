"""Orchestrator for the TalkerACR → Samus feedback pipeline.

Full flow:
  1. transcript_sync     — pull new AMR recordings from Z Flip5 MTP to staging
  2. audio_transcribe    — local Whisper STT on AMR → .txt sidecar
  3. prospect_lookup     — build phone→ProspectRecord index from local CSVs
  4. transcript_ingest   — parse each .txt into RawTranscript
  5. privacy_gate        — classify each call; skip unknowns pending review
  6. transcript_analyzer — local LM Studio analysis per approved transcript
  7. pattern_aggregator  — aggregate all analyses → PatternReport
  8. callsheet_updater   — apply patterns → dynamic_callsheet_intel.json

Fully offline. No external API calls — Whisper runs locally on CPU,
LLM analysis runs against LM Studio at 127.0.0.1:1234.

Personal calls (unmatched phone + no operator approval) never reach step 6.

Operator pending-review workflow:
  GET  /voice/pending_transcripts   → list calls needing classification
  POST /voice/classify_transcript   → mark approved or personal

Run standalone:
  python -m backend.voice.ingest_pipeline [--dry-run] [--skip-sync] [--skip-transcribe]
"""
from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path

from backend.common import storage
from backend.common.dates import iso_now

_LOG = logging.getLogger("samus.voice.ingest_pipeline")


def run_ingest_pipeline(
    *,
    device_name: str = "Hustle's Z Flip5",
    phone_sub_path: str = r"Internal storage\Documents\TalkerACR\All",
    dry_run: bool = False,
    skip_sync: bool = False,
    skip_transcribe: bool = False,
) -> dict:
    """Run the full transcript → feedback pipeline.

    Args:
        dry_run:   parse, gate, and analyze (with local LLM) but skip
                   writing dynamic callsheet intel to disk.
        skip_sync: skip MTP file pull; process whatever is in staging.

    Returns a summary dict suitable for the FastAPI response.
    """
    ts = iso_now()
    errors: list[str] = []

    # ── 1. MTP sync ────────────────────────────────────────────────
    files_synced = 0
    sync_error: str | None = None

    if not skip_sync:
        from .transcript_sync import sync_transcripts
        sync_result = sync_transcripts(
            device_name=device_name,
            phone_sub_path=phone_sub_path,
        )
        sync_error = sync_result.get("error")
        if sync_error:
            _LOG.warning("ingest_pipeline: sync error — %s", sync_error)
            errors.append(f"sync: {sync_error}")
        files_synced = len(sync_result.get("copied") or [])
        errors.extend(sync_result.get("errors") or [])

    # ── 2. Local audio transcription (AMR → .txt sidecar) ──────────
    transcribe_summary: dict = {"transcribed": 0, "skipped": 0, "errors": []}
    if not skip_transcribe:
        try:
            from .audio_transcribe import transcribe_pending
            from .transcript_sync import staging_dir
            transcribe_summary = transcribe_pending(staging_dir())
            errors.extend(
                f"transcribe: {e}" for e in (transcribe_summary.get("errors") or [])
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("ingest_pipeline: transcription stage failed: %s", exc)
            errors.append(f"transcribe_stage_failed: {exc}")

    # ── 3. Build prospect phone index (local CSVs only, no network) ─
    from .prospect_lookup import build_phone_index
    phone_index = build_phone_index()

    # ── 4. Collect staged transcripts ──────────────────────────────
    from .transcript_sync import list_staged_text
    staged = list_staged_text()

    analyses_dir = storage.root() / "voice" / "analyses"
    analyzed_hashes: set[str] = set()
    if analyses_dir.exists():
        analyzed_hashes = {p.stem for p in analyses_dir.glob("*.json")}

    from .transcript_ingest import parse_transcript_file
    from .privacy_gate import GateDecision, classify

    pending_count = 0
    personal_count = 0
    to_analyze: list[tuple[Path, object, object]] = []  # (path, raw, prospect)

    for fpath in staged:
        raw = parse_transcript_file(fpath)
        if raw is None:
            errors.append(f"parse_failed: {fpath.name}")
            continue

        if raw.file_hash in analyzed_hashes:
            continue  # already done

        # ── 4. Privacy gate ───────────────────────────────────────
        decision, prospect = classify(raw, phone_index=phone_index)

        if decision == GateDecision.PERSONAL:
            personal_count += 1
            _LOG.info("privacy_gate: %s — personal, skipped", fpath.name)
            continue

        if decision == GateDecision.PENDING_REVIEW:
            pending_count += 1
            continue

        # PROSPECT_MATCHED or APPROVED — proceed
        to_analyze.append((fpath, raw, prospect))

    # ── 5. Local LM Studio analysis ────────────────────────────────
    from .transcript_analyzer import analyze_transcript, TranscriptAnalysis

    new_analyses: list[TranscriptAnalysis] = []
    for fpath, raw, prospect in to_analyze:
        try:
            analysis = analyze_transcript(raw, prospect=prospect, dry_run=dry_run)
            new_analyses.append(analysis)
            if analysis.llm_error:
                errors.append(f"llm_error [{fpath.name}]: {analysis.llm_error}")
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("ingest_pipeline: analysis failed %s: %s", fpath.name, exc)
            errors.append(f"analysis_failed [{fpath.name}]: {exc}")

    files_analyzed = len(new_analyses)

    # ── 5b. Push analyzer outcomes to forge-ui call_outcomes journal ─
    # Operator entries always win — pipeline only fills in calls the
    # operator hasn't classified yet. dry_run skips the writeback so
    # operators can preview without touching the journal.
    crm_pushed = 0
    crm_skipped: dict[str, int] = {}
    if not dry_run:
        from .crm_writeback import write_outcome
        for fpath, raw, prospect in to_analyze:
            analysis = next(
                (a for a in new_analyses if a.file_hash == raw.file_hash),
                None,
            )
            if analysis is None or analysis.llm_error:
                continue
            try:
                result = write_outcome(
                    prospect_id=getattr(prospect, "prospect_id", "") or "",
                    company=getattr(prospect, "company_name", "") or "",
                    phone=getattr(prospect, "phone", "") or raw.contact_phone or "",
                    pipeline_outcome=analysis.outcome,
                    notes=f"[transcript pipeline] {fpath.name}",
                    call_ts=raw.call_ts,
                )
                if result.get("written"):
                    crm_pushed += 1
                else:
                    reason = result.get("skipped_for") or "unknown"
                    crm_skipped[reason] = crm_skipped.get(reason, 0) + 1
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("crm_writeback failed for %s: %s", fpath.name, exc)
                errors.append(f"crm_writeback_failed [{fpath.name}]: {exc}")

    # ── 6. Pattern aggregation (all historical + new) ──────────────
    from .pattern_aggregator import aggregate_patterns, load_all_analyses

    patterns_updated = False
    pattern_report = None
    try:
        all_analyses = load_all_analyses()
        pattern_report = aggregate_patterns(all_analyses)
        patterns_updated = True
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("ingest_pipeline: pattern aggregation failed: %s", exc)
        errors.append(f"aggregation_failed: {exc}")

    # ── 6.5. Day-over-day strategy reasoning ─────────────────────
    strategy_brief = None
    strategy_error: str | None = None
    if pattern_report is not None:
        try:
            from .call_strategy_reasoner import reason as reason_strategy
            strategy_brief = reason_strategy(today_report=pattern_report)
            if strategy_brief and strategy_brief.llm_error:
                strategy_error = strategy_brief.llm_error
                errors.append(f"strategy_reasoner: {strategy_error}")
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("ingest_pipeline: strategy reasoning failed: %s", exc)
            errors.append(f"strategy_reasoning_failed: {exc}")

    # ── 7. Callsheet intel update ──────────────────────────────────
    callsheet_updated = False
    if not dry_run and pattern_report is not None:
        try:
            from .callsheet_updater import update_callsheet_intel
            update_callsheet_intel(report=pattern_report, strategy=strategy_brief)
            callsheet_updated = True
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("ingest_pipeline: callsheet update failed: %s", exc)
            errors.append(f"callsheet_update_failed: {exc}")

    # ── Summary ────────────────────────────────────────────────────
    outcome_dist: dict[str, int] = {}
    if new_analyses:
        outcome_dist = dict(Counter(a.outcome for a in new_analyses))

    pos_rate = pattern_report.positive_outcome_rate if pattern_report else 0.0

    return {
        "ts": ts,
        "files_synced": files_synced,
        "files_transcribed": transcribe_summary.get("transcribed", 0),
        "transcribe_model": transcribe_summary.get("model"),
        "staged_text_total": len(staged),
        "files_analyzed": files_analyzed,
        "files_gated_pending": pending_count,
        "files_gated_personal": personal_count,
        "crm_outcomes_pushed": crm_pushed if not dry_run else 0,
        "crm_writeback_skipped": crm_skipped if not dry_run else {},
        "outcomes": outcome_dist,
        "positive_outcome_rate": pos_rate,
        "patterns_updated": patterns_updated,
        "strategy_reasoned": strategy_brief is not None and not strategy_error,
        "strategy_recommendations": len(strategy_brief.recommendations) if strategy_brief else 0,
        "callsheet_updated": callsheet_updated,
        "errors": errors,
        "sync_error": sync_error,
        "total_analyses_on_disk": len(analyzed_hashes) + files_analyzed,
    }


if __name__ == "__main__":
    import json
    import sys

    dry_run = "--dry-run" in sys.argv
    skip_sync = "--skip-sync" in sys.argv
    skip_transcribe = "--skip-transcribe" in sys.argv

    result = run_ingest_pipeline(
        dry_run=dry_run,
        skip_sync=skip_sync,
        skip_transcribe=skip_transcribe,
    )
    print(json.dumps(result, indent=2))
