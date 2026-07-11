"""Tests for the customer-fulfillment orchestrator.

All 3 downstream services (memory.CustomerStore, seo.audit_and_report,
outreach.ses_adapter.send_email_via_ses) are injected as fakes so tests
run without Neo4j, Anthropic, or SES roundtrips.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeCustomer:
    def __init__(self, id_: str, email: str, name: str = "",
                 current_state: str = "prospect"):
        self.id = id_
        self.email = email
        self.name = name
        self.company = ""
        self.source = "fulfill_cli"
        self.created_at = time.time()
        self.current_state = current_state
        self.current_state_since = time.time()
        self.metadata: dict = {}


class _FakeEvent:
    def __init__(self, from_state: str | None, to_state: str):
        self.event_id = "evt_test"
        self.customer_id = "cust_x"
        self.from_state = from_state
        self.to_state = to_state
        self.date = time.time()
        self.reason = ""
        self.metadata: dict = {}


class _FakeCustomerStore:
    """In-memory CustomerStore stub.

    Tracks every call so tests can assert sequence + state transitions
    without standing up Neo4j.
    """
    def __init__(self):
        self.customers: dict[str, _FakeCustomer] = {}  # by email
        self.calls: list[tuple] = []
        self.fail_on: set[str] = set()  # method names that should raise

    def get_by_email(self, email: str):
        self.calls.append(("get_by_email", email))
        if "get_by_email" in self.fail_on:
            raise RuntimeError("simulated graph failure")
        return self.customers.get(email.lower())

    def create_customer(self, *, email: str, name: str = "",
                        company: str = "", source: str = "manual",
                        metadata: dict | None = None):
        self.calls.append(("create_customer", email))
        if "create_customer" in self.fail_on:
            raise RuntimeError("simulated graph failure")
        cust = _FakeCustomer(
            id_=f"cust_{email.replace('@', '_at_').replace('.', '_')}",
            email=email, name=name, current_state="prospect",
        )
        self.customers[email.lower()] = cust
        return cust

    def advance_state(self, *, customer_id: str, to_state: str,
                      reason: str = "", metadata: dict | None = None):
        self.calls.append(("advance_state", customer_id, to_state, reason))
        if "advance_state" in self.fail_on:
            raise RuntimeError("simulated graph failure")
        # find the customer by id (linear scan is fine for tests)
        for cust in self.customers.values():
            if cust.id == customer_id:
                from_state = cust.current_state
                cust.current_state = to_state
                return _FakeEvent(from_state=from_state, to_state=to_state)
        raise ValueError(f"unknown customer_id: {customer_id}")


def _fake_audit_and_report(report_path: Path, *, raise_exc: Exception | None = None):
    """Build a fake audit_and_report callable that writes a tiny report."""
    def _fn(req, *, target_keywords=None, tone="professional",
            customer_label=None):
        if raise_exc:
            raise raise_exc
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            f"# SEO Audit for {req.url}\n\nScore: 87/100\n",
            encoding="utf-8",
        )
        return {
            "audit": {"url": req.url, "seo_score": 87},
            "optimize": {"url": req.url, "recommendations": []},
            "content": {"url": req.url, "page_drafts": {}, "word_count": 100,
                        "used_llm": False},
            "report_path": str(report_path),
            "customer_slug": report_path.parent.name,
        }
    return _fn


def _fake_send_email(captured: list, *, raise_exc: Exception | None = None,
                     message_id: str = "ses_fake_123"):
    """Build a fake send_email_via_ses that records args."""
    def _fn(*, to, subject, body, from_addr=None, aws_region=None):
        if raise_exc:
            raise raise_exc
        captured.append({"to": to, "subject": subject, "body": body})
        return {"message_id": message_id, "channel": "email", "to": to,
                "ts": "2026-05-15T00:00:00Z"}
    return _fn


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_full_pipeline_happy_path(tmp_path):
    from backend.fulfill import fulfill_customer
    store = _FakeCustomerStore()
    captured: list = []
    report = tmp_path / "customers" / "cust_x" / "seo_report.md"

    result = fulfill_customer(
        email="alice@example.com",
        audit_url="https://acme-plumbing.example.com",
        name="Alice",
        customer_store=store,
        audit_and_report_fn=_fake_audit_and_report(report),
        send_email_fn=_fake_send_email(captured),
    )

    assert result.ok is True
    assert result.email == "alice@example.com"
    assert result.customer_id.startswith("cust_alice")
    assert result.prior_state == "prospect"
    assert result.final_state == "delivered"
    assert result.report_path == str(report)
    assert result.email_message_id == "ses_fake_123"

    # 5 steps, all ok
    step_names = [s.name for s in result.steps]
    assert step_names == [
        "find_or_create_customer",
        "advance_to_in_delivery",
        "audit_and_report",
        "send_email",
        "advance_to_delivered",
    ]
    assert all(s.status == "ok" for s in result.steps)

    # State advanced through both transitions
    advances = [c for c in store.calls if c[0] == "advance_state"]
    assert [c[2] for c in advances] == ["in_delivery", "delivered"]

    # Email captured with the report body inline
    assert len(captured) == 1
    assert captured[0]["to"] == "alice@example.com"
    assert "SEO Audit for https://acme-plumbing.example.com" in captured[0]["body"]
    assert "Hi Alice," in captured[0]["body"]


def test_existing_customer_in_paid_state_advances_to_in_delivery(tmp_path):
    from backend.fulfill import fulfill_customer
    store = _FakeCustomerStore()
    # Pre-populate as a paid customer
    cust = _FakeCustomer(id_="cust_existing", email="bob@example.com",
                         current_state="paid")
    store.customers["bob@example.com"] = cust
    captured: list = []
    report = tmp_path / "customers" / "cust_existing" / "seo_report.md"

    result = fulfill_customer(
        email="bob@example.com",
        audit_url="https://bob.example.com",
        customer_store=store,
        audit_and_report_fn=_fake_audit_and_report(report),
        send_email_fn=_fake_send_email(captured),
    )

    assert result.ok is True
    assert result.prior_state == "paid"
    assert result.final_state == "delivered"
    # advance_state called twice: paid -> in_delivery, in_delivery -> delivered
    advances = [c for c in store.calls if c[0] == "advance_state"]
    assert [c[2] for c in advances] == ["in_delivery", "delivered"]
    # No create_customer call (existing)
    assert not any(c[0] == "create_customer" for c in store.calls)


def test_already_in_delivery_skips_intermediate_advance(tmp_path):
    from backend.fulfill import fulfill_customer
    store = _FakeCustomerStore()
    cust = _FakeCustomer(id_="cust_repeat", email="c@example.com",
                         current_state="in_delivery")
    store.customers["c@example.com"] = cust
    captured: list = []
    report = tmp_path / "customers" / "cust_repeat" / "seo_report.md"

    result = fulfill_customer(
        email="c@example.com", audit_url="https://c.example.com",
        customer_store=store,
        audit_and_report_fn=_fake_audit_and_report(report),
        send_email_fn=_fake_send_email(captured),
    )

    assert result.ok is True
    step_statuses = {s.name: s.status for s in result.steps}
    assert step_statuses["advance_to_in_delivery"] == "skipped"
    assert step_statuses["advance_to_delivered"] == "ok"
    # Only one advance call: in_delivery -> delivered
    advances = [c for c in store.calls if c[0] == "advance_state"]
    assert [c[2] for c in advances] == ["delivered"]


def test_already_delivered_skips_intermediate_advance(tmp_path):
    from backend.fulfill import fulfill_customer
    store = _FakeCustomerStore()
    cust = _FakeCustomer(id_="cust_done", email="d@example.com",
                         current_state="delivered")
    store.customers["d@example.com"] = cust
    captured: list = []
    report = tmp_path / "customers" / "cust_done" / "seo_report.md"

    result = fulfill_customer(
        email="d@example.com", audit_url="https://d.example.com",
        customer_store=store,
        audit_and_report_fn=_fake_audit_and_report(report),
        send_email_fn=_fake_send_email(captured),
    )

    # delivered customers don't re-advance to in_delivery; they re-fulfill
    # (the operator might be re-running for a fresh audit) but the
    # advance_to_delivered at the end is a no-op transition.
    step_statuses = {s.name: s.status for s in result.steps}
    assert step_statuses["advance_to_in_delivery"] == "skipped"
    assert result.ok is True


def test_no_send_skips_email_step(tmp_path):
    from backend.fulfill import fulfill_customer
    store = _FakeCustomerStore()
    captured: list = []
    report = tmp_path / "customers" / "x" / "seo_report.md"

    result = fulfill_customer(
        email="e@example.com", audit_url="https://e.example.com",
        send_email=False,
        customer_store=store,
        audit_and_report_fn=_fake_audit_and_report(report),
        send_email_fn=_fake_send_email(captured),
    )

    assert result.ok is True
    assert result.email_message_id is None
    step_statuses = {s.name: s.status for s in result.steps}
    assert step_statuses["send_email"] == "skipped"
    assert step_statuses["advance_to_delivered"] == "ok"
    # send_email_fn must not have been called
    assert captured == []


# ---------------------------------------------------------------------------
# Failure paths — each step's failure should NOT roll the state forward
# ---------------------------------------------------------------------------

def test_customer_lookup_failure_aborts(tmp_path):
    from backend.fulfill import fulfill_customer
    store = _FakeCustomerStore()
    store.fail_on.add("get_by_email")
    report = tmp_path / "customers" / "x" / "seo_report.md"

    result = fulfill_customer(
        email="f@example.com", audit_url="https://f.example.com",
        customer_store=store,
        audit_and_report_fn=_fake_audit_and_report(report),
        send_email_fn=_fake_send_email([]),
    )
    assert result.ok is False
    # Only one step recorded — the failed lookup
    assert len(result.steps) == 1
    assert result.steps[0].name == "find_or_create_customer"
    assert result.steps[0].status == "failed"
    # No advance_state calls, no report written
    assert not any(c[0] == "advance_state" for c in store.calls)


def test_audit_failure_aborts_before_email_or_delivered_advance(tmp_path):
    from backend.fulfill import fulfill_customer
    store = _FakeCustomerStore()
    captured: list = []

    result = fulfill_customer(
        email="g@example.com", audit_url="https://g.example.com",
        customer_store=store,
        audit_and_report_fn=_fake_audit_and_report(
            tmp_path / "unused.md",
            raise_exc=RuntimeError("audit crashed: stripe transport down"),
        ),
        send_email_fn=_fake_send_email(captured),
    )
    assert result.ok is False
    step_statuses = {s.name: s.status for s in result.steps}
    assert step_statuses["advance_to_in_delivery"] == "ok"
    assert step_statuses["audit_and_report"] == "failed"
    # send_email NOT attempted
    assert captured == []
    # State stayed at in_delivery — operator can fix audit + re-run
    advances = [c for c in store.calls if c[0] == "advance_state"]
    assert [c[2] for c in advances] == ["in_delivery"]
    assert result.final_state == "in_delivery"


def test_email_failure_leaves_state_at_in_delivery(tmp_path):
    from backend.fulfill import fulfill_customer
    store = _FakeCustomerStore()
    report = tmp_path / "customers" / "h" / "seo_report.md"

    result = fulfill_customer(
        email="h@example.com", audit_url="https://h.example.com",
        customer_store=store,
        audit_and_report_fn=_fake_audit_and_report(report),
        send_email_fn=_fake_send_email(
            [], raise_exc=RuntimeError("ses 503: throttled"),
        ),
    )
    assert result.ok is False
    step_statuses = {s.name: s.status for s in result.steps}
    assert step_statuses["audit_and_report"] == "ok"
    assert step_statuses["send_email"] == "failed"
    # No advance_to_delivered attempted
    assert "advance_to_delivered" not in step_statuses
    # State is in_delivery, report on disk for retry
    assert result.final_state == "in_delivery"
    assert Path(result.report_path).exists()


def test_advance_to_delivered_failure_records_partial_success(tmp_path):
    from backend.fulfill import fulfill_customer
    store = _FakeCustomerStore()
    captured: list = []
    report = tmp_path / "customers" / "i" / "seo_report.md"

    # Email succeeds, then delivered-advance fails
    cust = _FakeCustomer(id_="cust_i", email="i@example.com",
                         current_state="paid")
    store.customers["i@example.com"] = cust

    def _flaky_advance(*, customer_id, to_state, reason="", metadata=None):
        store.calls.append(("advance_state", customer_id, to_state, reason))
        if to_state == "delivered":
            raise RuntimeError("neo4j connection died mid-run")
        # in_delivery path works
        for c in store.customers.values():
            if c.id == customer_id:
                from_state = c.current_state
                c.current_state = to_state
                return _FakeEvent(from_state=from_state, to_state=to_state)
        raise ValueError("unknown id")

    store.advance_state = _flaky_advance

    result = fulfill_customer(
        email="i@example.com", audit_url="https://i.example.com",
        customer_store=store,
        audit_and_report_fn=_fake_audit_and_report(report),
        send_email_fn=_fake_send_email(captured),
    )
    assert result.ok is False
    step_statuses = {s.name: s.status for s in result.steps}
    assert step_statuses["send_email"] == "ok"
    assert step_statuses["advance_to_delivered"] == "failed"
    # Email already sent — that's the partial-success condition the
    # final_state hint captures for the operator. message_id recorded.
    assert result.email_message_id == "ses_fake_123"


# ---------------------------------------------------------------------------
# CLI rendering
# ---------------------------------------------------------------------------

def test_render_result_includes_all_fields(tmp_path):
    from backend.fulfill import _render_result, FulfillStep, FulfillmentResult
    res = FulfillmentResult(
        email="r@example.com", audit_url="https://r.example.com",
        customer_id="cust_r", prior_state="paid", final_state="delivered",
        report_path=str(tmp_path / "r.md"),
        email_message_id="ses_test_abc",
        ok=True, ts="2026-05-15T00:00:00Z",
        steps=[
            FulfillStep(name="find_or_create_customer", status="ok",
                        detail="found", elapsed_ms=12),
            FulfillStep(name="advance_to_in_delivery", status="ok",
                        detail="paid -> in_delivery", elapsed_ms=8),
            FulfillStep(name="audit_and_report", status="ok",
                        detail="score=87", elapsed_ms=1320),
            FulfillStep(name="send_email", status="ok",
                        detail="msg_id=ses_test_abc", elapsed_ms=415),
            FulfillStep(name="advance_to_delivered", status="ok",
                        detail="in_delivery -> delivered", elapsed_ms=7),
        ],
    )
    out = _render_result(res)
    assert "SAMUS FULFILL  [OK]" in out
    assert "r@example.com" in out
    assert "paid -> delivered" in out
    assert "ses_test_abc" in out
    assert "find_or_create_customer" in out
    assert "audit_and_report" in out


def test_render_result_failed_badge():
    from backend.fulfill import _render_result, FulfillStep, FulfillmentResult
    res = FulfillmentResult(
        email="x@example.com", audit_url="https://x.example.com",
        ok=False, ts="t",
        steps=[FulfillStep(name="find_or_create_customer", status="failed",
                           detail="graph down")],
    )
    out = _render_result(res)
    assert "SAMUS FULFILL  [FAILED]" in out
    assert "graph down" in out


# ---------------------------------------------------------------------------
# main() argument parsing
# ---------------------------------------------------------------------------

def test_main_requires_email_and_url(capsys):
    from backend.fulfill import main
    with pytest.raises(SystemExit):
        main([])  # argparse exits with usage error
    captured = capsys.readouterr()
    assert "required" in (captured.err or captured.out).lower()


def test_main_returns_exit_1_on_failure(tmp_path, monkeypatch, capsys):
    from backend.fulfill import main
    import backend.fulfill as mod

    def _failing(**kwargs):
        from backend.fulfill import FulfillmentResult
        return FulfillmentResult(
            email=kwargs["email"], audit_url=kwargs["audit_url"],
            ok=False, ts="t",
        )

    monkeypatch.setattr(mod, "fulfill_customer", _failing)
    code = main(["--email", "x@x.com", "--url", "https://x.com"])
    assert code == 1


def test_main_returns_exit_0_on_success(tmp_path, monkeypatch, capsys):
    from backend.fulfill import main
    import backend.fulfill as mod

    def _ok(**kwargs):
        from backend.fulfill import FulfillmentResult
        return FulfillmentResult(
            email=kwargs["email"], audit_url=kwargs["audit_url"],
            ok=True, ts="t",
        )

    monkeypatch.setattr(mod, "fulfill_customer", _ok)
    code = main(["--email", "x@x.com", "--url", "https://x.com", "--no-send"])
    assert code == 0
    out = capsys.readouterr().out
    assert "SAMUS FULFILL  [OK]" in out
