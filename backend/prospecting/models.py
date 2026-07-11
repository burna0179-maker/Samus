"""Pydantic models for the prospecting workcell.

Field set on ProspectRecord maps 1:1 to the 32-column CSV schema in
``csv_export.CSV_COLUMNS``. The schema is fixed by the prior-iteration output
(`call_list_2026-05-07.csv`).
"""

from __future__ import annotations


from pydantic import BaseModel, ConfigDict, Field


class DiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_name: str = "daily_call_list"
    zipcodes: list[str] = Field(min_length=1)
    industries: list[str] = Field(default_factory=list)
    max_results_per_zip: int = 25
    radius_miles: int = 15
    must_have_website: bool = True
    exclude_domains: list[str] = Field(default_factory=list)
    # When True (default), process_discovery runs the local-first SEO audit
    # (HTTP GET + BeautifulSoup parse) on each prospect's website_url and
    # populates ProspectRecord.seo_score. Adds ~1-3 sec per prospect. Set
    # False in unit tests to keep them offline.
    enable_seo_audit: bool = True
    # When True (default), process_discovery extracts owner-contact signals
    # (emails, social links, owner_name via JSON-LD) from the prospect's
    # homepage + /contact + /about pages. Adds 0-2 extra HTTP calls per
    # prospect. Disable for offline tests.
    enable_owner_enrichment: bool = True
    # When True (default), the enrichment cascade additionally tries
    # mbasic.facebook.com/{handle}/about as a last-resort source when the
    # homepage + /contact + /about didn't yield an owner_email AND a
    # social_facebook URL was discovered. Facebook will block aggressively
    # over time — that's expected, failures degrade to empty signals.
    enable_facebook_enrichment: bool = True
    # When True (default), warm/hot prospects (call_priority != "low") get
    # the deep backend.seo.audit_and_report pipeline run on their website,
    # producing a full markdown report under
    # <artifacts>/customers/<slug>/seo_report.md . Cold/low prospects skip
    # this to bound LLM spend within the daily $1 cap. Adds ~5-15 sec per
    # warm prospect (deterministic audit + LLM content drafts).
    enable_full_audit_for_warm: bool = True
    # When True (default), process_discovery consults the strategy workcell's
    # hierarchical bandit (backend.strategy.portfolio_manager.select_best_policy)
    # once per distinct industry and stamps the chosen policy family onto each
    # ProspectRecord.policy_family. Pure in-process call (DDB-backed bandit with
    # a JSON fallback) — best-effort: if strategy is unavailable the field stays
    # "" and prospecting continues. Disable for offline tests / operator control.
    enable_strategy_policy: bool = True
    # DEFAULT FALSE — turning this on adds new LLM spend to the live 07:30
    # daily prospecting run. When True, process_discovery Step 3 builds the
    # personalised callsheet via build_call_sheet_smart_costed, which takes
    # the LLM path for top-N prospects (hot AND lead_score >= 75) when an API
    # key is configured. When False (default), Step 3 uses the deterministic
    # templated build_call_sheet at zero cost — byte-identical to the
    # pre-strategy-integration behaviour. Enabling daily callsheet LLM spend
    # is an explicit operator money decision, so it must be opted into here.
    # NOTE: this toggle does NOT affect the Step 2.7 warm/hot audit's own
    # content-draft LLM (backend.seo.audit_and_report) — that already fires
    # in production for warm/hot prospects today and its cost capture stays
    # unconditional regardless of this flag.
    enable_llm_callsheet: bool = False
    # When True (default), process_discovery runs the signal_filter
    # pre-qualification gate (Step 2c) after lead scoring: each scored
    # prospect is mapped to a backend.signal_filter ProspectSignal and run
    # through signal_filter.queue_gate.should_enqueue. Prospects that fail
    # the weighted admission threshold are dropped BEFORE the costly Step 2.6
    # strategy / Step 2.7 warm-audit / Step 3 callsheet-LLM paths, so a
    # low-probability prospect never burns queue throughput, SEO audit
    # cycles, or LLM budget. Pure deterministic logic — no network calls and
    # no LLM (the gate scores off the enrichment already on each record).
    # Best-effort: a signal_filter import / scoring fault leaves the prospect
    # set untouched. Disable for offline tests / operator control.
    enable_signal_filter_gate: bool = True
    # When True (default), process_discovery runs a website-status verification
    # re-poll (Step 3.5) once the main pipeline is done: every prospect whose
    # website_status is a *soft* failure (timeout / unreachable / 5xx / 4xx /
    # blocked / empty / DNS-miss — see service._RECHECK_STATUSES) is fetched
    # once more. A single transient crawl-time blip can mislabel a healthy site
    # as unreachable for the whole day and inflate its lead score off an
    # unmeasured SEO; a prospect that now responds is re-classified + re-scored
    # so the call list is factually correct. A prospect that still fails is
    # confirmed. Best-effort + per-prospect isolated. Disable for offline tests.
    enable_website_recheck: bool = True
    # When True, process_discovery dispatches each finalized prospect to CRM
    # via the gateway-HMAC signed-HTTP seam (POST /crm/prospects). Default
    # False = legacy behaviour where discovery writes the CSV/TXT call list
    # only and CRM is populated by other paths. The in-container cadence
    # (cadence_task.py) opts in by passing persist_prospects=True; the
    # service.py dispatcher itself is a follow-up — the field exists now so
    # cadence_task forward-port lands cleanly, and the dispatcher can be
    # wired without another model change.
    persist_prospects: bool = False


class ProspectRecord(BaseModel):
    """One row of the daily call list."""

    model_config = ConfigDict(extra="forbid")

    # discovery (Google Places)
    prospect_id: str = ""
    account_id: str = ""
    company_name: str = ""
    phone: str = ""
    website_url: str = ""
    city: str = ""
    state: str = ""
    zipcode: str = ""
    industry: str = ""

    # scoring (computed)
    call_priority: str = "low"
    lead_score: int = 0
    seo_score: int = 0

    # strategy "decide" step — the hierarchical-bandit policy-family arm
    # chosen for this prospect's `industry` at discovery time (set in
    # service.py Step 2.6 via backend.strategy.portfolio_manager.
    # select_best_policy). "" when strategy is disabled or unavailable.
    # Carried downstream so the eventual deal outcome can be attributed
    # back to the arm that picked it (the strategy "learn" step, Unit 3).
    policy_family: str = ""

    # website reachability — classified from the homepage fetch in service.py
    # Step 2.5. "" until checked; otherwise one of crawler.WEBSITE_STATUSES
    # ("live", "domain_unresolved", "unreachable_timeout", "unreachable",
    # "server_error", "access_blocked", "gone", "http_error", "empty",
    # "parked", "social_only") or "no_website" when the prospect has no
    # website_url at all. Any non-live value except "access_blocked" is
    # surfaced in the call list as a cold-call hook ("access_blocked" means a
    # WAF blocked our crawl — the site is fine for a human, so it is not).
    website_status: str = ""

    # owner enrichment (empty in v1)
    owner_name: str = ""
    owner_email: str = ""
    owner_title: str = ""
    owner_linkedin_url: str = ""
    contact_emails: str = ""

    # business intel (Google Places)
    review_rating: str = ""
    review_count: str = ""
    business_description: str = ""
    business_hours: str = ""
    business_categories: str = ""

    # socials (empty in v1)
    social_facebook: str = ""
    social_instagram: str = ""
    social_linkedin: str = ""

    # SEO/audit (empty in v1; populated when seo_audit lands)
    negative_review_snippets: str = ""

    # Full SEO audit report path -- only set for warm/hot prospects when
    # enable_full_audit_for_warm is on. Empty for cold/low prospects (which
    # don't get the deep audit to keep LLM spend bounded).
    seo_report_path: str = ""

    # Passive security & trust-posture grade (A/B/C/D/F) lifted from the Step
    # 2.7 warm/hot audit. Empty for cold/low prospects (not deep-audited) and
    # when the security audit is disabled. Used as a call-list tie-breaker —
    # a worse grade ranks the prospect higher (more trust problems to pitch).
    security_grade: str = ""

    # strategy "learn" step — total LLM dollars spent on this prospect during
    # discovery (strategy-integration build, Unit 4). Accumulates the priced
    # per-call cost of the two per-prospect LLM call sites in service.py: the
    # personalised callsheet (Step 3, Top-N-gated) and the warm/hot
    # audit_and_report content drafts (Step 2.7). 0.0 when neither LLM path
    # fired (templated callsheet + cold/low prospect, or no API key). Carried
    # downstream into the eventual deal's RewardSignal.token_cost_usd so the
    # reward-density model can tell a cheap win from an expensive one.
    llm_cost_usd: float = 0.0

    # callsheet (templated in v1; LLM-driven later)
    callsheet_issues: str = ""
    callsheet_offer: str = ""
    callsheet_pitch: str = ""
    callsheet_opener: str = ""
    callsheet_voicemail: str = ""
    callsheet_objections: str = ""
    # The single specific, TTS-safe audit finding (callsheet._top_finding output,
    # e.g. "a security warning grading the site an F"). Deliberately WITHHELD
    # from the gatekeeper-aware opener — the opener routes to the decision-maker
    # first without revealing it — so Morgan still needs it to deliver the real
    # pitch once the owner is on the line. Threaded to Vapi as {{callsheet_finding}}
    # via dialer._build_variable_values and spoken in the prompt's finding-inject
    # beat (Step 2), only after the owner is confirmed. Empty only when a
    # callsheet was never built for the record.
    callsheet_finding: str = ""

    # Recent-contact backref — populated post-finalize from the CRM's CallState
    # so the morning call list can down-rank prospects the operator already
    # worked. Empty = no prior contact (or CRM lookup degraded). Added
    # 2026-06-22 to make operator notes actually shape tomorrow's sequencing.
    last_contact_at: str = ""  # ISO-8601, e.g. "2026-06-22T17:09:58Z"
    last_contact_outcome: str = ""  # e.g. "booked" / "follow_up" / "noted"
    # Recycle-pass context (2026-07-03): a prospect whose promotion cooldown
    # lapsed re-enters as a FOLLOW-UP, not a first touch. ``recycled`` flags
    # it ("true"/""); ``prior_touch_summary`` is the compact CRM-history
    # digest (touch_context.build_touch_summary) that downstream compose
    # (email stake/opener, voicemail draft, dial context) weaves in so the
    # conversation PROGRESSES instead of restarting cold.
    recycled: str = ""
    prior_touch_summary: str = ""


class DiscoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_name: str
    prospect_count: int
    csv_path: str
    txt_path: str = ""
    prospects: list[ProspectRecord] = Field(default_factory=list)
    cache_hit: bool = False
    # Count of prospects successfully dispatched to CRM via the gateway-HMAC
    # signed-HTTP seam. 0 when persist_prospects is False on the request, or
    # when the dispatcher is not yet wired (forward-port follow-up). Cadence
    # task logs this for its summary; tests check it for sanity.
    persisted_count: int = 0
    # Freshness gate (step 3.8, 2026-07-03): how many of prospect_count were
    # NEW to the pipeline (promoted onto the call list) vs held because they
    # were already promoted within the recycle cooldown. fresh_count is the
    # ring-exhaustion signal the cadence auto-advance keys on.
    fresh_count: int = 0
    recycled_held_count: int = 0
