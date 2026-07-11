"""Finance metering — Stripe meter-event push + report_call_minutes contract."""

from __future__ import annotations

import json

import pytest

from backend.finance.metering import report_call_minutes, seconds_to_billable_minutes
from backend.finance.stripe_client import StripeClient, StripeError


# ---------------------------------------------------------------------------
# seconds_to_billable_minutes — round UP (telecom convention)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, 0),
        (None, 0),
        (-5, 0),
        (1, 1),
        (59, 1),
        (60, 1),
        (61, 2),
        (119, 2),
        (120, 2),
        (181, 4),
    ],
)
def test_seconds_to_billable_minutes_rounds_up(seconds, expected):
    assert seconds_to_billable_minutes(seconds) == expected


# ---------------------------------------------------------------------------
# StripeClient.create_meter_event — validation + form encoding
# ---------------------------------------------------------------------------


def _client_capturing(captured: dict) -> StripeClient:
    client = StripeClient(api_key="sk_test_x")
    client._post = lambda path, data: (  # type: ignore[method-assign]
        captured.update(path=path, data=data) or {"identifier": "evt_1"}
    )
    return client


def test_create_meter_event_builds_form_payload():
    captured: dict = {}
    client = _client_capturing(captured)
    client.create_meter_event(
        event_name="receptionist_call_minutes",
        stripe_customer_id="cus_123",
        value=3,
        identifier="call_abc",
    )
    assert captured["path"] == "/billing/meter_events"
    assert captured["data"]["event_name"] == "receptionist_call_minutes"
    assert captured["data"]["payload[stripe_customer_id]"] == "cus_123"
    assert captured["data"]["payload[value]"] == "3"
    # call_id passes through as the idempotency identifier.
    assert captured["data"]["identifier"] == "call_abc"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"event_name": "", "stripe_customer_id": "cus_1", "value": 1},
        {"event_name": "m", "stripe_customer_id": "", "value": 1},
        {"event_name": "m", "stripe_customer_id": "cus_1", "value": 0},
        {"event_name": "m", "stripe_customer_id": "cus_1", "value": -2},
    ],
)
def test_create_meter_event_rejects_bad_input(kwargs):
    client = StripeClient(api_key="sk_test_x")
    with pytest.raises(ValueError):
        client.create_meter_event(**kwargs)


# ---------------------------------------------------------------------------
# report_call_minutes — never raises; logs every outcome
# ---------------------------------------------------------------------------


class _FakeStripe:
    """Records create_meter_event calls; optionally raises."""

    def __init__(self, *, raises: bool = False):
        self.raises = raises
        self.calls: list[dict] = []

    def create_meter_event(self, **kw):
        self.calls.append(kw)
        if self.raises:
            raise StripeError("stripe_http_402: card declined")
        return {"identifier": kw.get("identifier") or "evt_x"}


@pytest.fixture
def meter_log(tmp_path, monkeypatch):
    path = tmp_path / "meter_events.jsonl"
    monkeypatch.setenv("SAMUS_METER_EVENT_LOG", str(path))
    return path


def _log_rows(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln]


def test_report_call_minutes_success(meter_log):
    fake = _FakeStripe()
    result = report_call_minutes(
        stripe_customer_id="cus_1",
        call_id="call_1",
        duration_seconds=130,
        client=fake,
    )
    assert result.ok is True
    assert result.minutes_reported == 3  # ceil(130/60)
    assert result.meter_event_id == "call_1"  # Stripe echoes the identifier
    assert fake.calls[0]["identifier"] == "call_1"
    assert fake.calls[0]["event_name"] == "receptionist_call_minutes"
    rows = _log_rows(meter_log)
    assert rows and rows[-1]["ok"] is True and rows[-1]["minutes_reported"] == 3


def test_report_call_minutes_zero_duration_is_clean_noop(meter_log):
    fake = _FakeStripe()
    result = report_call_minutes(
        stripe_customer_id="cus_1",
        call_id="call_0",
        duration_seconds=0,
        client=fake,
    )
    assert result.ok is True and result.minutes_reported == 0
    assert fake.calls == []  # no Stripe call for a 0s call


def test_report_call_minutes_unknown_sku(meter_log):
    result = report_call_minutes(
        stripe_customer_id="cus_1",
        call_id="c",
        duration_seconds=60,
        sku_id="retainer_does_not_exist",
        client=_FakeStripe(),
    )
    assert result.ok is False and "unknown_sku" in result.error


def test_report_call_minutes_sku_without_meter(meter_log):
    # A flat retainer with no metered price -> can't report usage.
    result = report_call_minutes(
        stripe_customer_id="cus_1",
        call_id="c",
        duration_seconds=60,
        sku_id="retainer_seo_optimization",
        client=_FakeStripe(),
    )
    assert result.ok is False and "sku_has_no_meter" in result.error


def test_report_call_minutes_missing_customer(meter_log):
    result = report_call_minutes(
        stripe_customer_id="",
        call_id="c",
        duration_seconds=60,
        client=_FakeStripe(),
    )
    assert result.ok is False and result.error == "stripe_customer_id_unset"


def test_report_call_minutes_never_raises_on_stripe_error(meter_log):
    """A Stripe failure returns ok=False — it must not raise into the webhook."""
    result = report_call_minutes(
        stripe_customer_id="cus_1",
        call_id="call_err",
        duration_seconds=90,
        client=_FakeStripe(raises=True),
    )
    assert result.ok is False
    assert "stripe_meter_event_failed" in result.error
    rows = _log_rows(meter_log)
    assert rows and rows[-1]["ok"] is False


# ---------------------------------------------------------------------------
# Finance /meter-event route — wiring smoke test
# ---------------------------------------------------------------------------


def test_meter_event_route_is_wired(meter_log):
    """POST /meter-event parses the request + runs report_call_minutes.

    No Stripe key in the test env, so report_call_minutes returns
    ok=false/stripe_api_key_unset — which still proves the route, the
    MeterEventRequest model, and the capability check are all wired.
    """
    from fastapi.testclient import TestClient

    from backend.finance.app import app

    client = TestClient(app)
    resp = client.post(
        "/meter-event",
        json={
            "stripe_customer_id": "cus_1",
            "call_id": "call_route_1",
            "duration_seconds": 120.0,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["call_id"] == "call_route_1"
    assert body["ok"] is False
    assert body["error"] == "stripe_api_key_unset"


def test_meter_event_route_rejects_bad_body():
    from fastapi.testclient import TestClient

    from backend.finance.app import app

    client = TestClient(app)
    resp = client.post("/meter-event", json={"call_id": "x"})  # missing fields
    assert resp.status_code == 422
