"""End-to-end integration tests covering the doc §1 workcell pipelines.

All external boundaries are mocked: Google Places, Anthropic, SQS, DDB, HTTP.
The tests exercise the in-process orchestration through each workcell's
``service`` / ``logic`` module so the wiring (idempotency, audit, response
shape) is verified end-to-end without leaving the process.
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest


# --- shared fixtures --------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    """Per-test: fresh idempotency store, fresh storage root, fresh audit dirs."""
    # Re-import lazily so monkeypatch targets the live module.
    from backend.common import idempotency, storage

    # Reset the module-level singleton's internal dict.
    idempotency.GLOBAL_IDEMPOTENCY_STORE._data.clear()  # type: ignore[attr-defined]

    # Redirect artifact storage to a per-test tmp dir.
    # Set both the module attribute (direct patch) and the env var so all
    # storage access paths are covered regardless of which one a given module
    # reads at call time.
    artifact_root = tmp_path / "artifacts"
    monkeypatch.setattr(storage, "_ROOT", artifact_root)
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(artifact_root))

    # Redirect every workcell audit path to per-test tmp files.
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    for env_name in (
        "SAMUS_PROSPECTING_AUDIT_PATH",
        "SAMUS_LEADGEN_AUDIT_PATH",
        "SAMUS_SCAFFOLD_AUDIT_PATH",
        "SAMUS_FULFILLMENT_AUDIT_PATH",
        "SAMUS_FEEDBACK_AUDIT_PATH",
    ):
        monkeypatch.setenv(env_name, str(audit_dir / f"{env_name.lower()}.jsonl"))

    yield

    idempotency.GLOBAL_IDEMPOTENCY_STORE._data.clear()  # type: ignore[attr-defined]


# --- prospecting -----------------------------------------------------------


def test_prospecting_pipeline_produces_csv_with_mocked_places(monkeypatch):
    from backend.prospecting import csv_export, service
    from backend.prospecting.models import DiscoveryRequest, ProspectRecord

    fake_records = [
        ProspectRecord(
            prospect_id="p-1",
            company_name="Acme Plumbing",
            phone="+1-555-0100",
            website_url="https://acme.example",
            zipcode="90210",
            industry="construction",
            review_rating="4.6",
            review_count="83",
            business_hours="Mon-Fri 8-5",
            business_categories="plumber, contractor",
        ),
        ProspectRecord(
            prospect_id="p-2",
            company_name="Bayside Dental",
            phone="+1-555-0200",
            website_url="https://bayside.example",
            zipcode="90210",
            industry="healthcare",
            review_rating="4.9",
            review_count="142",
            business_hours="Mon-Sat 9-6",
            business_categories="dentist, oral_surgeon",
        ),
    ]

    def fake_discover_for_zipcode(*, zipcode, industries, max_results_per_zip, must_have_website):
        return list(fake_records)

    monkeypatch.setattr(service, "discover_for_zipcode", fake_discover_for_zipcode)

    req = DiscoveryRequest(
        campaign_name="e2e_smoke",
        zipcodes=["90210"],
        industries=["construction", "healthcare"],
        max_results_per_zip=10,
        must_have_website=True,
        # This smoke test predates the signal_filter admission gate and uses
        # sparse fixtures (no website_status / seo_score) the gate would
        # reject — disable it here; the gate has dedicated coverage in
        # test_prospecting_signal_gate.py.
        enable_signal_filter_gate=False,
    )

    result = service.process_discovery(req, task_id="e2e-prospecting-1")

    assert result.prospect_count == 2
    assert result.cache_hit is False
    csv_path = result.csv_path
    assert os.path.isfile(csv_path), f"csv not written at {csv_path}"

    with open(csv_path, encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = list(reader)
    assert tuple(header) == csv_export.CSV_COLUMNS
    assert len(rows) == 2
    opener_idx = csv_export.CSV_COLUMNS.index("callsheet_opener")
    for row in rows:
        assert row[opener_idx], "callsheet_opener should be populated for every row"


# --- leadgen ----------------------------------------------------------------


def test_leadgen_score_returns_lead_score():
    from backend.leadgen import service
    from backend.leadgen.models import LeadRequest

    req = LeadRequest(
        company="Northwind Logistics",
        domain="northwind.example",
        industry="logistics",
        employee_count=120,
        annual_revenue_usd=25_000_000,
        geo="US",
        signals=["high_intent", "rfp_open"],
    )

    score = service.process_lead(req, task_id="e2e-leadgen-1")

    assert score.company
    assert score.normalized_domain
    assert score.segment in ("micro", "smb", "midmarket", "enterprise")
    assert score.tier in ("low", "medium", "high", "priority")
    assert isinstance(score.recommendations, list)
    assert score.recommendations, "recommendations should not be empty"


# --- scaffold ---------------------------------------------------------------


def test_scaffold_generate_proposal_pack():
    from backend.scaffold import logic
    from backend.scaffold.models import ScaffoldRequest

    req = ScaffoldRequest(
        asset_type="proposal_pack",
        title="Q3 Local SEO Engagement",
        client="Bayside Dental",
        brand_voice="warm-professional",
        offer="48-Hour Rescue Audit",
        goals=["Lift local pack visibility", "Stabilize NAP citations"],
        inputs={"industry": "healthcare", "pain": "low local pack ranking"},
    )

    payload = logic.generate_scaffold(req)

    document = payload["document"]
    assert isinstance(document, str) and document.strip()
    assert "Proposal Pack" in document
    assert "Q3 Local SEO Engagement" in document
    assert "48-Hour Rescue Audit" in document
    assert payload["asset_type"] == "proposal_pack"
    assert payload["offer"]["headline"]


# --- fulfillment ------------------------------------------------------------


def test_fulfillment_plan_blocks_critical_without_approvals():
    from backend.fulfillment import logic

    result = logic.plan_fulfillment(
        task_id="e2e-fulfillment-critical",
        payload={
            "objective": "irreversible delete production database",
            "actions": [],
        },
        metadata={},
    )

    assert result["status"] == "blocked"
    assert result["risk_assessment"]["risk_level"] == "critical"
    assert result["block_reason"]
    assert "approval" in result["block_reason"].lower()


def test_fulfillment_plan_approved_normal_risk():
    from backend.fulfillment import logic

    result = logic.plan_fulfillment(
        task_id="e2e-fulfillment-normal",
        payload={
            "objective": "compile weekly summary",
            "actions": [],
        },
        metadata={},
    )

    assert result["status"] == "approved"
    assert result["risk_assessment"]["risk_level"] == "normal"
    assert result["approval_check"]["approved"] is True


# --- gateway dispatch -------------------------------------------------------


def test_gateway_dispatch_routes_to_sqs_when_queue_configured(monkeypatch):
    monkeypatch.setenv("SQS_LEADGEN_QUEUE_URL", "https://sqs.us-west-1/123/leadgen-q")
    from backend.common.settings import reload_settings
    from backend.gateway import sqs_dispatch

    reload_settings()
    sqs_dispatch.reload_queue_urls()

    fake_client = MagicMock()
    fake_client.send_message = MagicMock(return_value={"MessageId": "msg-e2e-1"})
    monkeypatch.setattr(sqs_dispatch, "sqs_client", lambda: fake_client)

    result = sqs_dispatch.enqueue_dispatch(
        "leadgen",
        task_id="t1",
        action="score_lead",
        payload={"company": "x", "domain": "x.test"},
        metadata={},
        trace_id="trace-e2e",
        idempotency_key="idem-e2e",
    )

    assert result["queued"] is True
    assert result["message_id"] == "msg-e2e-1"
    fake_client.send_message.assert_called_once()
    kwargs = fake_client.send_message.call_args.kwargs
    assert kwargs["QueueUrl"] == "https://sqs.us-west-1/123/leadgen-q"
    body = json.loads(kwargs["MessageBody"])
    assert body["task_id"] == "t1"
    assert body["action"] == "score_lead"


def test_gateway_dispatch_falls_back_to_http_when_no_queue(monkeypatch):
    # No SQS queue env vars — gateway HTTP fallback path.
    for env in (
        "SQS_LEADGEN_QUEUE_URL",
        "SQS_PROSPECTING_QUEUE_URL",
        "SQS_SCAFFOLD_QUEUE_URL",
        "SQS_FULFILLMENT_QUEUE_URL",
    ):
        monkeypatch.delenv(env, raising=False)

    from backend.common.settings import reload_settings
    from backend.gateway import service as gateway_service
    from backend.gateway import sqs_dispatch

    reload_settings()
    sqs_dispatch.reload_queue_urls()

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json = MagicMock(return_value={"accepted": True, "task_id": "t1"})
    fake_response.text = '{"accepted": true}'

    async_post = AsyncMock(return_value=fake_response)
    monkeypatch.setattr(gateway_service, "signed_post_json", async_post)

    envelope = {
        "task_id": "t1",
        "service": "leadgen",
        "action": "score_lead",
        "payload": {"company": "x"},
        "metadata": {},
    }
    status, body = asyncio.run(
        gateway_service.dispatch_to_target(
            "https://leadgen.example", "leadgen", envelope
        )
    )

    assert status == 200
    assert body == {"accepted": True, "task_id": "t1"}
    async_post.assert_awaited_once()


# --- feedback (Agent D's workcell) ------------------------------------------


def test_feedback_bounce_writes_suppression(monkeypatch):
    """Verify the feedback workcell's bounce path writes one suppression row per recipient."""
    try:
        from backend.feedback import handlers, service  # noqa: F401
        from backend.feedback.models import SnsNotification
    except ImportError:
        pytest.skip("feedback workcell not yet present")

    fake_suppression = MagicMock()
    fake_suppression.put_item = MagicMock(return_value={})
    fake_feedback_events = MagicMock()
    fake_feedback_events.put_item = MagicMock(return_value={})

    monkeypatch.setattr(handlers, "_suppression_table", lambda: fake_suppression)
    monkeypatch.setattr(
        handlers, "_feedback_events_table", lambda: fake_feedback_events
    )

    ses_payload = {
        "notificationType": "Bounce",
        "mail": {"messageId": "ses-1"},
        "bounce": {
            "bounceType": "Permanent",
            "bounceSubType": "General",
            "bouncedRecipients": [
                {"emailAddress": "a@example.com"},
                {"emailAddress": "b@example.com"},
            ],
            "timestamp": "2026-01-01T00:00:00Z",
        },
    }
    sns = SnsNotification(
        Type="Notification",
        MessageId="sns-1",
        TopicArn="arn:aws:sns:us-west-1:000000000000:ses-events",
        Message=json.dumps(ses_payload),
        Timestamp="2026-01-01T00:00:00Z",
    )

    result = service.process_sns_notification(sns, task_id="e2e-feedback-1")

    assert result.notification_type == "Bounce"
    assert result.recipient_count == 2
    assert sorted(result.suppressed) == ["a@example.com", "b@example.com"]
    assert fake_suppression.put_item.call_count == 2
    written = {
        call.kwargs["Item"]["email"] for call in fake_suppression.put_item.call_args_list
    }
    assert written == {"a@example.com", "b@example.com"}
