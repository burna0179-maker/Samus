#!/usr/bin/env python3
"""
ProspectWorker — ZIP-code-driven local business discovery + qualification lane
Source: ChatGPT recovery chat 08 (prospecting pipeline architecture)

Canonical relationship:
- [NEW pack] business/prospecting — first-class workcell on its own queue
- [EXPANDS §6 orchestration] new action set: discover_businesses | qualify_website
                              | audit_site (reuse SEO) | score_lead (reuse leadgen)
                              | build_call_sheet
- [EXPANDS §6 inter_agent] cross-lane handoff: prospect → seo → leadgen → proposal/outreach
- Matches memory: project_samus_crm_design (v1.7 prospect lane — never ran due to Stripe env crash)

Queue pair (target deployment):
  samus-prospect-jobs / samus-prospect-dlq

API entrypoint: POST /intake/prospect/zipcode

Pipeline flow:
  ZIP → discover_businesses → qualify_website → audit_site → score_lead → build_call_sheet → call_ready
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class ProspectStatus(str, Enum):
    DISCOVERED = "discovered"
    QUALIFIED = "qualified"
    AUDITED = "audited"
    CALL_READY = "call_ready"
    CALLED = "called"
    INTERESTED = "interested"
    PROPOSAL_SENT = "proposal_sent"
    CLOSED_WON = "closed_won"
    CLOSED_LOST = "closed_lost"


@dataclass
class DiscoveryRequest:
    campaign_name: str
    zipcodes: List[str]
    keywords: List[str] = field(default_factory=list)
    business_types: List[str] = field(default_factory=list)
    radius_miles: int = 15
    max_results_per_zip: int = 25
    must_have_website: bool = True
    exclude_domains: List[str] = field(default_factory=list)


@dataclass
class ProspectRecord:
    prospect_id: str
    company_name: str
    zipcode: str
    city: str = ""
    state: str = ""
    phone: str = ""
    website_url: str = ""
    industry: str = ""
    source: str = "google_places"
    seo_score: float = 0.0
    lead_score: float = 0.0
    status: ProspectStatus = ProspectStatus.DISCOVERED
    call_priority: str = "low"
    last_crawled_at: Optional[str] = None
    notes: str = ""
    assigned_to: Optional[str] = None
    attempt_count: int = 0


# -------------------------------------------------------------------
# Crawl guardrails — DO NOT REMOVE without review
# -------------------------------------------------------------------
class CrawlPolicy:
    MAX_PAGES_PER_DOMAIN = 4
    ALLOWED_PATHS = ("/", "/about", "/contact", "/services", "/index", "/home")
    REQUEST_TIMEOUT_SEC = 10
    RESPECT_ROBOTS = True
    DEDUPE_KEYS = ("domain", "phone", "company_name")
    CACHE_TTL_HOURS = 168              # 7 days — don't re-hit same domain
    BURST_LIMIT_PER_ZIP = 50           # max discoveries per ZIP per hour
    PROVENANCE_REQUIRED = True         # every lead must record its source


# -------------------------------------------------------------------
# Worker
# -------------------------------------------------------------------
class ProspectWorker:
    """Skeleton — wire to BaseSqsWorker in target env."""

    METRICS_PORT_DEFAULT = 9105

    ACTIONS = ("discover_businesses", "qualify_website", "build_call_sheet")

    def __init__(self, dispatch_fn: Callable[..., None], crm: Any, place_search: Any, crawler: Any):
        self.dispatch = dispatch_fn
        self.crm = crm
        self.place_search = place_search
        self.crawler = crawler

    def handle(self, envelope) -> Dict[str, Any]:
        action = envelope.action
        if action == "discover_businesses":
            return self._discover(envelope)
        if action == "qualify_website":
            return self._qualify(envelope)
        if action == "build_call_sheet":
            return self._call_sheet(envelope)
        raise ValueError(f"Unsupported prospect action: {action}")

    # ----- phase 1: discover -----
    def _discover(self, envelope) -> Dict[str, Any]:
        req = DiscoveryRequest(**envelope.payload)
        discovered: List[ProspectRecord] = []
        for zc in req.zipcodes:
            for biz in self.place_search.find_local_businesses(
                zipcode=zc,
                keywords=req.keywords,
                radius_miles=req.radius_miles,
                limit=req.max_results_per_zip,
            ):
                if req.must_have_website and not biz.get("website"):
                    continue
                if biz.get("domain") in req.exclude_domains:
                    continue
                rec = ProspectRecord(
                    prospect_id=f"prospect-{biz['place_id']}",
                    company_name=biz["name"],
                    zipcode=zc,
                    city=biz.get("city", ""),
                    state=biz.get("state", ""),
                    phone=biz.get("phone", ""),
                    website_url=biz.get("website", ""),
                    industry=biz.get("category", ""),
                )
                self.crm.put_prospect(rec)
                discovered.append(rec)
                self.dispatch(service="prospect", action="qualify_website",
                              payload={"prospect_id": rec.prospect_id})
        return {"status": "discovered", "count": len(discovered)}

    # ----- phase 2: qualify -----
    def _qualify(self, envelope) -> Dict[str, Any]:
        prospect_id = envelope.payload["prospect_id"]
        rec = self.crm.get_prospect(prospect_id)
        page = self.crawler.fetch_homepage(rec.website_url, policy=CrawlPolicy)
        if self._is_dead_or_junk(page):
            rec.status = ProspectStatus.CLOSED_LOST
            rec.notes = "qualify_website: dead/parked/social-only"
            self.crm.put_prospect(rec)
            return {"status": "rejected"}
        rec.status = ProspectStatus.QUALIFIED
        self.crm.put_prospect(rec)
        self.dispatch(service="seo", action="audit_site",
                      payload={"url": rec.website_url, "prospect_id": prospect_id})
        return {"status": "qualified"}

    # ----- phase 3: call sheet (after audit_site + score_lead results return) -----
    def _call_sheet(self, envelope) -> Dict[str, Any]:
        prospect_id = envelope.payload["prospect_id"]
        rec = self.crm.get_prospect(prospect_id)
        sheet = self._build_call_sheet(rec)
        self.crm.create_artifact({"type": "CALL_SHEET", "prospect_id": prospect_id, "data": sheet})
        rec.status = ProspectStatus.CALL_READY
        rec.call_priority = self._priority_from_scores(rec)
        self.crm.put_prospect(rec)
        return {"status": "call_ready", "priority": rec.call_priority}

    # ----- helpers -----
    def _is_dead_or_junk(self, page: Dict[str, Any]) -> bool:
        if not page or not page.get("html"):
            return True
        url = (page.get("final_url") or "").lower()
        if any(s in url for s in ("facebook.com", "instagram.com", "yelp.com", "yellowpages")):
            return True
        if page.get("status_code", 0) >= 400:
            return True
        return False

    def _build_call_sheet(self, rec: ProspectRecord) -> Dict[str, Any]:
        return {
            "company": rec.company_name,
            "phone": rec.phone,
            "website": rec.website_url,
            "zip": rec.zipcode,
            "city": rec.city,
            "industry": rec.industry,
            "seo_issues": [],                  # filled by audit_site result
            "likely_pain": "",                 # filled by score_lead result
            "pitch_angle": "",                 # filled by callsheet_product_registry
            "monthly_service_fit": "",
            "urgency": "",
            "first_call_opener": "",
            "voicemail": "",
        }

    def _priority_from_scores(self, rec: ProspectRecord) -> str:
        if rec.lead_score >= 75:
            return "hot"
        if rec.lead_score >= 50:
            return "warm"
        return "low"


# Additional outreach actions to extend the existing outreach worker for phone flow:
PHONE_FLOW_ACTIONS = (
    "log_call_attempt",
    "log_call_outcome",
    "schedule_callback",
    "trigger_proposal_after_call",
)
