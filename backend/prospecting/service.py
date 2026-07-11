"""process_discovery — orchestrates the prospecting pipeline.

zipcodes + industries -> Google Places discovery -> score + classify priority ->
strategy "decide" step (per-industry bandit policy family) -> callsheet
templating -> CSV export. Audit event appended on completion.

In-process idempotency via ``GLOBAL_IDEMPOTENCY_STORE`` keyed on the task_id —
re-running with the same task_id returns the cached result.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import date
from typing import Callable, TypeVar

from backend.common import events, persistence
from backend.common.http_client import signed_get_json_sync
from backend.common.idempotency import GLOBAL_IDEMPOTENCY_STORE

from .callsheet import build_call_sheet, build_call_sheet_smart_costed
from .csv_export import write_call_list
from .models import DiscoveryRequest, DiscoveryResult, ProspectRecord
from .place_search import discover_for_zipcode
from .scorer import classify_priority, score_prospect
from .text_export import write_morning_call_list

_LOG = logging.getLogger("samus.prospecting.service")

# Container-side default — every workcell anchors its audit ledger under
# /opt/samus/data/<workcell>/ (crm, intake, proposal, seo, … all match). The
# host overrides via SAMUS_PROSPECTING_AUDIT_PATH. The prior r"E:\…" Windows
# path does not exist in the Cloud Run / Docker image, so the ledger append
# silently OSError'd and the audit trail was lost off-host.
_AUDIT_PATH_DEFAULT = "/opt/samus/data/prospecting/prospecting_audit.jsonl"

_T = TypeVar("_T")

# Per-prospect wall-clock ceilings (seconds). Belt-and-suspenders over the
# individual per-request HTTP/DNS timeouts: even if some future call site
# forgets its own timeout, or a legitimate chain of bounded calls stacks up
# (the warm/hot audit does homepage + robots.txt + 2x PageSpeed + an LLM
# draft + the security probes), no single prospect can stall the whole
# sequential daily run past this bound. Overrun => that prospect is logged
# and SKIPPED (its record is kept at the pre-step state), the run continues,
# and the call-list CSV is still written from the prospects that finished.
# This is the second half of the 2026-07-01 hang fix (the first is the
# bounded DNS resolver in backend.common.safe_fetch).
_ENRICH_DEADLINE_S = float(os.getenv("SAMUS_PROSPECT_ENRICH_DEADLINE_S", "45"))
_AUDIT_DEADLINE_S = float(os.getenv("SAMUS_PROSPECT_AUDIT_DEADLINE_S", "90"))


class ProspectDeadlineExceeded(TimeoutError):
    """A single prospect's enrichment/audit step overran its wall-clock bound."""


def _run_with_deadline(fn: Callable[[], _T], *, deadline_s: float) -> _T:
    """Run ``fn`` on a fresh single-use daemon thread; raise on overrun.

    2026-07-01 hang, second root cause. The prior implementation ran every
    deadline call on a *shared* ``ThreadPoolExecutor(max_workers=4)``. On
    timeout the worker thread keeps running the un-cancellable blocked socket
    read (``future.cancel()`` is a no-op once a worker has picked the task up),
    so a wedged prospect PERMANENTLY consumes one of the four pool workers.
    After four wedged prospects the pool is exhausted and the very next
    ``submit()`` blocks forever waiting for a free worker — the whole run
    hangs (matching the observed 4×90s≈6min stall).

    The fix: spawn a fresh daemon thread per call. A leaked (wedged) thread is
    simply abandoned — it never occupies a slot that a later prospect needs, so
    a handful of genuinely-wedged prospects can never stall the run. Daemon =
    process exit is never blocked by an abandoned read. This is only a
    backstop; the primary defense is the phase-split HTTP timeouts in
    backend.common.safe_fetch / backend.seo.* which bound the read itself, so
    in practice these threads finish on their own and are not leaked at all.
    """
    result_box: list[_T] = []
    error_box: list[BaseException] = []

    def _runner() -> None:
        try:
            result_box.append(fn())
        except BaseException as exc:  # noqa: BLE001 — re-raised on the caller thread
            error_box.append(exc)

    worker = threading.Thread(
        target=_runner, name="prospect-deadline", daemon=True,
    )
    worker.start()
    worker.join(timeout=deadline_s)
    if worker.is_alive():
        # Over budget: abandon the (daemon) thread and skip this prospect. The
        # thread keeps running its blocked read to natural completion in the
        # background but holds no pooled resource, so it cannot stall the run.
        raise ProspectDeadlineExceeded(
            f"prospect step exceeded {deadline_s}s wall-clock budget"
        )
    if error_box:
        raise error_box[0]
    if result_box:
        return result_box[0]
    # Defensive: a runner that neither produced a result nor an error (should
    # be unreachable) is treated as an overrun rather than returning None.
    raise ProspectDeadlineExceeded(
        f"prospect step produced no result within {deadline_s}s"
    )


def _audit_llm_cost(audit_result: dict) -> float:
    """Best-effort: pull the content-draft LLM cost out of an audit_and_report dict.

    The warm/hot ``audit_and_report`` (Step 2.7) runs SEO content drafting,
    whose LLM call cost rides on ``ContentResult.llm_cost_usd`` and surfaces
    in the result's ``content`` block. Returns 0.0 when the key is absent
    (templated content) or the shape is unexpected — cost capture must never
    break discovery (strategy-integration build, Unit 4).
    """
    try:
        content = audit_result.get("content") or {}
        return float(content.get("llm_cost_usd", 0.0) or 0.0)
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _audit_ledger() -> persistence.JsonlLedger:
    path = os.getenv("SAMUS_PROSPECTING_AUDIT_PATH", _AUDIT_PATH_DEFAULT)
    return persistence.JsonlLedger(path)


# Website statuses worth a verification re-poll once the main run finishes —
# every *soft* failure, i.e. one a transient crawl-time blip (timeout, IPv6
# path flap, momentary 5xx, flaky DNS, a WAF that throttled) could have caused.
# A positive "no real website" verdict (no_website / parked / social_only) and
# the deterministic gone(410) are NOT re-polled: re-polling cannot change them.
_RECHECK_STATUSES: frozenset[str] = frozenset({
    "domain_unresolved", "unreachable_timeout", "unreachable",
    "server_error", "http_error", "access_blocked", "empty",
})


def recheck_unreachable(
    prospects: list[ProspectRecord],
    *,
    enable_seo_audit: bool = True,
) -> list[ProspectRecord]:
    """Re-poll prospects flagged with a soft-failure website status; re-classify
    and re-score any that now respond (list-integrity backup pass).

    A single transient crawl failure at Step 2a can mislabel a healthy site as
    unreachable for the whole day — and an unmeasured ``seo_score`` of 0 then
    inflates the lead score (the scorer reads worse SEO as a stronger lead).
    This pass, run after the main pipeline, fetches each soft-failed prospect
    once more: one that now responds is re-classified, re-SEO-scored, re-lead-
    scored and gets a fresh callsheet; one that still fails is left as a
    confirmed, factual non-response. Best-effort + per-prospect isolated — any
    fault leaves that prospect exactly as the main run left it.
    """
    from .crawler import classify_website, fetch_homepage
    from .seo_audit import score_seo

    out: list[ProspectRecord] = []
    for p in prospects:
        status = (p.website_status or "").strip().lower()
        if status not in _RECHECK_STATUSES or not p.website_url:
            out.append(p)
            continue
        try:
            page = fetch_homepage(p.website_url)
            new_status = classify_website(page)
            if new_status == status:
                out.append(p)  # still failing — confirmed non-response
                continue
            update: dict[str, object] = {"website_status": new_status}
            if enable_seo_audit and new_status == "live":
                seo_value, _issues = score_seo(page)
                update["seo_score"] = int(seo_value or 0)
            recovered = p.model_copy(update=update)
            new_score = score_prospect(recovered)
            recovered = recovered.model_copy(update={
                "lead_score": new_score,
                "call_priority": classify_priority(new_score),
            })
            out.append(build_call_sheet(recovered))
            _LOG.info(
                "website recheck reclassified prospect=%s %s -> %s",
                p.prospect_id, status, new_status,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort integrity pass
            _LOG.warning(
                "website recheck failed prospect=%s err=%s", p.prospect_id, exc,
            )
            out.append(p)
    return out


# Website-ABSENCE statuses: the prospect is flagged as having no site at all, so
# there is no URL to re-poll (recheck_unreachable can't catch these) — but a
# fresh, targeted Places search by name+city can find a site the bulk discovery
# missed. That's the root of the "pitch a business it has no website when it
# does" false positive.
_ABSENCE_STATUSES: frozenset[str] = frozenset({
    "no_website", "parked", "social_only", "gone", "domain_unresolved",
})


def verify_web_presence(prospects: list[ProspectRecord]) -> list[ProspectRecord]:
    """ROOT web-presence integrity pass. For prospects flagged with NO website
    (no URL — so ``recheck_unreachable`` skips them), do a fresh, targeted Google
    Places lookup by name+city. If Places actually lists a live website, the bulk
    discovery/crawler missed it — correct ``website_url`` + ``website_status``
    (and re-score + re-callsheet) at the SOURCE, so NO downstream (callsheet
    finding, demo build, Morgan's pitch) ever treats a business that HAS a site
    as if it doesn't. Fixing it here makes the demo/callsheet presence gates
    belt-and-braces rather than the fix.

    Best-effort + per-prospect isolated: a Places error (verify_presence is
    fail-soft) leaves the prospect exactly as it was.
    """
    from backend.website.presence_check import verify_presence

    out: list[ProspectRecord] = []
    for p in prospects:
        status = (p.website_status or "").strip().lower()
        if status not in _ABSENCE_STATUSES or (p.website_url or "").strip():
            out.append(p)
            continue
        try:
            # Pass the CRM phone as the deep-verify anchor (guaranteed present
            # from discovery; more reliable than re-deriving it from a fresh
            # Places name-match). The street address for matching comes from the
            # Places listing inside verify_presence (it carries the street#+ZIP).
            v = verify_presence(
                p.company_name, city=p.city, state=p.state, known_phone=p.phone)
            if not v.buildable and v.website:
                recovered = p.model_copy(update={
                    "website_url": v.website,
                    # a live site we simply couldn't fetch — NOT a "no site" hook
                    "website_status": "access_blocked",
                })
                new_score = score_prospect(recovered)
                recovered = recovered.model_copy(update={
                    "lead_score": new_score,
                    "call_priority": classify_priority(new_score),
                })
                out.append(build_call_sheet(recovered))
                _LOG.info(
                    "presence verify: prospect=%s HAS a live site (%s) — corrected "
                    "%s -> access_blocked (no false 'no website' pitch)",
                    p.prospect_id, v.website, status,
                )
            else:
                out.append(p)
        except Exception as exc:  # noqa: BLE001 — best-effort integrity pass
            _LOG.warning("presence verify failed prospect=%s err=%s", p.prospect_id, exc)
            out.append(p)
    return out


def process_discovery(
    req: DiscoveryRequest,
    *,
    task_id: str | None = None,
) -> DiscoveryResult:
    """End-to-end: discover -> score -> callsheet -> CSV."""
    task_id = task_id or f"discovery-{req.campaign_name}-{date.today().isoformat()}"
    cache_key = f"prospecting:{task_id}"

    cached = GLOBAL_IDEMPOTENCY_STORE.get(cache_key)
    if cached is not None and isinstance(cached, dict):
        _LOG.info("process_discovery cache hit task_id=%s", task_id)
        result = DiscoveryResult.model_validate(cached)
        return result.model_copy(update={"cache_hit": True})

    # Step 1: discover
    # Two fixes vs. the legacy iteration:
    #   (a) industry-first-eats-everything: discover_for_zipcode iterates
    #       industries internally and breaks the loop when len(out) hits
    #       max_results_per_zip. For Yuba City with industries=[real estate,
    #       dentist, hvac, ...] this meant real estate filled the 25-slot cap
    #       before any other industry ran. Fix: call discover_for_zipcode
    #       per (zip, industry) so each pair gets its own quota.
    #   (b) cross-zip dupes: a Yuba City business shows up in the Places
    #       results for both 95991 and 95993 because their search radii
    #       overlap. discover_for_zipcode dedupes by place_id WITHIN a zip
    #       call but not across calls. Fix: track prospect_id globally and
    #       skip subsequent re-emissions.
    industries_list = list(req.industries) or [""]
    per_industry_cap = max(3, req.max_results_per_zip // max(1, len(industries_list)))
    discovered: list[ProspectRecord] = []
    seen_prospect_ids: set[str] = set()
    for zipcode in req.zipcodes:
        zip_count = 0
        for industry in industries_list:
            zi_prospects = discover_for_zipcode(
                zipcode=zipcode,
                industries=[industry],
                max_results_per_zip=per_industry_cap,
                must_have_website=req.must_have_website,
            )
            for p in zi_prospects:
                if p.prospect_id and p.prospect_id in seen_prospect_ids:
                    continue
                seen_prospect_ids.add(p.prospect_id)
                discovered.append(p)
                zip_count += 1
        _LOG.info(
            "process_discovery zip done",
            extra={"zipcode": zipcode, "count": zip_count,
                   "industries": industries_list,
                   "per_industry_cap": per_industry_cap},
        )

    # Step 2: SEO + owner enrichment, THEN lead scoring. The SEO/enrichment
    # pass runs first because the lead scorer folds in an SEO-opportunity
    # component (worse SEO = better lead — see scorer.py), so a prospect must
    # carry its real seo_score before score_prospect sees it.
    #
    # Step 2a — SEO score + owner enrichment per prospect. Both analyses run
    # against the homepage HTML so we fetch each prospect's site at most once.
    # Owner enrichment additionally hits /contact + /about (and FB About when
    # enabled) as fallback when the homepage didn't yield an owner_email.
    #
    # Failure modes:
    #   - fetch_homepage swallows transport errors -> page dict with no html
    #   - score_seo returns (0, ['no_html']) when html is empty
    #   - enrich_from_page_with_fallback returns all-empty signals safely
    # The outer try/except is a defensive boundary against unexpected
    # exceptions in the analysis chain — never tank the whole run for one
    # prospect.
    enriched: list[ProspectRecord] = list(discovered)
    if req.enable_seo_audit or req.enable_owner_enrichment:
        from .crawler import classify_website, fetch_homepage
        from .enrichment import enrich_from_page_with_fallback
        from .seo_audit import score_seo

        def _enrich_one(prospect: ProspectRecord) -> dict[str, object]:
            """Compute the enrichment update dict for one prospect.

            Runs the (network-bound) homepage fetch + SEO score + owner
            enrichment + optional Apollo lookup. Pulled into a closure so the
            whole chain can be run under a per-prospect wall-clock deadline —
            no single site may stall the sequential daily run.
            """
            page = fetch_homepage(prospect.website_url)
            # Reachability is recorded even when the fetch failed — a dead
            # domain yields no SEO/enrichment signal but IS a call hook.
            update: dict[str, object] = {
                "website_status": classify_website(page),
            }
            if req.enable_seo_audit:
                seo_score_value, _issues = score_seo(page)
                update["seo_score"] = int(seo_score_value or 0)
            if req.enable_owner_enrichment:
                signals = enrich_from_page_with_fallback(
                    page, prospect.website_url,
                    enable_facebook=req.enable_facebook_enrichment,
                )
                update.update(signals)
                # business_description: the homepage scrape wins, but the
                # Places editorialSummary set at discovery is the fallback.
                # An empty scrape must not clobber that — drop the key so
                # model_copy keeps the discovery-stage value.
                if not signals.get("business_description"):
                    update.pop("business_description", None)
                # Stage 4 (last-resort): Apollo people-search by domain.
                # Fires only when the on-site cascade yielded no owner
                # email AND an Apollo key is configured. Gated by per-day
                # cap (see apollo_adapter.py). Fail-soft: missing key /
                # cap hit / network error / masked-tier all return {}.
                if not signals.get("owner_email"):
                    try:
                        from .apollo_adapter import enrich_via_apollo
                        apollo = enrich_via_apollo(
                            company_name=prospect.company_name,
                            website_url=prospect.website_url,
                            city=prospect.city,
                            state=prospect.state,
                        )
                        if apollo.get("owner_email"):
                            # Don't clobber existing non-empty fields.
                            for k, v in apollo.items():
                                if v and not update.get(k):
                                    update[k] = v
                    except Exception as _exc:  # noqa: BLE001
                        _LOG.debug("apollo enrichment skipped url=%s err=%s",
                                   prospect.website_url, _exc)
            return update

        audited: list[ProspectRecord] = []
        for prospect in discovered:
            if not prospect.website_url:
                # No website at all — the strongest web-design pitch there is.
                audited.append(prospect.model_copy(update={
                    "website_status": "no_website",
                }))
                continue
            try:
                # Wall-clock bound: an over-budget prospect is logged + SKIPPED
                # (kept at its discovery-stage state) rather than hanging the
                # whole run. Layers on top of the per-request HTTP/DNS timeouts.
                update = _run_with_deadline(
                    lambda p=prospect: _enrich_one(p),
                    deadline_s=_ENRICH_DEADLINE_S,
                )
                audited.append(prospect.model_copy(update=update))
                # Unified business-event ledger (HOTL Tranche 1) — one
                # lead.enriched row per prospect whose enrichment pass
                # completed. Fail-soft by contract: never raises.
                from backend.common.business_events import (
                    LEAD_ENRICHED,
                    emit_business_event,
                )
                emit_business_event(
                    LEAD_ENRICHED,
                    workcell="prospecting",
                    prospect_id=(prospect.prospect_id or None),
                    metadata={
                        "website_url": prospect.website_url,
                        "website_status": str(update.get("website_status") or ""),
                        "seo_score": update.get("seo_score"),
                        "owner_email_found": bool(update.get("owner_email")),
                    },
                )
            except ProspectDeadlineExceeded as exc:
                _LOG.warning(
                    "enrichment deadline exceeded url=%s (%s) — skipping "
                    "enrichment for this prospect, keeping it on the list",
                    prospect.website_url, exc,
                )
                audited.append(prospect)
            except Exception as exc:  # noqa: BLE001 — defensive boundary
                _LOG.warning(
                    "enrichment failed url=%s err=%s",
                    prospect.website_url, exc,
                )
                audited.append(prospect)
        enriched = audited

    # Step 2b — lead score + priority, now that seo_score is populated.
    scored: list[ProspectRecord] = []
    for prospect in enriched:
        score = score_prospect(prospect)
        priority = classify_priority(score)
        scored.append(prospect.model_copy(update={
            "lead_score": score,
            "call_priority": priority,
        }))

    # Step 2c: signal_filter pre-qualification gate. Each scored prospect is
    # run through the backend.signal_filter weighted admission threshold;
    # prospects that fail are DROPPED here, before the costly Step 2.6
    # strategy decision, Step 2.7 warm/hot deep audit, and Step 3 callsheet
    # LLM. This keeps low-probability prospects out of the queue/LLM/SEO/
    # outreach paths (v1.3.0 changelog). Pure deterministic logic — the gate
    # scores off the enrichment already collected in Step 2a, so it adds no
    # network calls and no LLM spend.
    #
    # Best-effort, like every other cross-workcell boundary in this pipeline:
    # apply_signal_filter_gate swallows a missing-module / scoring fault and
    # returns the prospect list untouched, so signal_filter being unavailable
    # never tanks a discovery run. Gated on req.enable_signal_filter_gate
    # (DEFAULT TRUE) for operator control and offline tests.
    if req.enable_signal_filter_gate:
        from .signal_gate import apply_signal_filter_gate

        before = len(scored)
        scored, rejected = apply_signal_filter_gate(scored)
        _LOG.info(
            "process_discovery signal_filter gate applied",
            extra={"task_id": task_id, "admitted": len(scored),
                   "rejected": rejected, "before": before},
        )

    # Step 2.6: Strategy "decide" step — pick the bandit policy family.
    # For each distinct industry in the scored set, ask the strategy
    # workcell's hierarchical bandit which execution policy family to run,
    # then stamp that choice onto every prospect of that industry. The
    # outcome of each resulting deal is attributed back to this arm by the
    # strategy "learn" step (Unit 3).
    #
    # select_best_policy is per-industry: call it ONCE per distinct industry
    # and reuse the answer for every prospect of that industry — never
    # per-prospect. A cold-start bandit (no trials for the industry) yields
    # families[0] via UCB1's explore-everything-once rule, so a fresh bandit
    # still returns a sensible default policy family rather than nothing.
    #
    # Best-effort, like the Step 2.5 enrichment boundary: the strategy import
    # or the bandit-store read could fail (missing module, catastrophic store
    # fault). On any failure policy_family stays "" and prospecting continues
    # unaffected — strategy being down must never tank a discovery run.
    if req.enable_strategy_policy:
        try:
            # Lazy import — matches the Step 2.5 / 2.7 lazy-import pattern;
            # keeps the prospecting module loadable when strategy is disabled.
            from backend.strategy.portfolio_manager import select_best_policy

            policy_by_industry: dict[str, str] = {}
            with_policy: list[ProspectRecord] = []
            for prospect in scored:
                industry = prospect.industry
                if industry not in policy_by_industry:
                    # One call per distinct industry; reused for every
                    # prospect of that industry below.
                    policy_by_industry[industry] = select_best_policy(industry)
                with_policy.append(prospect.model_copy(update={
                    "policy_family": policy_by_industry[industry],
                }))
            scored = with_policy
            _LOG.info(
                "process_discovery strategy policy decided",
                extra={"task_id": task_id,
                       "policy_by_industry": policy_by_industry},
            )
        except Exception as exc:  # noqa: BLE001 — defensive boundary
            _LOG.warning(
                "strategy policy selection failed; policy_family left empty: %s",
                exc,
            )

    # Step 2.7: Full audit + report for warm/hot prospects only.
    # Cold/low prospects skip this to bound LLM spend — the operator won't
    # call them anyway, so a deep report on each would burn budget for no
    # benefit. The deep audit is idempotency-cached by URL inside
    # backend.seo.service so back-to-back runs reuse a same-day cache hit.
    if req.enable_full_audit_for_warm:
        # Lazy imports — keeps the prospecting module light when this
        # feature is disabled, and dodges the bs4/anthropic import graph
        # in tests that opt out.
        from backend.seo.models import AuditRequest
        from backend.seo.service import audit_and_report
        with_reports: list[ProspectRecord] = []
        for prospect in scored:
            if not prospect.website_url or prospect.call_priority == "low":
                with_reports.append(prospect)
                continue
            try:
                audit_req = AuditRequest(
                    url=prospect.website_url,
                    keywords=[prospect.industry, prospect.city] if prospect.industry else [],
                    industry=prospect.industry,
                )
                # Wall-clock bound over the whole deep-audit chain (homepage +
                # robots.txt + 2x PageSpeed + LLM draft + security probes). An
                # over-budget audit is logged + SKIPPED (the prospect stays on
                # the list without a report) rather than stalling the run.
                result = _run_with_deadline(
                    lambda r=audit_req, p=prospect: audit_and_report(
                        r,
                        target_keywords=[p.industry] if p.industry else None,
                        customer_label=p.company_name or None,
                    ),
                    deadline_s=_AUDIT_DEADLINE_S,
                )
                # The warm/hot full audit also runs the passive security
                # audit; lift its A-F grade onto the prospect for the
                # call-list tie-breaker. Absent when the security audit is
                # disabled — degrades to "" (no grade).
                audit_findings = (result.get("audit") or {}).get("findings") or {}
                security_grade = str(
                    (audit_findings.get("security") or {}).get("grade") or ""
                )
                # Strategy-integration build, Unit 4: the audit's content-draft
                # stage may have fired an LLM call — add its real priced cost
                # to this prospect's running per-prospect LLM spend.
                audit_cost = _audit_llm_cost(result)
                with_reports.append(prospect.model_copy(update={
                    "seo_report_path": str(result.get("report_path") or ""),
                    "security_grade": security_grade,
                    "llm_cost_usd": prospect.llm_cost_usd + audit_cost,
                }))
            except ProspectDeadlineExceeded as exc:
                _LOG.warning(
                    "full audit deadline exceeded url=%s priority=%s (%s) — "
                    "skipping report for this prospect, keeping it on the list",
                    prospect.website_url, prospect.call_priority, exc,
                )
                with_reports.append(prospect)
            except Exception as exc:  # noqa: BLE001 — defensive boundary
                _LOG.warning(
                    "full audit failed url=%s priority=%s err=%s",
                    prospect.website_url, prospect.call_priority, exc,
                )
                with_reports.append(prospect)
        scored = with_reports

    # Step 3: callsheet. Gated on req.enable_llm_callsheet (DEFAULT FALSE) —
    # turning the callsheet LLM on adds new spend to the live 07:30 daily run,
    # which is an explicit operator money decision and must be opted into.
    #
    #   enable_llm_callsheet=True  -> build_call_sheet_smart_costed, which
    #     auto-selects the LLM path only for top-N prospects (hot AND
    #     lead_score >= 75) with an API key configured; every other prospect
    #     still takes the deterministic templated path at zero cost. The
    #     costed variant returns the real priced USD cost of any LLM call.
    #     Best-effort: a callsheet LLM/pricing fault must never tank a
    #     discovery run — on any failure fall back to the templated callsheet
    #     at 0.0 cost.
    #   enable_llm_callsheet=False (default) -> plain templated build_call_sheet
    #     at callsheet_cost = 0.0, byte-identical to pre-strategy-integration
    #     behaviour. No callsheet LLM call ever fires.
    #
    # Either way callsheet_cost is folded into llm_cost_usd below — it is just
    # always 0.0 when the toggle is off. The Step 2.7 audit-content cost
    # capture above is unaffected by this toggle.
    finalized: list[ProspectRecord] = []
    for prospect in scored:
        if req.enable_llm_callsheet:
            try:
                sheet, callsheet_cost = build_call_sheet_smart_costed(prospect)
            except Exception as exc:  # noqa: BLE001 — defensive boundary
                _LOG.warning(
                    "callsheet generation failed prospect=%s; templated fallback: %s",
                    prospect.prospect_id, exc,
                )
                sheet, callsheet_cost = build_call_sheet(prospect), 0.0
        else:
            sheet, callsheet_cost = build_call_sheet(prospect), 0.0
        finalized.append(sheet.model_copy(update={
            "llm_cost_usd": sheet.llm_cost_usd + callsheet_cost,
        }))

    # Step 3.5: website-status verification re-poll (list-integrity backup).
    # A transient crawl failure at Step 2a can mislabel a healthy site as
    # unreachable for the day and inflate its lead score off an unmeasured SEO.
    # Re-poll the soft failures now the run is done; a prospect that responds
    # is re-classified + re-scored, one that still fails is confirmed.
    if req.enable_website_recheck:
        finalized = recheck_unreachable(
            finalized, enable_seo_audit=req.enable_seo_audit,
        )
        # Root fix for the "no website" false positive: a fresh Places lookup
        # catches sites the bulk discovery missed (no URL to re-poll), so we
        # never build/pitch a site to a business that already has one.
        finalized = verify_web_presence(finalized)

    # Step 3.6: backfill recent-contact state from the CRM. Lets the morning
    # call list down-rank prospects the operator already worked (any outcome
    # logged via forge-ui's cc-notes counts — see backend.crm.call_outcomes
    # "noted"). Best-effort: a CRM read failure leaves last_contact_* empty
    # and the prospect ranks by the original priority+score key.
    finalized = _backfill_recent_contact(finalized)

    # Step 3.7: Persist to DDB samus_prospects when the cadence requests it.
    # The in-container cadence sets persist_prospects=True so newly discovered
    # prospects flow into the auto-stake sweep pool.  Best-effort per-prospect.
    persisted_count = 0
    if req.persist_prospects and finalized:
        try:
            from backend.crm.persistence import upsert_prospect_record
            persisted_count = sum(1 for p in finalized if upsert_prospect_record(p))
            _LOG.info(
                "process_discovery persisted %d/%d prospects to DDB",
                persisted_count, len(finalized),
            )
        except Exception as _exc:  # noqa: BLE001 — never block discovery
            _LOG.warning("persist_prospects block failed: %s", _exc)

    # Step 3.8: freshness gate — the funnel's single promotion point. The
    # cumulative geo-ring re-queries the same zips daily and Places returns
    # the same businesses, so without memory the SAME prospects were promoted
    # as "new" every day (operator-reported 2026-07-03). A prospect promoted
    # within SAMUS_PROSPECT_RECYCLE_DAYS (default 30) is already in the
    # pipeline (CRM state, drafts, sends) and is held OUT of the call list;
    # when the cooldown lapses it re-qualifies — that lapse IS the recycle
    # pass. Fail-open: an unreadable ledger promotes everything (pre-3.8
    # behaviour) rather than starving production.
    from .promotion_ledger import (
        ever_promoted_ids,
        recently_promoted_ids,
        record_promotions,
    )

    already = recently_promoted_ids()
    fresh = [p for p in finalized if p.prospect_id not in already]
    held = len(finalized) - len(fresh)
    if held:
        _LOG.info(
            "freshness gate: %d/%d prospects held (promoted within %s-day "
            "cooldown); %d fresh promoted to the call list",
            held, len(finalized),
            os.environ.get("SAMUS_PROSPECT_RECYCLE_DAYS", "30") or "30",
            len(fresh),
        )

    # Step 3.9: recycle enrichment — a fresh prospect that was promoted
    # BEFORE (cooldown lapsed) is a RECYCLED prospect: mark it and attach
    # the compact CRM touch-history digest so every downstream composer
    # (email stake/opener, voicemail draft, dial context) opens as a
    # follow-up that PROGRESSES the conversation, never a cold restart.
    # Bounded + fail-soft: enrichment faults leave the prospect un-enriched.
    try:
        from .touch_context import build_touch_summary

        prior = ever_promoted_ids()
        recycled_n = 0
        for p in fresh:
            if p.prospect_id in prior:
                p.recycled = "true"
                p.prior_touch_summary = build_touch_summary(p.prospect_id)
                recycled_n += 1
        if recycled_n:
            _LOG.info(
                "recycle enrichment: %d/%d fresh prospects are returning "
                "(prior pipeline history attached)", recycled_n, len(fresh),
            )
    except Exception as exc:  # noqa: BLE001 — enrichment never blocks promotion
        _LOG.warning("recycle enrichment failed: %s", exc)

    record_promotions(fresh)

    # Step 4: CSV export + human-readable morning call list (FRESH only —
    # write_call_list merges into the existing day file, so an empty fresh
    # batch preserves the pool already promoted today).
    csv_path = write_call_list(fresh)
    txt_path = write_morning_call_list(fresh)

    result = DiscoveryResult(
        campaign_name=req.campaign_name,
        prospect_count=len(finalized),
        csv_path=str(csv_path),
        txt_path=str(txt_path),
        prospects=finalized,
        cache_hit=False,
        persisted_count=persisted_count,
        fresh_count=len(fresh),
        recycled_held_count=held,
    )

    audit_event = events.build_audit_event(
        service="prospecting",
        task_id=task_id,
        action="discover",
        input_payload=req.model_dump(),
        output_payload={
            "count": len(finalized),
            "csv_path": str(csv_path),
            "txt_path": str(txt_path),
        },
        status="completed",
    )
    try:
        _audit_ledger().append(audit_event)
    except OSError as exc:
        _LOG.warning("prospecting audit ledger append failed: %s", exc)

    GLOBAL_IDEMPOTENCY_STORE.set(cache_key, result.model_dump())
    _LOG.info(
        "process_discovery complete",
        extra={"task_id": task_id, "count": len(finalized), "csv_path": str(csv_path)},
    )
    return result


def _backfill_recent_contact(
    prospects: list[ProspectRecord],
) -> list[ProspectRecord]:
    """For each prospect, GET /crm/call-state/{prospect_id} and mirror the
    ``updated_at`` + ``last_outcome`` onto the record.

    Best-effort. A CRM-unreachable or per-prospect failure leaves the field
    empty and the prospect ranks normally (text_export._sort_key treats empty
    last_contact_at as "never contacted"). Skips prospects with no
    prospect_id (host break-glass runs that bypass CRM persistence).
    """
    crm_url = os.environ.get("CRM_URL", "http://samus-crm:8080").rstrip("/")
    if not crm_url:
        return prospects
    out: list[ProspectRecord] = []
    hits = 0
    for p in prospects:
        pid = (p.prospect_id or "").strip()
        if not pid:
            out.append(p)
            continue
        try:
            resp = signed_get_json_sync(
                crm_url,
                f"/crm/call-state/{pid}",
                timeout=3.0,
                retries=0,
            )
            if resp.status_code == 200:
                data = resp.json() or {}
                out.append(p.model_copy(update={
                    "last_contact_at": str(data.get("updated_at") or ""),
                    "last_contact_outcome": str(data.get("last_outcome") or ""),
                }))
                hits += 1
                continue
            # 404 / other = treat as no prior contact
        except Exception as exc:  # noqa: BLE001 — never block list build
            _LOG.debug("call_state lookup skipped for %s: %s", pid, exc)
        out.append(p)
    if hits:
        _LOG.info("call_state backfill: %d/%d prospects had prior contact", hits, len(out))
    return out
