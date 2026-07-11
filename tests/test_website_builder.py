"""Website-build capability — transport, state, gates, and the supervised/
autonomous orchestrator (dormancy + fail-closed parking).

State is isolated to tmp via SAMUS_STATE_ROOT (the cash_engine test pattern).
The Codex registry is loaded session-wide by conftest, so the real stages walk
through a clean Codex; benign test copy passes the G2 scan.
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from backend.website import gate as gate_mod
from backend.website import service as svc
from backend.website import stages as stages_mod
from backend.website.models import WebsiteBrief, WebsiteOrder, WebsitePage
from backend.website.service import (
    WebsiteBuilderDisabled,
    advance,
    approve_stage,
    run,
    start_order,
)
from backend.website.state import STAGE_SEQUENCE, load_state, save_state
from backend.website.state import WebsiteBuildState
from backend.website.stages import (
    StageContext,
    _business_info_stage,
    _content_row,
    _content_stage,
    _parse_us_address,
    _publish_stage,
    _settle_stage,
)
from backend.website.wix_client import WixClient, WixError


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))


def _settings(**over):
    base = dict(
        website_builder_enabled=True,
        website_autonomous_enabled=False,
        website_live_publish_enabled=False,
        # Legacy posture for these transport/walk tests: content generation off,
        # so the `generate` stage is a pass-through and the operator-authored
        # brief copy stands. The content-gen path has its own test module.
        website_content_generation_enabled=False,
        anthropic_api_key="",
        wix_api_key="",
        wix_account_id="",
        website_content_collection_id="",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _order(pages=True, **over):
    page_list = (
        [WebsitePage(slug="home", title="Welcome",
                     content={"intro": "Fresh home cooked catering for your events."})]
        if pages else []
    )
    brief = WebsiteBrief(
        business_name="Mackabee Catering",
        business_description="Home cooked catering for local events.",
        contact_email="hello@example.test",
        pages=page_list,
    )
    kwargs = dict(customer_name="Sample Customer", brief=brief)
    kwargs.update(over)
    return WebsiteOrder(**kwargs)


def _json_body(request: httpx.Request) -> dict:
    import json as _j
    return _j.loads(request.content.decode() or "{}")


# ---------------------------------------------------------------------------
# WixClient transport
# ---------------------------------------------------------------------------

def test_wix_client_requires_api_key():
    with pytest.raises(ValueError):
        WixClient("")


def test_wix_client_auth_header_is_raw_no_bearer():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["site"] = request.headers.get("wix-site-id")
        return httpx.Response(200, json={"dataItem": {"id": "i1"}})

    client = WixClient("k-123", transport=httpx.MockTransport(handler))
    client.insert_data_item(site_id="s-1", collection_id="c-1", data={"title": "x"})
    assert seen["auth"] == "k-123"          # raw, NOT "Bearer k-123"
    assert seen["site"] == "s-1"


def test_wix_create_site_sends_account_id_and_returns_site_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["acct"] = request.headers.get("wix-account-id")
        seen["path"] = request.url.path
        return httpx.Response(200, json={"metaSiteId": "meta-9"})

    client = WixClient("k", account_id="acct-1", transport=httpx.MockTransport(handler))
    resp = client.create_site("Mackabee Catering", template_id="tpl-7")
    assert seen["acct"] == "acct-1"
    assert seen["path"] == "/funnel/projects/v1/create"
    assert resp["metaSiteId"] == "meta-9"


def test_wix_create_site_without_account_id_fails_closed():
    client = WixClient("k", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={})))
    with pytest.raises(WixError):
        client.create_site("X")


def test_wix_error_maps_http_status():
    client = WixClient("k", transport=httpx.MockTransport(
        lambda r: httpx.Response(403, json={"message": "denied"})))
    with pytest.raises(WixError) as ei:
        client.insert_data_item(site_id="s", collection_id="c", data={})
    assert ei.value.status_code == 403
    assert "denied" in str(ei.value)


def test_wix_business_profile_body_has_wrapper_and_field_mask():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, _json_body(request)))
        return httpx.Response(200, json={})

    client = WixClient("k", account_id="a", transport=httpx.MockTransport(handler))
    client.update_business_profile(site_id="s", business_profile={"description": "X"})
    client.update_business_contact(
        site_id="s", business_contact={"phone": "5", "address": {"city": "C", "zip": "9"}})
    client.publish_site(site_id="s")

    assert [p for p, _ in seen][:2] == [
        "/site-properties/v4/properties/business-profile",
        "/site-properties/v4/properties/business-contact",
    ]
    # profile: wrapped under businessProfile + comma-string field mask.
    _, prof = seen[0]
    assert "businessProfile" in prof and prof["fields"] == "description"
    # contact: nested objects (address) are masked at the TOP-LEVEL key, not
    # by dotted sub-paths (the live API rejects address.city etc.).
    _, cont = seen[1]
    assert "businessContact" in cont
    assert cont["fields"] == "phone,address"


def test_field_mask_top_level_keys():
    assert WixClient._field_mask({"a": 1, "b": {"c": 2, "d": 3}}) == "a,b"


def test_parse_us_address():
    a = _parse_us_address("<street>, <city>, <state> 97624")
    assert a == {
        "country": "US", "streetNumber": "3076", "street": "East Lake Blvd",
        "city": "<city>", "state": "OR", "zip": "97624",
    }
    # Fallback: unparseable -> street + country only.
    b = _parse_us_address("just a street")
    assert b == {"country": "US", "street": "just a street"}


def test_wix_429_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr("backend.website.wix_client._RETRY_BACKOFF_SEC", 0.0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"message": "slow down"})
        return httpx.Response(200, json={"dataItem": {"id": "ok"}})

    client = WixClient("k", transport=httpx.MockTransport(handler))
    resp = client.insert_data_item(site_id="s", collection_id="c", data={})
    assert calls["n"] == 2
    assert resp["dataItem"]["id"] == "ok"


# ---------------------------------------------------------------------------
# state + gates
# ---------------------------------------------------------------------------

def test_state_roundtrip():
    st = WebsiteBuildState(order_id="wb-1", customer_name="Harmony", order=_order())
    assert save_state(st)
    loaded = load_state("wb-1")
    assert loaded is not None
    assert loaded.customer_name == "Harmony"
    assert loaded.order.brief.business_name == "Mackabee Catering"


def test_stage_sequence_is_ordered_and_terminal():
    assert STAGE_SEQUENCE[0] == "brief"
    assert STAGE_SEQUENCE[-1] == "settle"
    assert "publish" in STAGE_SEQUENCE and "deliver" in STAGE_SEQUENCE
    # The codegen stage sits right after brief, before any Wix/outward step.
    assert STAGE_SEQUENCE[1] == "generate"
    # The AI-media stage runs after content (so the CMS row exists to merge refs).
    assert "media" in STAGE_SEQUENCE
    assert STAGE_SEQUENCE.index("media") > STAGE_SEQUENCE.index("content")


def test_codex_gate_fail_closed_when_registry_unavailable(monkeypatch):
    from backend.common.codex.exceptions import CodexUnavailable

    def boom(*a, **k):
        raise CodexUnavailable("registry down")

    monkeypatch.setattr(gate_mod, "check_action", boom)
    verdict = gate_mod.codex_gate(capability="x", payload={})
    assert verdict.allowed is False
    assert verdict.violated_rule_id == "CODEX_UNAVAILABLE"


def test_approval_gate_autonomous_implies_approval():
    st = WebsiteBuildState(order_id="wb-1")
    assert gate_mod.approval_ok(st, "brief", autonomous=True) is True
    assert gate_mod.approval_ok(st, "brief", autonomous=False) is False
    st.approved_stages.append("brief")
    assert gate_mod.approval_ok(st, "brief", autonomous=False) is True


def test_outward_gate():
    assert gate_mod.outward_ok("brief", live_publish_enabled=False) is True
    assert gate_mod.outward_ok("publish", live_publish_enabled=False) is False
    assert gate_mod.outward_ok("publish", live_publish_enabled=True) is True


# ---------------------------------------------------------------------------
# orchestrator: dormancy + supervised approval gate
# ---------------------------------------------------------------------------

def test_start_order_disabled_raises():
    with pytest.raises(WebsiteBuilderDisabled):
        start_order(_order(), settings=_settings(website_builder_enabled=False))


def test_start_order_mints_id_and_is_idempotent():
    s = _settings()
    st = start_order(_order(), settings=s)
    assert st.order_id.startswith("wb-")
    again = start_order(_order(order_id=st.order_id), settings=s)
    assert again.order_id == st.order_id


def test_supervised_advance_without_approval_pauses():
    s = _settings()
    st = start_order(_order(), settings=s)
    out = advance(st.order_id, settings=s)
    assert out.status == "awaiting_approval"
    assert out.stage == "brief"
    assert "brief" not in out.completed_stages


def test_supervised_approve_then_advance_runs_one_stage():
    s = _settings()
    st = start_order(_order(), settings=s)
    approve_stage(st.order_id, "brief")
    out = advance(st.order_id, settings=s)
    assert "brief" in out.completed_stages
    # Next stage now needs its own approval (generate sits after brief).
    nxt = advance(st.order_id, settings=s)
    assert nxt.status == "awaiting_approval"
    assert nxt.stage == "generate"


def test_brief_parks_when_no_pages():
    s = _settings()
    st = start_order(_order(pages=False), settings=s)
    approve_stage(st.order_id, "brief")
    out = advance(st.order_id, settings=s)
    assert out.status == "parked"
    assert out.park["reason"] == "no_pages_in_brief"


def test_provision_parks_without_credentials():
    s = _settings()  # no wix_api_key; generation off -> generate is a pass-through
    st = start_order(_order(), settings=s)
    approve_stage(st.order_id, "brief")
    advance(st.order_id, settings=s)
    approve_stage(st.order_id, "generate")
    advance(st.order_id, settings=s)
    approve_stage(st.order_id, "provision")
    out = advance(st.order_id, settings=s)
    assert out.status == "parked"
    assert out.park["reason"] == "wix_credentials_unset"


def test_autonomous_run_walks_until_park():
    s = _settings(website_autonomous_enabled=True)  # approval implied
    st = start_order(_order(), settings=s)
    out = run(st.order_id, settings=s)
    # brief + generate (pass-through, gen disabled) complete; provision parks.
    assert out.completed_stages == ["brief", "generate"]
    assert out.status == "parked"
    assert out.park["reason"] == "wix_credentials_unset"


def test_provision_adopts_existing_site_without_api():
    s = _settings(website_autonomous_enabled=True)
    order = _order(brief=WebsiteBrief(
        business_name="Mackabee Catering",
        business_description="Catering.",
        existing_site_id="adopted-site-1",
        pages=[WebsitePage(slug="home", title="Welcome",
                           content={"intro": "Local catering done right."})],
    ))
    st = start_order(order, settings=s)
    out = run(st.order_id, settings=s)
    # brief + generate (pass-through) + provision (adopted, no API) complete;
    # business_info is the first stage that needs the Wix API, so it parks.
    assert out.completed_stages == ["brief", "generate", "provision"]
    assert out.site_id == "adopted-site-1"
    assert out.status == "parked"
    assert out.stage == "business_info"
    assert out.park["reason"] == "wix_credentials_unset"


# ---------------------------------------------------------------------------
# outward + settle units
# ---------------------------------------------------------------------------

def test_publish_parks_when_live_disabled():
    st = WebsiteBuildState(order_id="wb-1", site_id="s-1", order=_order())
    ctx = StageContext(state=st, order=st.order, settings=_settings(),
                       live_publish_enabled=False)
    res = _publish_stage(ctx)
    assert res.parked and res.park_reason == "live_publish_disabled"


def test_settle_barter_emits_marker_and_operator_task():
    order = _order(settlement_kind="barter", settlement_lender_id="sample-customer",
                   settlement_amount_usd=740.0)
    st = WebsiteBuildState(order_id="wb-1", site_id="s-9", order=order)

    class FakeCRM:
        def __init__(self):
            self.tasks = []

        def create_operator_task(self, req):
            self.tasks.append(req)
            return SimpleNamespace(operator_task_id="t-1")

    crm = FakeCRM()
    ctx = StageContext(state=st, order=order, settings=_settings(), crm=crm)
    res = _settle_stage(ctx)
    assert res.ok
    assert res.detail["settlement_ref"].startswith("barter_repayment:sample-customer:$740.00")
    assert len(crm.tasks) == 1
    assert "sample-customer" in crm.tasks[0].title


def test_business_info_sets_profile_and_contact():
    order = _order()
    order.brief.contact_phone = "<phone>"
    order.brief.address = "<street>, <city>, <state> 97624"
    st = WebsiteBuildState(order_id="wb-1", site_id="s-1", order=order)
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(request.url.path)
        return httpx.Response(200, json={})

    wix = WixClient("k", transport=httpx.MockTransport(handler))
    ctx = StageContext(state=st, order=order, settings=_settings(), wix=wix)
    res = _business_info_stage(ctx)
    assert res.ok
    assert res.detail["business_info_applied"] == ["profile", "contact"]
    assert posted == [
        "/site-properties/v4/properties/business-profile",
        "/site-properties/v4/properties/business-contact",
    ]


def test_business_info_parks_on_wix_error_with_partial_progress():
    order = _order()
    st = WebsiteBuildState(order_id="wb-1", site_id="s-1", order=order)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # profile ok, contact 403.
        return httpx.Response(200 if calls["n"] == 1 else 403, json={"message": "no"})

    wix = WixClient("k", transport=httpx.MockTransport(handler))
    ctx = StageContext(state=st, order=order, settings=_settings(), wix=wix)
    res = _business_info_stage(ctx)
    assert res.parked
    assert res.park_reason == "wix_business_info_failed:403"
    assert res.detail["business_info_applied"] == ["profile"]


def test_content_row_flattens_pages_to_template_fields():
    order = WebsiteOrder(
        customer_name="Harmony",
        brief=WebsiteBrief(
            business_name="Sample Cleaning",
            business_description="Family-owned cleaning.",
            pages=[
                WebsitePage(slug="home", title="Home",
                            content={"headline": "Mighty clean", "intro": "We clean."}),
                WebsitePage(slug="services", title="Services",
                            content={"body": "Deep cleaning", "list": "A | B"}),
            ],
        ),
    )
    row = _content_row(order)
    assert row == {
        "ref": "main",
        "businessName": "Sample Cleaning",
        "tagline": "Family-owned cleaning.",
        "homeHeadline": "Mighty clean",
        "homeIntro": "We clean.",
        "servicesBody": "Deep cleaning",
        "servicesList": "A | B",
    }


def test_create_data_collection_path_and_fields():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = _json_body(request)
        return httpx.Response(200, json={"collection": {"id": "SiteContent"}})

    client = WixClient("k", transport=httpx.MockTransport(handler))
    client.create_data_collection(
        site_id="s", collection_id="SiteContent", display_name="Site Content",
        field_keys=["ref", "homeHeadline"])
    assert seen["path"] == "/wix-data/v2/collections"
    col = seen["body"]["collection"]
    assert col["id"] == "SiteContent"
    assert [f["key"] for f in col["fields"]] == ["ref", "homeHeadline"]
    assert all(f["type"] == "TEXT" for f in col["fields"])


def test_content_parks_without_collection_mapping():
    order = _order()
    st = WebsiteBuildState(order_id="wb-1", site_id="s-1", order=order)
    wix = WixClient("k", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"dataItem": {"id": "x"}})))
    ctx = StageContext(state=st, order=order, settings=_settings(), wix=wix)
    res = _content_stage(ctx)
    assert res.parked and res.park_reason == "cms_collection_unmapped"


def test_content_inserts_row_when_collection_empty():
    order = _order()
    st = WebsiteBuildState(order_id="wb-1", site_id="s-1", order=order)
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append((request.method, request.url.path))
        if request.url.path.endswith("/query"):
            return httpx.Response(200, json={"dataItems": []})  # empty -> insert
        return httpx.Response(200, json={"dataItem": {"id": "new-1"}})

    wix = WixClient("k", transport=httpx.MockTransport(handler))
    ctx = StageContext(
        state=st, order=order,
        settings=_settings(website_content_collection_id="col-1"), wix=wix,
    )
    res = _content_stage(ctx)
    assert res.ok
    assert res.detail["content_item_ids"] == ["new-1"]
    assert res.detail["content_upserted"] == "inserted"
    assert ("POST", "/wix-data/v2/items") in paths


def test_content_updates_existing_row_idempotent():
    order = _order()
    st = WebsiteBuildState(order_id="wb-1", site_id="s-1", order=order)
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append((request.method, request.url.path))
        if request.url.path.endswith("/query"):
            return httpx.Response(200, json={"dataItems": [{"data": {"_id": "existing-1"}}]})
        return httpx.Response(200, json={"dataItem": {"id": "existing-1"}})

    wix = WixClient("k", transport=httpx.MockTransport(handler))
    ctx = StageContext(
        state=st, order=order,
        settings=_settings(website_content_collection_id="col-1"), wix=wix,
    )
    res = _content_stage(ctx)
    assert res.ok
    assert res.detail["content_item_ids"] == ["existing-1"]
    assert res.detail["content_upserted"] == "updated"
    assert ("PUT", "/wix-data/v2/items/existing-1") in paths


def test_settle_barter_parks_without_lender():
    order = _order(settlement_kind="barter")
    st = WebsiteBuildState(order_id="wb-1", order=order)
    ctx = StageContext(state=st, order=order, settings=_settings())
    res = _settle_stage(ctx)
    assert res.parked and res.park_reason == "barter_lender_unset"


# ---------------------------------------------------------------------------
# CLI driver (the walk-through surface)
# ---------------------------------------------------------------------------

def test_cli_start_approve_advance(tmp_path, capsys):
    import json as _json

    from backend.website import cli

    order = {
        "customer_name": "Sample Customer",
        "settlement_kind": "barter",
        "settlement_lender_id": "sample-customer",
        "settlement_amount_usd": 740.0,
        "brief": {
            "business_name": "Mackabee Catering",
            "business_description": "Home cooked catering for local events.",
            "existing_site_id": "site-xyz",
            "pages": [{"slug": "home", "title": "Home",
                       "content": {"intro": "Fresh local catering."}}],
        },
    }
    order_file = tmp_path / "harmony.json"
    order_file.write_text(_json.dumps(order), encoding="utf-8")

    assert cli.main(["start", "--order", str(order_file)]) == 0
    state = _json.loads(capsys.readouterr().out)
    oid = state["order_id"]
    assert oid.startswith("wb-")

    # Unapproved advance pauses.
    assert cli.main(["advance", "--order-id", oid]) == 0
    paused = _json.loads(capsys.readouterr().out)
    assert paused["status"] == "awaiting_approval"

    # Approve brief, advance -> brief completes.
    assert cli.main(["approve", "--order-id", oid, "--stage", "brief"]) == 0
    capsys.readouterr()
    assert cli.main(["advance", "--order-id", oid]) == 0
    done_brief = _json.loads(capsys.readouterr().out)
    assert "brief" in done_brief["completed_stages"]
