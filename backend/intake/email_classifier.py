"""Inbound email classifier -- heuristic categorization for business inbox.

Classifies each inbound email into one of the categories a business
operating system cares about:

  - **client_correspondence** : reply from a known/existing client — sender
    email is listed in ``clients/<slug>/campaign.yaml`` (resolved via
    :mod:`backend.crm.client_directory`). Takes precedence over ``business``
    so signed-client correspondence routes to a customer-service path
    instead of the lead-nurture path.
  - **bill**       : invoices, receipts, payment confirmations/declines
  - **account**    : service health alerts, quota warnings, security notices
  - **calendar**   : booking confirmations, meeting invites, schedule changes
  - **developer**  : CI/CD, deploy, git, build, monitoring alerts
  - **social**     : LinkedIn, social media notifications
  - **business**   : actual correspondence from prospects/partners (unknown senders)
  - **marketing**  : newsletters, promotions, cold outreach from others
  - **other**      : unclassifiable

For bills, cross-references against the CODB vendor registry via
:mod:`backend.finance.gmail_bill_scan` to extract vendor, amount, and
signal kind (receipt/invoice/payment_declined/renewal).

All classification is heuristic (no LLM, no network calls). Runs
synchronously in the poller's per-message path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from backend.intake.gmail_poller import ParsedInboundEmail

CategoryType = Literal[
    "client_correspondence",
    "bill",
    "account",
    "calendar",
    "developer",
    "social",
    "business",
    "marketing",
    "other",
]


DirectionType = Literal["inbound", "outbound"]


@dataclass
class EmailClassification:
    category: CategoryType
    confidence: float
    vendor_domain: str = ""
    vendor_registry_id: str = ""
    bill_amount_usd: float | None = None
    bill_signal_kind: str = ""
    # Populated when category == "client_correspondence".
    client_id: str = ""
    campaign_id: str = ""
    client_role: str = ""
    direction: DirectionType = "inbound"
    # For an outbound (forwarded operator reply) — the ORIGINAL recipient,
    # subject, and date extracted from the forward preamble.
    original_to: str = ""
    original_subject: str = ""
    original_date: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "category": self.category,
            "confidence": round(self.confidence, 2),
        }
        if self.vendor_domain:
            d["vendor_domain"] = self.vendor_domain
        if self.vendor_registry_id:
            d["vendor_registry_id"] = self.vendor_registry_id
        if self.bill_amount_usd is not None:
            d["bill_amount_usd"] = self.bill_amount_usd
        if self.bill_signal_kind:
            d["bill_signal_kind"] = self.bill_signal_kind
        if self.client_id:
            d["client_id"] = self.client_id
        if self.campaign_id:
            d["campaign_id"] = self.campaign_id
        if self.client_role:
            d["client_role"] = self.client_role
        if self.direction != "inbound":
            d["direction"] = self.direction
        if self.original_to:
            d["original_to"] = self.original_to
        if self.original_subject:
            d["original_subject"] = self.original_subject
        if self.original_date:
            d["original_date"] = self.original_date
        if self.tags:
            d["tags"] = self.tags
        return d


_SOCIAL_DOMAINS = frozenset(
    {
        "linkedin.com",
        "facebookmail.com",
        "twitter.com",
        "x.com",
        "instagram.com",
        "tiktok.com",
        "reddit.com",
        "pinterest.com",
    }
)

_CALENDAR_DOMAINS = frozenset(
    {
        "calendly.com",
        "calendar-notification@google.com",
    }
)
_CALENDAR_SUBJECT_RE = re.compile(
    r"(booking|booked|confirmed|invitation|meeting|calendar|rsvp|schedule|reschedule|cancel)",
    re.IGNORECASE,
)

_DEVELOPER_DOMAINS = frozenset(
    {
        "gitlab.com",
        "github.com",
        "bitbucket.org",
        "circleci.com",
        "app.circleci.com",
        "vercel.com",
        "netlify.com",
        "render.com",
        "sentry.io",
        "pagerduty.com",
        "statuspage.io",
        "datadog.com",
        "newrelic.com",
    }
)
_DEVELOPER_SUBJECT_RE = re.compile(
    r"(deploy|build|pipeline|ci/cd|merge request|pull request|commit|branch|release|incident|alert|downtime)",
    re.IGNORECASE,
)

_ACCOUNT_SUBJECT_RE = re.compile(
    r"(quota|limit|threshold|capacity|usage|health|security|password|auth|verification|suspended|disabled|warning|exceeded|action required)",
    re.IGNORECASE,
)
_ACCOUNT_DOMAINS = frozenset(
    {
        "grafana.com",
        "alerts.grafana.com",
    }
)

_MARKETING_SUBJECT_RE = re.compile(
    r"(newsletter|unsubscribe|weekly digest|roundup|tips|webinar|announcement|% off|free trial|limited time)",
    re.IGNORECASE,
)


def classify(parsed: ParsedInboundEmail) -> EmailClassification:
    """Classify one parsed inbound email. Never raises."""
    from_addr = (parsed.from_addr or "").lower()
    from_domain = from_addr.rsplit("@", 1)[-1] if "@" in from_addr else ""
    subject = parsed.subject or ""
    # HTML-heavy Titan/Gmail-HTML bodies compress 10-20x when tags are
    # stripped. Strip first when the body looks like HTML, then window —
    # this way the 2000-char classification budget lands on actual
    # content (forward preambles, client mentions, keyword signals)
    # instead of getting eaten by inline styles + wrapper divs.
    raw = parsed.body_text or ""
    from backend.intake.forwarded_email import _looks_like_html, strip_html

    if _looks_like_html(raw):
        # Larger raw window to compensate for HTML overhead — 20 KB of
        # HTML typically distills to ~2-4 KB of readable text.
        cleaned = strip_html(raw[:20000])
    else:
        cleaned = raw
    body_head = cleaned[:2000]
    tags: list[str] = []

    # --- KNOWN CLIENT — highest priority ---
    # Three symmetric branches under one category:
    #   A. INBOUND  : sender IS a known client (from -> us)
    #   B. OUTBOUND : sender IS the operator + forward preamble names a
    #                 known client (any recipient, OR body/subject mention)
    #   C. INBOUND (content) : sender is unknown but subject/body strongly
    #                 mentions a known client (e.g. Kerry replies from a
    #                 personal address, or a third party emails us about
    #                 Conquerors)
    # All route to client_correspondence so a client's full thread is
    # visible chronologically. Subject-line heuristics (billing/calendar)
    # never override — client identity is source-of-truth.
    try:
        from backend.crm.client_directory import (
            find_client_in_text,
            is_operator_address,
            lookup_client,
        )

        # A) inbound from known client (address match)
        client = lookup_client(from_addr)
        if client is not None:
            tags.append(client.client_id)
            if client.role:
                tags.append(client.role)
            return EmailClassification(
                category="client_correspondence",
                confidence=1.0,
                client_id=client.client_id,
                campaign_id=client.campaign_id,
                client_role=client.role,
                direction="inbound",
                tags=tags,
            )

        # Content haystack for content-based checks: subject + already-
        # stripped body_head. body_head is HTML-cleaned at the top of
        # classify() so no re-stripping needed here.
        haystack = f"{subject}\n\n{body_head}"

        # B) OUTBOUND from operator — the operator forwarded a sent email
        # to samus's inbox for archival.
        if is_operator_address(from_addr):
            from backend.intake.forwarded_email import parse_forwarded_body

            headers = parse_forwarded_body(body_head)
            hit: KnownClient | None = None  # type: ignore[name-defined]  # noqa: F821
            hit_to = ""
            # B.1 forward preamble names any known recipient (checks the
            # first recipient AND every additional address in the To: line)
            if headers is not None:
                for addr in (headers.to_addr, *headers.all_to_addrs):
                    if not addr:
                        continue
                    kc = lookup_client(addr)
                    if kc is not None:
                        hit = kc
                        hit_to = addr
                        break
            # B.2 subject or body mentions a known client (Titan/HTML
            # forwards whose preamble scan failed; operator-on-behalf-of
            # emails whose recipient isn't yet in the directory)
            if hit is None:
                hit = find_client_in_text(haystack)
                if hit is not None:
                    hit_to = headers.to_addr if headers is not None else ""

            if hit is not None:
                tags.append(hit.client_id)
                tags.append("forwarded_by_operator")
                return EmailClassification(
                    category="client_correspondence",
                    confidence=1.0,
                    client_id=hit.client_id,
                    campaign_id=hit.campaign_id,
                    client_role=hit.role,
                    direction="outbound",
                    original_to=hit_to,
                    original_subject=(headers.subject if headers else ""),
                    original_date=(headers.date if headers else ""),
                    tags=tags,
                )

        # C) INBOUND content-based: sender is unknown, but subject/body
        # mentions a known client. Common with third-party partners
        # emailing us ABOUT a client engagement.
        content_hit = find_client_in_text(haystack)
        if content_hit is not None:
            tags.append(content_hit.client_id)
            tags.append("content_match")
            return EmailClassification(
                category="client_correspondence",
                confidence=0.9,  # lower than direct-address match
                client_id=content_hit.client_id,
                campaign_id=content_hit.campaign_id,
                client_role=content_hit.role,
                direction="inbound",
                tags=tags,
            )
    except Exception:  # noqa: BLE001 — directory unavailable falls through
        pass

    # --- Account alerts take priority over vendor-matched bills ---
    # (AWS health alerts come from aws.amazon.com, a known vendor, but
    # are NOT bills — they're quota/health/security notices.)
    is_account_alert = bool(
        _ACCOUNT_SUBJECT_RE.search(subject)
        or _domain_match(from_domain, _ACCOUNT_DOMAINS)
        or "health" in from_addr
    )

    # --- Bill detection (reuse gmail_bill_scan vendor matching) ---
    try:
        from backend.finance.gmail_bill_scan import (
            classify_signal_kind,
            extract_amount,
            match_vendor,
            _pick_registry_id,
        )

        vendor_domain, candidates = match_vendor(from_addr, subject)
        if vendor_domain or _has_billing_keywords(subject):
            text = f"{subject}\n{body_head}"
            amount = extract_amount(text)
            signal_kind = classify_signal_kind(subject, body_head)
            registry_id = (
                _pick_registry_id(
                    vendor_domain,
                    candidates,
                    subject,
                    amount,
                )
                if candidates
                else ""
            )

            # If the signal_kind is "other" AND account-alert keywords match,
            # this is an alert from a known vendor, not a bill.
            if is_account_alert and signal_kind == "other":
                tags.append("alert")
                if vendor_domain:
                    tags.append(vendor_domain)
                return EmailClassification(
                    category="account",
                    confidence=0.8,
                    tags=tags,
                )

            if vendor_domain or signal_kind != "other":
                if signal_kind == "payment_declined":
                    tags.append("payment_declined")
                if amount is not None:
                    tags.append(f"${amount:.2f}")
                return EmailClassification(
                    category="bill",
                    confidence=0.9 if vendor_domain else 0.6,
                    vendor_domain=vendor_domain,
                    vendor_registry_id=registry_id,
                    bill_amount_usd=amount,
                    bill_signal_kind=signal_kind,
                    tags=tags,
                )
    except Exception:  # noqa: BLE001
        pass

    # --- Social notifications ---
    if _domain_match(from_domain, _SOCIAL_DOMAINS):
        return EmailClassification(category="social", confidence=0.95, tags=tags)

    # --- Calendar / booking ---
    if _domain_match(from_domain, _CALENDAR_DOMAINS) or _CALENDAR_SUBJECT_RE.search(subject):
        tags.append("booking" if "book" in subject.lower() else "calendar")
        return EmailClassification(category="calendar", confidence=0.8, tags=tags)

    # --- Developer / CI ---
    if _domain_match(from_domain, _DEVELOPER_DOMAINS) or _DEVELOPER_SUBJECT_RE.search(subject):
        return EmailClassification(category="developer", confidence=0.8, tags=tags)

    # --- Account alerts (AWS health, Grafana, quota warnings) ---
    if (
        _domain_match(from_domain, _ACCOUNT_DOMAINS)
        or _ACCOUNT_SUBJECT_RE.search(subject)
        or "aws.amazon.com" in from_domain
        or "health" in from_addr
    ):
        tags.append("alert")
        return EmailClassification(category="account", confidence=0.7, tags=tags)

    # --- Marketing / newsletters ---
    if (
        _MARKETING_SUBJECT_RE.search(subject)
        or "noreply" in from_addr
        or "no-reply" in from_addr
        or "newsletter" in from_addr
    ):
        return EmailClassification(category="marketing", confidence=0.5, tags=tags)

    # --- Business (catch-all for real correspondence) ---
    if _looks_like_business(from_addr, subject, body_head):
        return EmailClassification(category="business", confidence=0.4, tags=tags)

    return EmailClassification(category="other", confidence=0.3, tags=tags)


def _domain_match(domain: str, known: frozenset) -> bool:
    if domain in known:
        return True
    return any(domain.endswith("." + k) for k in known)


def _has_billing_keywords(subject: str) -> bool:
    s = subject.lower()
    return any(
        kw in s
        for kw in (
            "invoice",
            "receipt",
            "statement",
            "payment",
            "billing",
            "declined",
            "charge",
            "renewal",
            "subscription",
        )
    )


def _looks_like_business(from_addr: str, subject: str, body: str) -> bool:
    if not from_addr or "noreply" in from_addr or "no-reply" in from_addr:
        return False
    # Personal-looking from addresses (name@domain, not system@domain)
    local = from_addr.split("@")[0] if "@" in from_addr else ""
    if "." in local or "_" in local:
        return True
    if any(w in subject.lower() for w in ("re:", "fwd:", "follow up", "proposal", "question")):
        return True
    return False
