"""Gmail-based bill/invoice scanner — cross-referenced against the CODB registry.

Once the operator has authorized Gmail OAuth (``scripts/Authorize-Gmail.ps1``),
this module searches the mailbox (READ-ONLY: list + get only, never
mark-as-read / modify / send) for vendor billing emails — invoices, receipts,
statements, payment confirmations, payment-declined notices — and compiles a
"bills snapshot" that cross-references observed dollar amounts against
``codb_registry.yaml`` so estimates can be refined with real numbers.

Reuses the existing Gmail transport (:mod:`backend.intake.gmail_api_client`)
and RFC822 parsing helpers (:mod:`backend.intake.gmail_poller`) rather than
building a second Gmail client or a second MIME parser.

NEVER writes to ``codb_registry.yaml`` — this is a read-only reconnaissance
tool. Refining the registry stays a manual operator edit, matching the
"recommend-only" pattern used by ``backend/cognitive/codb_reasoner.py``.
"""
from __future__ import annotations

import email
import email.utils
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from backend.common.config import get_settings
from backend.common.dates import iso_now
from backend.finance.codb import load_registry
from backend.finance.models import CodbRegistry
from backend.intake.gmail_api_client import GmailApiClient, GmailApiError
from backend.intake.gmail_poller import parse_rfc822


_LOG = logging.getLogger("samus.finance.gmail_bill_scan")


# ---------------------------------------------------------------------------
# Vendor mapping — best-effort keyword/domain -> codb_registry.yaml cost id
# ---------------------------------------------------------------------------
#
# Values are a list of *candidate* registry ids: usually one, occasionally
# two (e.g. openai.com covers both the API bill and the ChatGPT Plus
# consumer subscription — distinguished, when possible, by subject/amount
# heuristics in `_pick_openai_candidate`). ``None`` id list entries flag a
# vendor seen in Gmail with NO current registry entry — surfaced separately
# as "consider adding" rather than silently dropped.

KNOWN_VENDORS: dict[str, list[str]] = {
    "anthropic.com": ["anthropic-claude-subscription"],
    "openai.com": ["openai-api-samus-inference", "openai-chatgpt-plus"],
    "twilio.com": ["twilio-telephony"],
    "vapi.ai": ["vapi-voice-calls"],
    "wordpress.com": ["wordpress-com-domain"],
    "google.com": ["google-workspace-hustleforge"],
    "workspace.google.com": ["google-workspace-hustleforge"],
    "cloud.google.com": ["gcp-cloud-run"],
    "aws.amazon.com": ["aws-sqs", "aws-dynamodb", "aws-sns", "aws-ses", "aws-other"],
    "amazon.com": ["aws-sqs", "aws-dynamodb", "aws-sns", "aws-ses", "aws-other"],
    "pge.com": ["pge-energy"],
    "xfinity.com": ["xfinity-internet-cable"],
    "comcast.com": ["xfinity-internet-cable"],
    "spotify.com": ["spotify-premium"],
    # No current registry entry — flagged as NEW (unmatched_new_vendor bucket).
    "sendgrid.com": [],
    "apollo.io": [],
}

# Domains that disambiguate to a SINGLE aws-* bucket by subject keyword —
# checked before falling back to "aws-other".
_AWS_SERVICE_HINTS: dict[str, str] = {
    "sqs": "aws-sqs",
    "dynamodb": "aws-dynamodb",
    "sns": "aws-sns",
    "ses": "aws-ses",
}

# Subject keywords that flag a billing-related email even when the sender
# domain isn't in KNOWN_VENDORS — used to find genuinely NEW vendors.
_SUBJECT_KEYWORDS = (
    "invoice", "receipt", "statement", "payment", "billing",
    "declined", "charge", "renewal",
)

UNMATCHED_BUCKET = "unmatched_new_vendor"

SignalKind = Literal["receipt", "invoice", "payment_declined", "renewal_notice", "other"]

_AMOUNT_RE = re.compile(r"\$[\d,]+\.?\d*")
_PRIORITY_CONTEXT_RE = re.compile(
    r"(total|charged|due|amount)\D{0,20}(\$[\d,]+\.?\d*)", re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Vendor matching
# ---------------------------------------------------------------------------

def _domain_from_addr(from_addr: str) -> str:
    """Bare lower-cased domain from an email address, '' if unparseable."""
    addr = email.utils.parseaddr(from_addr)[1] or from_addr
    addr = (addr or "").strip().lower()
    if "@" not in addr:
        return ""
    return addr.rsplit("@", 1)[-1]


def match_vendor(from_addr: str, subject: str = "") -> tuple[str, list[str]]:
    """Match a sender address against :data:`KNOWN_VENDORS`.

    Returns ``(matched_domain, candidate_ids)``. ``matched_domain`` is ``""``
    when no vendor domain matched (caller should fall back to subject-keyword
    detection for the unmatched-vendor bucket). ``candidate_ids`` may be
    empty for a known-but-unregistered vendor (e.g. sendgrid.com).
    """
    domain = _domain_from_addr(from_addr)
    if not domain:
        return "", []
    # Exact match first, then suffix match (mail.pge.com -> pge.com).
    if domain in KNOWN_VENDORS:
        return domain, list(KNOWN_VENDORS[domain])
    for known_domain, ids in KNOWN_VENDORS.items():
        if domain == known_domain or domain.endswith("." + known_domain):
            return known_domain, list(ids)
    return "", []


def _pick_openai_candidate(candidates: list[str], subject: str, amount: float | None) -> str:
    """Disambiguate openai.com -> API billing vs ChatGPT Plus.

    Heuristic: subject text is the strongest signal ("chatgpt", "plus"
    -> consumer plan); otherwise the flat $20/mo Plus price is a fair
    amount-based tiebreak. Falls back to the first candidate (API billing,
    the higher-criticality item) when ambiguous — better to over-attribute
    to the critical item than silently drop the signal.
    """
    subj_l = subject.lower()
    if "chatgpt" in subj_l or "plus" in subj_l:
        return "openai-chatgpt-plus"
    if "api" in subj_l or "platform" in subj_l:
        return "openai-api-samus-inference"
    if amount is not None and 18.0 <= amount <= 22.0:
        return "openai-chatgpt-plus"
    return candidates[0] if candidates else "openai-api-samus-inference"


def _pick_registry_id(domain: str, candidates: list[str], subject: str, amount: float | None) -> str:
    """Resolve ``candidates`` (possibly multiple) down to one registry id."""
    if not candidates:
        return UNMATCHED_BUCKET
    if len(candidates) == 1:
        return candidates[0]
    if domain == "openai.com":
        return _pick_openai_candidate(candidates, subject, amount)
    if domain in ("aws.amazon.com", "amazon.com"):
        subj_l = subject.lower()
        for hint, cid in _AWS_SERVICE_HINTS.items():
            if hint in subj_l:
                return cid
        return "aws-other"
    return candidates[0]


# ---------------------------------------------------------------------------
# Amount + signal-kind extraction
# ---------------------------------------------------------------------------

def extract_amount(text: str) -> float | None:
    """Best-effort dollar amount from subject+body text.

    Prefers an amount adjacent to "total"/"charged"/"due"/"amount"; falls
    back to the largest plausible `$...` match in the text (guards against
    picking up a stray small number like a line-item quantity). Returns
    ``None`` when no amount-looking token is found.
    """
    if not text:
        return None
    priority = _PRIORITY_CONTEXT_RE.search(text)
    if priority:
        return _to_float(priority.group(2))
    matches = _AMOUNT_RE.findall(text)
    if not matches:
        return None
    values = [v for v in (_to_float(m) for m in matches) if v is not None]
    if not values:
        return None
    return max(values)


def _to_float(raw: str) -> float | None:
    cleaned = raw.replace("$", "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def classify_signal_kind(subject: str, body: str) -> SignalKind:
    """Classify a billing email into a coarse kind from subject+body text."""
    text = f"{subject} {body}".lower()
    if "declined" in text or "failed" in text or "could not be processed" in text:
        return "payment_declined"
    if "renew" in text:
        return "renewal_notice"
    if "receipt" in text or "payment received" in text or "paid" in text:
        return "receipt"
    if "invoice" in text or "statement" in text or "billing" in text:
        return "invoice"
    return "other"


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------

@dataclass
class RawMatch:
    """One Gmail search hit before parsing — gmail_id + raw RFC822 bytes."""
    gmail_id: str
    raw: bytes


@dataclass
class BillSignal:
    """One parsed + classified billing email."""
    gmail_id: str
    message_id: str
    from_addr: str
    from_domain: str
    subject: str
    date_header: str
    amount_usd: float | None
    signal_kind: SignalKind
    matched_registry_id: str   # codb id, "aws-other", or UNMATCHED_BUCKET
    snippet: str = ""          # first ~200 chars of body, for operator context

    def to_dict(self) -> dict[str, Any]:
        return {
            "gmail_id": self.gmail_id,
            "message_id": self.message_id,
            "from_addr": self.from_addr,
            "from_domain": self.from_domain,
            "subject": self.subject,
            "date_header": self.date_header,
            "amount_usd": self.amount_usd,
            "signal_kind": self.signal_kind,
            "matched_registry_id": self.matched_registry_id,
            "snippet": self.snippet,
        }


@dataclass
class VendorBillRow:
    """One row in the compiled snapshot — one registry id (or bucket)."""
    registry_id: str
    vendor_label: str                  # registry name, or from_domain if unmatched
    latest_observed_usd: float | None
    last_seen_date: str                # raw Date header of the most recent signal
    registry_estimate_usd: float | None
    delta_usd: float | None
    signal_count: int
    payment_declined: bool             # True if ANY signal for this vendor was declined
    flag: str                          # human-readable flag for the summary table

    def to_dict(self) -> dict[str, Any]:
        return {
            "registry_id": self.registry_id,
            "vendor_label": self.vendor_label,
            "latest_observed_usd": self.latest_observed_usd,
            "last_seen_date": self.last_seen_date,
            "registry_estimate_usd": self.registry_estimate_usd,
            "delta_usd": self.delta_usd,
            "signal_count": self.signal_count,
            "payment_declined": self.payment_declined,
            "flag": self.flag,
        }


@dataclass
class BillsSnapshot:
    """Compiled bills snapshot — the CLI's JSON output shape."""
    ts: str
    lookback_days: int
    signals_scanned: int
    rows: list[VendorBillRow] = field(default_factory=list)
    at_risk_vendor_ids: list[str] = field(default_factory=list)
    unmatched_vendors: list[str] = field(default_factory=list)  # domains, not ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "lookback_days": self.lookback_days,
            "signals_scanned": self.signals_scanned,
            "rows": [r.to_dict() for r in self.rows],
            "at_risk_vendor_ids": self.at_risk_vendor_ids,
            "unmatched_vendors": self.unmatched_vendors,
        }


# ---------------------------------------------------------------------------
# Step 2: search + fetch (READ-ONLY — list + get only)
# ---------------------------------------------------------------------------

def _build_search_query(lookback_days: int) -> str:
    """Combine vendor domains OR subject keywords, restricted to a window.

    Gmail search syntax: ``(from:a OR from:b OR subject:c OR ...) newer_than:Nd``.
    """
    domain_terms = [f"from:{d}" for d in KNOWN_VENDORS.keys()]
    subject_terms = [f"subject:{kw}" for kw in _SUBJECT_KEYWORDS]
    or_clause = " OR ".join(domain_terms + subject_terms)
    return f"({or_clause}) newer_than:{max(1, int(lookback_days))}d"


def search_billing_emails(
    client: Any,
    *,
    lookback_days: int = 90,
    max_results: int = 200,
) -> list[RawMatch]:
    """List + fetch Gmail messages matching the billing search query.

    READ-ONLY: calls only ``list_unread_message_ids``-equivalent search and
    ``fetch_raw``. Never calls ``mark_read`` or any modify/send endpoint —
    this scanner must never mutate mailbox state.

    ``client`` exposes a Gmail-search method; production callers pass a
    :class:`GmailApiClient` (already ``__enter__``-ed by the caller so the
    OAuth token is loaded/refreshed once for the whole scan). Tests pass a
    fake exposing the same surface.
    """
    query = _build_search_query(lookback_days)
    if hasattr(client, "search_message_ids"):
        message_ids = client.search_message_ids(query=query, max_results=max_results)
    else:
        # Fallback for a bare GmailApiClient that only exposes the unread-only
        # list method (shouldn't happen once search_message_ids is added, but
        # keeps this function usable against the narrower fake in older tests).
        message_ids = client.list_unread_message_ids(max_results=max_results)

    matches: list[RawMatch] = []
    for gmail_id in message_ids:
        try:
            raw = client.fetch_raw(gmail_id)
        except Exception as exc:  # noqa: BLE001 — one bad message must not kill the scan
            _LOG.warning("gmail_bill_scan fetch_raw failed gmail_id=%s: %s", gmail_id, exc)
            continue
        matches.append(RawMatch(gmail_id=gmail_id, raw=raw))
    return matches


# ---------------------------------------------------------------------------
# Step 3: parse one message -> BillSignal
# ---------------------------------------------------------------------------

def parse_bill_signal(raw_message: RawMatch) -> BillSignal | None:
    """Parse one raw Gmail match into a :class:`BillSignal`.

    Best-effort — fail-soft per message (matches the poller's per-message
    pattern): returns ``None`` (never raises) on an unparseable message so
    the caller can skip + log without losing the rest of the batch.
    """
    try:
        parsed = parse_rfc822(raw_message.raw)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(
            "gmail_bill_scan parse failed gmail_id=%s: %s", raw_message.gmail_id, exc,
        )
        return None

    try:
        domain, candidates = match_vendor(parsed.from_addr, parsed.subject)
        text_for_amount = f"{parsed.subject}\n{parsed.body_text[:2000]}"
        amount = extract_amount(text_for_amount)
        registry_id = _pick_registry_id(domain, candidates, parsed.subject, amount)
        signal_kind = classify_signal_kind(parsed.subject, parsed.body_text)
        snippet = (parsed.body_text or "").strip()[:200]
        return BillSignal(
            gmail_id=raw_message.gmail_id,
            message_id=parsed.message_id,
            from_addr=parsed.from_addr,
            from_domain=domain or _domain_from_addr(parsed.from_addr),
            subject=parsed.subject,
            date_header=parsed.date_header,
            amount_usd=amount,
            signal_kind=signal_kind,
            matched_registry_id=registry_id,
            snippet=snippet,
        )
    except Exception as exc:  # noqa: BLE001 — never raise on a single message
        _LOG.warning(
            "gmail_bill_scan signal-build failed gmail_id=%s: %s",
            raw_message.gmail_id, exc,
        )
        return None


# ---------------------------------------------------------------------------
# Step 4: compile snapshot (cross-reference against the registry — READ ONLY)
# ---------------------------------------------------------------------------

def _parse_date_for_sort(date_header: str) -> float:
    """Best-effort epoch seconds from an RFC822 Date header; 0.0 if unparseable."""
    if not date_header:
        return 0.0
    try:
        dt = email.utils.parsedate_to_datetime(date_header)
        if dt is None:
            return 0.0
        return dt.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def compile_bills_snapshot(
    signals: list[BillSignal],
    registry: CodbRegistry,
    *,
    lookback_days: int = 90,
    ts: str | None = None,
) -> BillsSnapshot:
    """Group signals by matched registry id and cross-reference the registry.

    NEVER writes to ``codb_registry.yaml`` — pure computation over the
    already-loaded, in-memory :class:`CodbRegistry`. The most RECENT amount
    per vendor becomes ``latest_observed_usd``; ``delta_usd`` compares it to
    the registry's ``estimated_monthly_usd`` (``None`` when either side is
    missing an amount). Payment-declined signals are flagged prominently
    (``at_risk_vendor_ids``); vendors with no registry entry are listed
    separately in ``unmatched_vendors``.
    """
    registry_by_id = {c.id: c for c in registry.costs}

    by_id: dict[str, list[BillSignal]] = {}
    for sig in signals:
        by_id.setdefault(sig.matched_registry_id, []).append(sig)

    rows: list[VendorBillRow] = []
    at_risk: list[str] = []
    unmatched_domains: set[str] = set()

    for registry_id, sigs in by_id.items():
        # Most-recent signal by parsed Date header (fallback: list order).
        sigs_sorted = sorted(sigs, key=lambda s: _parse_date_for_sort(s.date_header))
        latest = sigs_sorted[-1]
        # Latest AMOUNT specifically — prefer the most recent signal that
        # actually carried a parsed amount, not just the most recent email.
        amount_bearing = [s for s in sigs_sorted if s.amount_usd is not None]
        latest_amount_signal = amount_bearing[-1] if amount_bearing else None
        latest_observed = latest_amount_signal.amount_usd if latest_amount_signal else None

        declined = any(s.signal_kind == "payment_declined" for s in sigs)

        if registry_id == UNMATCHED_BUCKET:
            for s in sigs:
                if s.from_domain:
                    unmatched_domains.add(s.from_domain)
            vendor_label = "unmatched vendor(s) — see unmatched_vendors"
            registry_estimate = None
        else:
            item = registry_by_id.get(registry_id)
            vendor_label = item.name if item else registry_id
            registry_estimate = item.estimated_monthly_usd if item else None

        delta = None
        if latest_observed is not None and registry_estimate is not None:
            delta = round(latest_observed - registry_estimate, 2)

        flags: list[str] = []
        if declined:
            flags.append("PAYMENT_DECLINED")
            at_risk.append(registry_id)
        if registry_id == UNMATCHED_BUCKET:
            flags.append("NEW_VENDOR_NOT_IN_REGISTRY")
        elif registry_id not in registry_by_id:
            flags.append("NO_REGISTRY_ENTRY")
        if delta is not None and abs(delta) >= 1.0:
            flags.append(f"DELTA_{'+' if delta > 0 else ''}{delta:.2f}")

        rows.append(VendorBillRow(
            registry_id=registry_id,
            vendor_label=vendor_label,
            latest_observed_usd=latest_observed,
            last_seen_date=latest.date_header,
            registry_estimate_usd=registry_estimate,
            delta_usd=delta,
            signal_count=len(sigs),
            payment_declined=declined,
            flag=", ".join(flags) if flags else "ok",
        ))

    # Sort: at-risk first, then by |delta| desc, then alpha.
    rows.sort(key=lambda r: (
        not r.payment_declined,
        -(abs(r.delta_usd) if r.delta_usd is not None else 0.0),
        r.vendor_label,
    ))

    return BillsSnapshot(
        ts=ts or iso_now(),
        lookback_days=lookback_days,
        signals_scanned=len(signals),
        rows=rows,
        at_risk_vendor_ids=at_risk,
        unmatched_vendors=sorted(unmatched_domains),
    )


# ---------------------------------------------------------------------------
# Output-path convention (mirrors gmail_poller's ledger path pattern)
# ---------------------------------------------------------------------------

def default_snapshot_path() -> Path:
    """Default output path — sibling of the inbound-email ledger.

    ``gmail_inbox_ledger_path`` is e.g. ``.../data/intake/inbound_email.jsonl``;
    this scanner writes a JSON snapshot (not a ledger) alongside it under
    ``data/finance/`` per the finance workcell's on-disk convention.
    """
    settings = get_settings()
    ledger_path = Path(settings.gmail_inbox_ledger_path)
    # .../data/intake/inbound_email.jsonl -> .../data/finance/gmail_bills_snapshot.json
    data_root = ledger_path.parent.parent  # strip "intake"
    return data_root / "finance" / "gmail_bills_snapshot.json"


# ---------------------------------------------------------------------------
# Pipeline runner (used by the CLI; also test-friendly via api_factory)
# ---------------------------------------------------------------------------

def run_scan(
    *,
    lookback_days: int = 90,
    max_results: int = 200,
    api_factory=None,
) -> BillsSnapshot:
    """Full pipeline: search -> parse -> compile. Raises GmailApiError on
    connect failure (including a missing OAuth token) — the CLI translates
    that into the exit-2 / one-line message contract.
    """
    settings = get_settings()
    if api_factory is None:
        def api_factory() -> GmailApiClient:  # type: ignore[no-redef]
            return GmailApiClient(
                client_id=settings.gmail_oauth_client_id,
                client_secret=settings.gmail_oauth_client_secret,
                token_path=Path(settings.gmail_oauth_token_path),
            )

    signals: list[BillSignal] = []
    with api_factory() as client:
        raw_matches = search_billing_emails(
            client, lookback_days=lookback_days, max_results=max_results,
        )
        for raw in raw_matches:
            sig = parse_bill_signal(raw)
            if sig is not None:
                signals.append(sig)

    registry = load_registry()
    return compile_bills_snapshot(signals, registry, lookback_days=lookback_days)


# ---------------------------------------------------------------------------
# Human-readable summary table
# ---------------------------------------------------------------------------

def render_summary_table(snapshot: BillsSnapshot) -> str:
    """Vendor | latest_observed_usd | registry_estimate_usd | delta | last_seen_date | flag."""
    headers = ("vendor", "latest_$", "registry_est_$", "delta_$", "last_seen", "flag")
    rows_fmt: list[tuple[str, ...]] = []
    for r in snapshot.rows:
        rows_fmt.append((
            r.vendor_label[:40],
            f"{r.latest_observed_usd:.2f}" if r.latest_observed_usd is not None else "-",
            f"{r.registry_estimate_usd:.2f}" if r.registry_estimate_usd is not None else "-",
            f"{r.delta_usd:+.2f}" if r.delta_usd is not None else "-",
            r.last_seen_date[:25] if r.last_seen_date else "-",
            r.flag,
        ))
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in rows_fmt)) if rows_fmt else len(headers[i])
        for i in range(len(headers))
    ]
    lines = [
        " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)),
        "-+-".join("-" * w for w in widths),
    ]
    for row in rows_fmt:
        lines.append(" | ".join(c.ljust(widths[i]) for i, c in enumerate(row)))
    if not rows_fmt:
        lines.append("(no billing signals found)")
    if snapshot.unmatched_vendors:
        lines.append("")
        lines.append(
            "vendors seen in Gmail with no CODB registry entry - consider adding: "
            + ", ".join(snapshot.unmatched_vendors),
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

_TOKEN_MISSING_MESSAGE = (
    "gmail bill scan: Gmail OAuth token missing — run scripts/Authorize-Gmail.ps1 "
    "first to authorize (one-time interactive browser step)."
)


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m backend.finance.gmail_bill_scan [--lookback-days N] [--out PATH]``.

    Exit codes:
        0 - scan completed, snapshot written + printed
        2 - OAuth token file missing (clear message, no traceback)
        1 - any other connect/auth failure
    """
    import argparse
    import logging as _logging

    _logging.basicConfig(
        level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="Gmail bill/invoice scanner")
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--out", type=str, default="")
    args = parser.parse_args(argv)

    out_path = Path(args.out) if args.out else default_snapshot_path()

    try:
        snapshot = run_scan(lookback_days=args.lookback_days)
    except GmailApiError as exc:
        if "gmail_oauth_token_missing" in str(exc):
            print(_TOKEN_MISSING_MESSAGE)
            return 2
        print(f"gmail bill scan: FAILED connect_error={exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 — never crash the script
        print(f"gmail bill scan: FAILED unexpected_error={exc}")
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8",
    )

    print(render_summary_table(snapshot))
    print()
    print(f"snapshot written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
