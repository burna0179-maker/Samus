"""Tests for the retainer monthly-cycle DAG runner.

All downstream services (SEO audit, email sender, operator-task CRM
dispatch, fix-log writer) are injected as fakes so tests don't touch
Neo4j, SES/SendGrid, or the network.

Coverage targets:

  - SEO Optimization 4-step DAG completes end-to-end (happy path)
  - DAG step order is respected (audit -> diff -> fixes -> report)
  - Plan persists to disk at the expected artifact path
  - Visibility report renders + lands on disk
  - Email send fires with the rendered markdown
  - First-cycle path correctly flags "no prior month"
  - Registry lookup rejects unknown SKUs
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_artifact_root(monkeypatch, tmp_path):
    """Redirect SAMUS_ARTIFACT_ROOT to a per-test tmp dir.

    The retainer module writes plan.json + snapshot.json + reports to
    storage.root(); tests need a fresh root so customer slugs don't
    bleed between tests.
    """
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    yield


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _make_fake_audit_fn(
    seo_score: int = 78,
    issues: list[dict[str, Any]] | None = None,
    rank_by_keyword: dict[str, int] | None = None,
):
    """Build a stub run_audit_fn that returns a controllable snapshot."""
    issues = issues if issues is not None else [
        {"id": "missing_meta_desc", "severity": "high", "category": "content",
         "message": "Missing meta description on homepage"},
        {"id": "broken_link_1", "severity": "medium", "category": "technical",
         "message": "Broken internal link /old-page"},
        {"id": "no_schema", "severity": "high", "category": "technical",
         "message": "No LocalBusiness schema markup"},
    ]
    rank_by_keyword = rank_by_keyword or {"plumber sacramento": 11}

    def _fn(audit_url: str) -> dict[str, Any]:
        return {
            "seo_score": seo_score,
            "issues": issues,
            "rank_by_keyword": rank_by_keyword,
            "gsc_clicks": 250,
            "gsc_impressions": 5000,
        }
    return _fn


def _make_fake_send_email(captured: list[dict[str, Any]]):
    """Build a stub send_email_fn that records calls."""
    def _fn(*, to: str, subject: str, body: str, **kwargs):
        captured.append({
            "to": to, "subject": subject, "body": body, "kwargs": kwargs,
        })
        return {"message_id": "fake_msg_42", "channel": "email", "to": to}
    return _fn


def _make_fake_fix_log_fn():
    """Build a stub fix_log_fn that mirrors the queue as 'applied'."""
    def _fn(customer_id: str, sku_id: str, cycle_month: str,
            fix_queue: list[dict[str, Any]]) -> list[dict[str, str]]:
        return [
            {
                "area": (item.get("category") or "general"),
                "description": item.get("message", "fix applied"),
            }
            for item in fix_queue
        ]
    return _fn


def _make_fake_operator_task_fn(created: list[dict[str, Any]]):
    """Build a stub operator_task_fn that records each task payload."""
    counter = {"n": 0}

    def _fn(payload: dict[str, Any]) -> dict[str, str]:
        counter["n"] += 1
        tid = f"task_{counter['n']}"
        created.append({"operator_task_id": tid, "payload": payload})
        return {"operator_task_id": tid}
    return _fn


# ---------------------------------------------------------------------------
# SKU registry
# ---------------------------------------------------------------------------

def test_registry_contains_four_retainer_skus():
    from backend.retainer.registry import RETAINER_SKUS, list_retainer_sku_ids

    ids = list_retainer_sku_ids()
    assert set(ids) == {
        "retainer_seo_optimization",
        "retainer_ai_ops_partner_entry",
        "retainer_ai_ops_partner_premium",
        "retainer_ai_receptionist",
    }
    assert len(RETAINER_SKUS) == 4


def test_get_retainer_sku_unknown_returns_none():
    from backend.retainer.registry import get_retainer_sku
    assert get_retainer_sku("retainer_does_not_exist") is None


def test_get_retainer_sku_known_returns_config():
    from backend.retainer.registry import get_retainer_sku

    seo = get_retainer_sku("retainer_seo_optimization")
    assert seo is not None
    assert seo.cadence == "monthly"
    assert seo.cycle_id == "seo_optimization_cycle"
    assert seo.price_usd_cents_low == 30000
    assert seo.price_usd_cents_low == seo.price_usd_cents_high
    assert seo.stripe_product_id is not None
    assert seo.stripe_price_id is not None

    entry = get_retainer_sku("retainer_ai_ops_partner_entry")
    assert entry is not None
    assert entry.cycle_id == "ai_ops_partner_cycle"
    assert entry.price_usd_cents_low == 200000   # $2,000/mo entry tier
    assert entry.stripe_product_id == "prod_U913mXXZXG9REI"

    premium = get_retainer_sku("retainer_ai_ops_partner_premium")
    assert premium is not None
    assert premium.cycle_id == "ai_ops_partner_cycle"
    assert premium.price_usd_cents_low == 500000   # $5,000/mo premium tier
    assert premium.stripe_product_id == "prod_UWt4i2QB7BZlFx"


# ---------------------------------------------------------------------------
# Monthly cycle — SEO Optimization (4-step DAG)
# ---------------------------------------------------------------------------

def test_seo_optimization_cycle_completes_4_step_dag(tmp_path):
    """Happy path: every step runs in order + DAG completes successfully."""
    from backend.retainer.monthly_cycle import run_monthly_cycle

    captured_emails: list[dict[str, Any]] = []

    result = run_monthly_cycle(
        customer_id="acme_plumbing_example_com",
        sku_id="retainer_seo_optimization",
        audit_url="https://acme-plumbing.example.com",
        customer_email="jordan@acme-plumbing.example.com",
        customer_name="Jordan",
        run_audit_fn=_make_fake_audit_fn(),
        send_email_fn=_make_fake_send_email(captured_emails),
        fix_log_fn=_make_fake_fix_log_fn(),
        operator_task_fn=lambda payload: {"operator_task_id": "unused"},
    )

    # Overall DAG completed
    assert result.ok is True, f"steps: {[(s.id, s.status, s.detail) for s in result.steps]}"
    assert result.sku_id == "retainer_seo_optimization"

    # All 4 expected step ids ran, in dependency order, all done
    step_ids_done = [s.id for s in result.steps if s.status == "done"]
    assert step_ids_done == [
        "audit_current_state",
        "diff_against_prior_month",
        "apply_priority_fixes",
        "render_and_send",
    ]
    # No failures or skips
    assert all(s.status == "done" for s in result.steps), \
        f"unexpected non-done step: {[(s.id, s.status) for s in result.steps]}"


def test_seo_cycle_persists_plan_json_to_artifact_dir():
    """plan.json lands at customers/<slug>/<sku>/<YYYY-MM>/plan.json."""
    from backend.retainer.monthly_cycle import run_monthly_cycle

    result = run_monthly_cycle(
        customer_id="acme_test",
        sku_id="retainer_seo_optimization",
        audit_url="https://acme.test",
        customer_email="x@acme.test",
        run_audit_fn=_make_fake_audit_fn(),
        send_email_fn=_make_fake_send_email([]),
        fix_log_fn=_make_fake_fix_log_fn(),
        operator_task_fn=lambda payload: {"operator_task_id": "unused"},
    )

    plan_path = Path(result.plan_path)
    assert plan_path.exists()
    assert plan_path.name == "plan.json"
    parts = plan_path.parts
    assert "customers" in parts
    assert "acme_test" in parts
    assert "retainer_seo_optimization" in parts

    raw = json.loads(plan_path.read_text(encoding="utf-8"))
    assert raw["status"] == "complete"
    assert len(raw["steps"]) == 4


def test_seo_cycle_writes_visibility_report_to_disk():
    """The render_and_send step writes visibility_report.md."""
    from backend.retainer.monthly_cycle import run_monthly_cycle

    captured: list[dict[str, Any]] = []
    result = run_monthly_cycle(
        customer_id="cust_report_test",
        sku_id="retainer_seo_optimization",
        audit_url="https://example.test",
        customer_email="r@example.test",
        customer_name="River",
        run_audit_fn=_make_fake_audit_fn(),
        send_email_fn=_make_fake_send_email(captured),
        fix_log_fn=_make_fake_fix_log_fn(),
        operator_task_fn=lambda payload: {"operator_task_id": "unused"},
    )

    assert result.ok is True
    assert result.report_path is not None
    report_path = Path(result.report_path)
    assert report_path.exists()
    assert report_path.name == "visibility_report.md"

    body = report_path.read_text(encoding="utf-8")
    assert "# SEO Visibility Report" in body
    assert "Hi River," in body
    assert "Headline metrics" in body
    assert "Rank movement" in body
    assert "Fixes applied this month" in body
    assert "Morgan, HustleForge" in body


def test_seo_cycle_emails_the_report():
    """render_and_send step calls send_email_fn with the markdown body."""
    from backend.retainer.monthly_cycle import run_monthly_cycle

    captured: list[dict[str, Any]] = []
    result = run_monthly_cycle(
        customer_id="cust_email_test",
        sku_id="retainer_seo_optimization",
        audit_url="https://email.test",
        customer_email="customer@email.test",
        customer_name="Avery",
        run_audit_fn=_make_fake_audit_fn(),
        send_email_fn=_make_fake_send_email(captured),
        fix_log_fn=_make_fake_fix_log_fn(),
        operator_task_fn=lambda payload: {"operator_task_id": "unused"},
    )

    assert result.ok is True
    assert len(captured) == 1
    assert captured[0]["to"] == "customer@email.test"
    assert "SEO Optimization report" in captured[0]["subject"]
    assert "# SEO Visibility Report" in captured[0]["body"]
    assert result.email_message_id == "fake_msg_42"


def test_seo_cycle_first_month_flags_no_prior():
    """When no prior snapshot exists, diff step reports is_first_cycle=True."""
    from backend.retainer.monthly_cycle import run_monthly_cycle

    result = run_monthly_cycle(
        customer_id="brand_new_cust",
        sku_id="retainer_seo_optimization",
        audit_url="https://brand-new.test",
        customer_email="new@brand-new.test",
        run_audit_fn=_make_fake_audit_fn(),
        send_email_fn=_make_fake_send_email([]),
        fix_log_fn=_make_fake_fix_log_fn(),
        operator_task_fn=lambda payload: {"operator_task_id": "unused"},
    )

    assert result.ok is True
    plan = json.loads(Path(result.plan_path).read_text(encoding="utf-8"))
    diff_step = next(s for s in plan["steps"] if s["id"] == "diff_against_prior_month")
    assert diff_step["output"]["is_first_cycle"] is True
    assert diff_step["status"] == "done"


def test_seo_cycle_snapshot_persisted_for_next_month():
    """audit_current_state writes snapshot.json so next month's diff has prior."""
    from backend.retainer.monthly_cycle import run_monthly_cycle
    from backend.common import storage

    result = run_monthly_cycle(
        customer_id="snap_test_cust",
        sku_id="retainer_seo_optimization",
        audit_url="https://snap.test",
        customer_email="s@snap.test",
        run_audit_fn=_make_fake_audit_fn(seo_score=85),
        send_email_fn=_make_fake_send_email([]),
        fix_log_fn=_make_fake_fix_log_fn(),
        operator_task_fn=lambda payload: {"operator_task_id": "unused"},
    )

    assert result.ok is True
    cycle_dir = (
        storage.root() / "customers" / "snap_test_cust"
        / "retainer_seo_optimization" / result.cycle_month
    )
    snap = cycle_dir / "snapshot.json"
    assert snap.exists()
    data = json.loads(snap.read_text(encoding="utf-8"))
    assert data["seo_score"] == 85
    assert data["audit_url"] == "https://snap.test"


# ---------------------------------------------------------------------------
# Monthly cycle — AI Ops Partner (4-phase DAG)
# ---------------------------------------------------------------------------

def test_ai_ops_partner_cycle_creates_4_operator_tasks():
    """Week 1-4 each dispatch an OperatorTask via the injected fn."""
    from backend.retainer.monthly_cycle import run_monthly_cycle

    created_tasks: list[dict[str, Any]] = []
    result = run_monthly_cycle(
        customer_id="riverbend_auto",
        sku_id="retainer_ai_ops_partner_premium",
        customer_email="marcus@riverbend.test",
        customer_name="Marcus",
        operations_summary="trade-in triage backlog + service no-show rate",
        run_audit_fn=_make_fake_audit_fn(),
        send_email_fn=_make_fake_send_email([]),
        fix_log_fn=_make_fake_fix_log_fn(),
        operator_task_fn=_make_fake_operator_task_fn(created_tasks),
    )

    assert result.ok is True
    # Week 1 (assess), Week 2 (prioritize), Week 3 (build) each create
    # one task. Week 4 (monthly_report) does not.
    assert len(created_tasks) == 3
    titles = [t["payload"]["title"] for t in created_tasks]
    assert any("Week 1" in t for t in titles)
    assert any("Week 2" in t for t in titles)
    assert any("Week 3" in t for t in titles)


# ---------------------------------------------------------------------------
# Bad input — unknown SKU
# ---------------------------------------------------------------------------

def test_run_monthly_cycle_rejects_unknown_sku():
    from backend.retainer.monthly_cycle import run_monthly_cycle

    with pytest.raises(ValueError, match="unknown retainer SKU"):
        run_monthly_cycle(
            customer_id="x",
            sku_id="retainer_does_not_exist",
            audit_url="https://nope.test",
        )


# ---------------------------------------------------------------------------
# Resume / idempotency
# ---------------------------------------------------------------------------

def test_seo_cycle_rerun_reloads_existing_plan():
    """A second run within the same calendar month reuses the existing plan.json."""
    from backend.retainer.monthly_cycle import run_monthly_cycle

    captured_a: list[dict[str, Any]] = []
    result_a = run_monthly_cycle(
        customer_id="rerun_test",
        sku_id="retainer_seo_optimization",
        audit_url="https://rerun.test",
        customer_email="r@rerun.test",
        run_audit_fn=_make_fake_audit_fn(),
        send_email_fn=_make_fake_send_email(captured_a),
        fix_log_fn=_make_fake_fix_log_fn(),
        operator_task_fn=lambda payload: {"operator_task_id": "unused"},
    )
    assert result_a.ok is True
    assert len(captured_a) == 1

    # All steps are already 'done' in plan.json — second run should NOT
    # re-execute anything (no new email).
    captured_b: list[dict[str, Any]] = []
    result_b = run_monthly_cycle(
        customer_id="rerun_test",
        sku_id="retainer_seo_optimization",
        audit_url="https://rerun.test",
        customer_email="r@rerun.test",
        run_audit_fn=_make_fake_audit_fn(),
        send_email_fn=_make_fake_send_email(captured_b),
        fix_log_fn=_make_fake_fix_log_fn(),
        operator_task_fn=lambda payload: {"operator_task_id": "unused"},
    )
    assert result_b.ok is True
    # Existing plan loaded; no steps re-executed
    assert captured_b == []
    assert result_b.plan_id == result_a.plan_id


# ---------------------------------------------------------------------------
# Enroll smoke (uses fake customer store)
# ---------------------------------------------------------------------------

class _FakeCustomer:
    def __init__(self, id_, email, name="", state="prospect"):
        self.id = id_
        self.email = email
        self.name = name
        self.company = ""
        self.current_state = state
        self.metadata: dict = {}


class _FakeStore:
    def __init__(self):
        self.by_email: dict[str, _FakeCustomer] = {}
        self.advances: list[tuple[str, str]] = []

    def get_by_email(self, email):
        return self.by_email.get(email.lower())

    def create_customer(self, *, email, name="", company="", source="manual",
                        metadata=None):
        cust = _FakeCustomer(
            id_=email.replace("@", "_at_").replace(".", "_"),
            email=email, name=name, state="prospect",
        )
        self.by_email[email.lower()] = cust
        return cust

    def advance_state(self, *, customer_id, to_state, reason="", metadata=None):
        self.advances.append((customer_id, to_state))
        for c in self.by_email.values():
            if c.id == customer_id:
                c.current_state = to_state
                return type("Evt", (), {"to_state": to_state})()
        raise ValueError("unknown")


def test_enroll_retainer_happy_path():
    from backend.retainer.enroll import enroll_retainer

    store = _FakeStore()
    sent: list[dict[str, Any]] = []
    crm_dispatched: list[dict[str, Any]] = []

    def _fake_email(*, to, subject, body, html_body=None):
        sent.append({"to": to, "subject": subject, "body": body,
                     "html_body": html_body})
        return {"message_id": "welcome_msg_1", "channel": "email", "to": to}

    def _fake_crm(payload):
        crm_dispatched.append(payload)

    result = enroll_retainer(
        sku_id="retainer_seo_optimization",
        email="newcust@retainer.test",
        name="Quinn",
        customer_store=store,
        crm_dispatch_fn=_fake_crm,
        send_email_fn=_fake_email,
    )

    assert result.ok is True
    assert result.sku_id == "retainer_seo_optimization"
    assert result.customer_id is not None
    assert result.next_cycle_at is not None
    assert result.welcome_message_id == "welcome_msg_1"
    # advanced to active_retainer (state-machine extension landed 2026-05-16)
    assert any(adv[1] == "active_retainer" for adv in store.advances)
    # CRM dispatched with our SKU id
    assert len(crm_dispatched) == 1
    assert "retainer_seo_optimization" in crm_dispatched[0]["service_interest"]
    # Welcome email content
    assert "SEO Optimization" in sent[0]["subject"]


def test_enroll_retainer_unknown_sku_raises():
    from backend.retainer.enroll import enroll_retainer, UnknownRetainerSkuError

    with pytest.raises(UnknownRetainerSkuError):
        enroll_retainer(
            sku_id="retainer_nope",
            email="x@x.test",
            customer_store=_FakeStore(),
            crm_dispatch_fn=lambda p: None,
            send_email_fn=lambda **kw: {"message_id": "x"},
        )
