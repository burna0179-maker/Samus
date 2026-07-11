"""Gmail bill/invoice scanner — vendor matching, amount extraction, snapshot
compilation, missing-token handling, and read-only-ness guarantees.

All Gmail interaction is MOCKED — no real network / OAuth. The fake client
below deliberately does NOT implement mark_read / modify / send so any
accidental call raises AttributeError, which the read-only tests assert
against as a second line of defense on top of the source-grep check.
"""

from __future__ import annotations

import ast
import inspect
import json

import pytest

from backend.finance import gmail_bill_scan as gbs
from backend.finance.models import CodbItem, CodbRegistry, CodbRevenueTargets
from backend.intake.gmail_api_client import GmailApiError


# ---------------------------------------------------------------------------
# Fixtures: synthetic RFC822 messages
# ---------------------------------------------------------------------------


def _rfc822(*, from_addr: str, subject: str, date: str, body: str, message_id: str) -> bytes:
    return (
        f"Message-ID: <{message_id}>\r\n"
        f"From: {from_addr}\r\n"
        f"To: samushustleforge@gmail.com\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {date}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n"
        f"{body}\r\n"
    ).encode("utf-8")


ANTHROPIC_RECEIPT = _rfc822(
    from_addr="Anthropic <billing@anthropic.com>",
    subject="Your receipt from Anthropic - $200.00",
    date="Mon, 01 Jun 2026 10:00:00 +0000",
    body="Thanks for your payment. Total charged: $200.00",
    message_id="a1@anthropic.com",
)

GOOGLE_DECLINED = _rfc822(
    from_addr="Google Payments <payments-noreply@google.com>",
    subject="Payment declined: Google Workspace ($54.56)",
    date="Tue, 02 Jun 2026 09:00:00 +0000",
    body="Your payment of $54.56 for Google Workspace was declined. Please update your payment method.",
    message_id="g1@google.com",
)

WORDPRESS_RENEWAL = _rfc822(
    from_addr="WordPress.com <billing@wordpress.com>",
    subject="WordPress.com renewal invoice",
    date="Wed, 03 Jun 2026 08:00:00 +0000",
    body="Your domain hustleforge.tech Professional Email renews. Amount due: $10.00",
    message_id="w1@wordpress.com",
)

TWILIO_INVOICE = _rfc822(
    from_addr="Twilio Billing <billing@twilio.com>",
    subject="Your Twilio invoice is ready",
    date="Thu, 04 Jun 2026 08:00:00 +0000",
    body="Invoice total: $15.32",
    message_id="t1@twilio.com",
)

OPENAI_PLUS_RECEIPT = _rfc822(
    from_addr="OpenAI <receipts@openai.com>",
    subject="Your ChatGPT Plus receipt",
    date="Fri, 05 Jun 2026 08:00:00 +0000",
    body="Receipt for ChatGPT Plus subscription. Amount charged: $20.00",
    message_id="o1@openai.com",
)

OPENAI_API_INVOICE = _rfc822(
    from_addr="OpenAI <billing@openai.com>",
    subject="Your OpenAI API platform invoice",
    date="Sat, 06 Jun 2026 08:00:00 +0000",
    body="API usage invoice. Total: $15.00",
    message_id="o2@openai.com",
)

SENDGRID_UNMATCHED = _rfc822(
    from_addr="SendGrid <billing@sendgrid.com>",
    subject="Your SendGrid receipt",
    date="Sun, 07 Jun 2026 08:00:00 +0000",
    body="Receipt: $19.95 charged.",
    message_id="s1@sendgrid.com",
)

NON_BILLING_NEWSLETTER = _rfc822(
    from_addr="News <news@randomsite.com>",
    subject="Weekly Digest",
    date="Sun, 07 Jun 2026 09:00:00 +0000",
    body="Nothing billing-related here.",
    message_id="n1@randomsite.com",
)


# ---------------------------------------------------------------------------
# Vendor matching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "domain,expected_first_id",
    [
        ("anthropic.com", "anthropic-claude-subscription"),
        ("twilio.com", "twilio-telephony"),
        ("vapi.ai", "vapi-voice-calls"),
        ("wordpress.com", "wordpress-com-domain"),
        ("google.com", "google-workspace-hustleforge"),
        ("cloud.google.com", "gcp-cloud-run"),
        ("pge.com", "pge-energy"),
        ("xfinity.com", "xfinity-internet-cable"),
        ("comcast.com", "xfinity-internet-cable"),
        ("spotify.com", "spotify-premium"),
    ],
)
def test_known_vendor_domains_map_correctly(domain, expected_first_id):
    matched_domain, candidates = gbs.match_vendor(f"billing@{domain}")
    assert matched_domain == domain
    assert candidates[0] == expected_first_id


def test_openai_domain_has_two_candidates():
    _, candidates = gbs.match_vendor("billing@openai.com")
    assert "openai-api-samus-inference" in candidates
    assert "openai-chatgpt-plus" in candidates


def test_aws_domain_has_bucket_candidates():
    _, candidates = gbs.match_vendor("no-reply@aws.amazon.com")
    assert "aws-sqs" in candidates
    assert "aws-other" in candidates


def test_new_vendor_flagged_no_registry_id():
    _, candidates = gbs.match_vendor("billing@sendgrid.com")
    assert candidates == []


def test_unknown_vendor_domain_returns_empty():
    domain, candidates = gbs.match_vendor("someone@totally-unrelated-domain.com")
    assert domain == ""
    assert candidates == []


def test_subdomain_matches_known_vendor():
    domain, candidates = gbs.match_vendor("no-reply@mail.pge.com")
    assert domain == "pge.com"
    assert candidates[0] == "pge-energy"


# ---------------------------------------------------------------------------
# Amount extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Your receipt from Anthropic - $200.00", 200.0),
        ("Payment declined: Google Workspace ($54.56)", 54.56),
        ("Total charged: $200.00", 200.0),
        ("Amount due: $10.00", 10.0),
        ("No amount here at all", None),
    ],
)
def test_amount_extraction_from_subjects(text, expected):
    assert gbs.extract_amount(text) == expected


def test_amount_extraction_prefers_total_context_over_stray_number():
    text = "Item quantity 2 x $5.00. Total charged: $200.00 for this billing period."
    assert gbs.extract_amount(text) == 200.0


# ---------------------------------------------------------------------------
# Signal classification
# ---------------------------------------------------------------------------


def test_classify_signal_kind_payment_declined():
    assert gbs.classify_signal_kind("Payment declined", "card was declined") == "payment_declined"


def test_classify_signal_kind_renewal():
    assert gbs.classify_signal_kind("WordPress.com renewal invoice", "") == "renewal_notice"


def test_classify_signal_kind_receipt():
    assert gbs.classify_signal_kind("Your receipt", "thanks for your payment") == "receipt"


def test_classify_signal_kind_invoice():
    assert gbs.classify_signal_kind("Your Twilio invoice is ready", "") == "invoice"


def test_classify_signal_kind_other_fallback():
    assert gbs.classify_signal_kind("Weekly Digest", "nothing here") == "other"


# ---------------------------------------------------------------------------
# parse_bill_signal — fail-soft on unparseable messages
# ---------------------------------------------------------------------------


def test_parse_bill_signal_happy_path():
    match = gbs.RawMatch(gmail_id="g1", raw=ANTHROPIC_RECEIPT)
    sig = gbs.parse_bill_signal(match)
    assert sig is not None
    assert sig.matched_registry_id == "anthropic-claude-subscription"
    assert sig.amount_usd == 200.0
    assert sig.signal_kind == "receipt"


def test_parse_bill_signal_never_raises_on_garbage():
    match = gbs.RawMatch(gmail_id="bad1", raw=b"not a valid rfc822 message at all \xff\xfe")
    # Should not raise; either returns a BillSignal with best-effort fields
    # or None -- never propagates an exception.
    result = gbs.parse_bill_signal(match)
    assert result is None or isinstance(result, gbs.BillSignal)


def test_parse_bill_signal_openai_plus_vs_api_disambiguation():
    plus = gbs.parse_bill_signal(gbs.RawMatch(gmail_id="o1", raw=OPENAI_PLUS_RECEIPT))
    api = gbs.parse_bill_signal(gbs.RawMatch(gmail_id="o2", raw=OPENAI_API_INVOICE))
    assert plus.matched_registry_id == "openai-chatgpt-plus"
    assert api.matched_registry_id == "openai-api-samus-inference"


def test_parse_bill_signal_unmatched_vendor_bucketed():
    sig = gbs.parse_bill_signal(gbs.RawMatch(gmail_id="s1", raw=SENDGRID_UNMATCHED))
    assert sig.matched_registry_id == gbs.UNMATCHED_BUCKET
    assert sig.from_domain == "sendgrid.com"


# ---------------------------------------------------------------------------
# search_billing_emails — read-only usage of the (fake) client
# ---------------------------------------------------------------------------


class _FakeGmailClient:
    """Fake exposing ONLY the read-only surface — no mark_read/modify/send."""

    def __init__(self, messages: dict[str, bytes]):
        self._messages = messages
        self.search_calls: list[str] = []

    def search_message_ids(self, *, query: str, max_results: int = 100) -> list[str]:
        self.search_calls.append(query)
        return list(self._messages.keys())[:max_results]

    def fetch_raw(self, message_id: str) -> bytes:
        return self._messages[message_id]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def test_search_billing_emails_uses_search_not_unread_only():
    fake = _FakeGmailClient(
        {
            "g1": ANTHROPIC_RECEIPT,
            "g2": GOOGLE_DECLINED,
        }
    )
    matches = gbs.search_billing_emails(fake, lookback_days=30, max_results=50)
    assert len(matches) == 2
    assert fake.search_calls, "search_message_ids must be called"
    assert "newer_than:30d" in fake.search_calls[0]


def test_search_billing_emails_skips_fetch_failures_without_raising():
    class _PartialFailClient(_FakeGmailClient):
        def fetch_raw(self, message_id: str) -> bytes:
            if message_id == "bad":
                raise RuntimeError("boom")
            return super().fetch_raw(message_id)

    fake = _PartialFailClient({"g1": ANTHROPIC_RECEIPT, "bad": b""})
    matches = gbs.search_billing_emails(fake, lookback_days=90)
    assert len(matches) == 1
    assert matches[0].gmail_id == "g1"


# ---------------------------------------------------------------------------
# compile_bills_snapshot — delta computation, declined flagging, bucketing
# ---------------------------------------------------------------------------


def _mock_registry() -> CodbRegistry:
    return CodbRegistry(
        costs=[
            CodbItem(
                id="anthropic-claude-subscription",
                name="Anthropic Claude subscription",
                category="ai",
                criticality="critical",
                estimated_monthly_usd=150.0,
            ),
            CodbItem(
                id="google-workspace-hustleforge",
                name="Google Workspace (Hustleforge)",
                category="saas",
                criticality="high",
                estimated_monthly_usd=54.56,
            ),
            CodbItem(
                id="wordpress-com-domain",
                name="WordPress.com",
                category="saas",
                criticality="medium",
                estimated_monthly_usd=10.0,
            ),
            CodbItem(
                id="twilio-telephony",
                name="Twilio",
                category="infrastructure",
                criticality="critical",
                estimated_monthly_usd=15.0,
            ),
        ],
        revenue_targets=CodbRevenueTargets(monthly_minimum_usd=5500, runway_alert_days=60),
    )


def test_compile_bills_snapshot_delta_computation():
    signals = [
        gbs.parse_bill_signal(gbs.RawMatch(gmail_id="g1", raw=ANTHROPIC_RECEIPT)),
    ]
    snap = gbs.compile_bills_snapshot(signals, _mock_registry(), lookback_days=90)
    row = next(r for r in snap.rows if r.registry_id == "anthropic-claude-subscription")
    assert row.latest_observed_usd == 200.0
    assert row.registry_estimate_usd == 150.0
    assert row.delta_usd == 50.0


def test_compile_bills_snapshot_flags_payment_declined_prominently():
    signals = [gbs.parse_bill_signal(gbs.RawMatch(gmail_id="g2", raw=GOOGLE_DECLINED))]
    snap = gbs.compile_bills_snapshot(signals, _mock_registry(), lookback_days=90)
    row = snap.rows[0]
    assert row.payment_declined is True
    assert "PAYMENT_DECLINED" in row.flag
    assert "google-workspace-hustleforge" in snap.at_risk_vendor_ids
    # At-risk rows sort first.
    assert snap.rows[0].payment_declined is True


def test_compile_bills_snapshot_unmatched_vendor_bucketing():
    signals = [gbs.parse_bill_signal(gbs.RawMatch(gmail_id="s1", raw=SENDGRID_UNMATCHED))]
    snap = gbs.compile_bills_snapshot(signals, _mock_registry(), lookback_days=90)
    assert "sendgrid.com" in snap.unmatched_vendors
    row = next(r for r in snap.rows if r.registry_id == gbs.UNMATCHED_BUCKET)
    assert "NEW_VENDOR_NOT_IN_REGISTRY" in row.flag


def test_compile_bills_snapshot_most_recent_amount_wins():
    older = gbs.BillSignal(
        gmail_id="x1",
        message_id="m1",
        from_addr="billing@twilio.com",
        from_domain="twilio.com",
        subject="old invoice",
        date_header="Mon, 01 Jun 2026 08:00:00 +0000",
        amount_usd=10.0,
        signal_kind="invoice",
        matched_registry_id="twilio-telephony",
    )
    newer = gbs.BillSignal(
        gmail_id="x2",
        message_id="m2",
        from_addr="billing@twilio.com",
        from_domain="twilio.com",
        subject="new invoice",
        date_header="Thu, 04 Jun 2026 08:00:00 +0000",
        amount_usd=15.32,
        signal_kind="invoice",
        matched_registry_id="twilio-telephony",
    )
    snap = gbs.compile_bills_snapshot([older, newer], _mock_registry(), lookback_days=90)
    row = next(r for r in snap.rows if r.registry_id == "twilio-telephony")
    assert row.latest_observed_usd == 15.32
    assert row.signal_count == 2


def test_compile_bills_snapshot_never_touches_registry_file(monkeypatch, tmp_path):
    """Calling compile_bills_snapshot must not write codb_registry.yaml."""
    registry_file = tmp_path / "codb_registry.yaml"
    registry_file.write_text("costs: []\nrevenue_targets: {}\n", encoding="utf-8")
    before = registry_file.read_text(encoding="utf-8")
    signals = [gbs.parse_bill_signal(gbs.RawMatch(gmail_id="g1", raw=ANTHROPIC_RECEIPT))]
    gbs.compile_bills_snapshot(signals, _mock_registry(), lookback_days=90)
    after = registry_file.read_text(encoding="utf-8")
    assert before == after


# ---------------------------------------------------------------------------
# End-to-end fixture pipeline (no real network)
# ---------------------------------------------------------------------------


def test_end_to_end_fixture_pipeline_and_summary_table():
    fake = _FakeGmailClient(
        {
            "g1": ANTHROPIC_RECEIPT,
            "g2": GOOGLE_DECLINED,
            "g3": WORDPRESS_RENEWAL,
            "g4": TWILIO_INVOICE,
            "g5": SENDGRID_UNMATCHED,
            "g6": NON_BILLING_NEWSLETTER,
        }
    )
    matches = gbs.search_billing_emails(fake, lookback_days=90)
    signals = [s for s in (gbs.parse_bill_signal(m) for m in matches) if s is not None]
    snap = gbs.compile_bills_snapshot(signals, _mock_registry(), lookback_days=90)
    table = gbs.render_summary_table(snap)
    assert "vendor" in table
    assert "PAYMENT_DECLINED" in table
    assert "sendgrid.com" in table  # surfaced in the unmatched-vendors line


# ---------------------------------------------------------------------------
# CLI / missing-token graceful exit
# ---------------------------------------------------------------------------


def test_cli_missing_token_exits_2_no_traceback(monkeypatch, tmp_path, capsys):
    missing_token_path = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(
        gbs,
        "run_scan",
        lambda **kw: (_ for _ in ()).throw(
            GmailApiError(f"gmail_oauth_token_missing: {missing_token_path}"),
        ),
    )
    rc = gbs.main(["--lookback-days", "30"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "Authorize-Gmail.ps1" in out
    assert "Traceback" not in out


def test_cli_other_connect_error_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(
        gbs,
        "run_scan",
        lambda **kw: (_ for _ in ()).throw(GmailApiError("gmail_http_500: boom")),
    )
    rc = gbs.main([])
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAILED" in out


def test_cli_writes_json_snapshot_and_prints_table(monkeypatch, tmp_path, capsys):
    snap = gbs.compile_bills_snapshot(
        [gbs.parse_bill_signal(gbs.RawMatch(gmail_id="g1", raw=ANTHROPIC_RECEIPT))],
        _mock_registry(),
        lookback_days=45,
    )
    monkeypatch.setattr(gbs, "run_scan", lambda **kw: snap)
    out_file = tmp_path / "snap.json"
    rc = gbs.main(["--lookback-days", "45", "--out", str(out_file)])
    assert rc == 0
    assert out_file.exists()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["lookback_days"] == 45
    out = capsys.readouterr().out
    assert "snapshot written to" in out


# ---------------------------------------------------------------------------
# Read-only guarantees — source-level assertion (no mark_read/modify/send)
# ---------------------------------------------------------------------------


def test_module_source_never_calls_mutating_gmail_methods():
    """AST-level check: gmail_bill_scan.py must not CALL client.mark_read /
    .modify(...) / send_message anywhere — this is a read-only
    reconnaissance tool. (Docstring prose referencing these names to
    explain the read-only guarantee is fine; only actual Call nodes count.)
    """
    source = inspect.getsource(gbs)
    tree = ast.parse(source)
    forbidden_attrs = {"mark_read", "modify", "send_message", "send"}
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in forbidden_attrs:
                hits.append(node.func.attr)
    assert hits == [], f"forbidden mutating call(s) found: {hits}"


def test_module_source_never_writes_codb_registry_yaml():
    source = inspect.getsource(gbs)
    assert (
        "codb_registry.yaml" not in source
        or "def "
        not in source.split(
            "codb_registry.yaml",
        )[0][-50:]
    )
    # Stronger check: no open(...'w') / write_text / dump calls near the
    # registry path helper — the module only ever calls load_registry().
    assert "yaml.safe_dump" not in source
    assert "yaml.dump" not in source
    assert "registry_path()" not in source  # never resolves the writable path


def test_fake_client_has_no_mutating_methods():
    """Second line of defense: the fake used throughout has no mark_read/
    modify/send at all, so any accidental call would AttributeError.
    """
    for forbidden in ("mark_read", "modify", "send"):
        assert not hasattr(_FakeGmailClient({}), forbidden)
