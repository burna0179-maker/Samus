"""Tests for backend.website.client_assets — operator-mandated brand-asset
isolation (no asset registered to one client may ship in another's build)."""

from __future__ import annotations

import base64
import hashlib
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.website.client_assets import (
    derive_client_key,
    enforce_asset_isolation,
    get_asset_path,
    list_assets,
    register_asset,
)

RUN_DATE = date(2026, 6, 11)


@pytest.fixture()
def artifact_root(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# client-key derivation
# ---------------------------------------------------------------------------


def test_client_key_precedence():
    assert derive_client_key("pr_ChIJabc", "acct_1", "Cool Breeze") == "pr_ChIJabc"
    assert derive_client_key("", "acct_1", "Cool Breeze") == "acct_1"
    assert derive_client_key("", "", "Cool Breeze Air, LLC") == "cool-breeze-air-llc"
    assert derive_client_key() == "unknown-client"
    # deterministic + filesystem-safe
    assert derive_client_key("pr_a/.\\b") == derive_client_key("pr_a/.\\b")
    assert "/" not in derive_client_key("pr_a/.\\b")


# ---------------------------------------------------------------------------
# register + manifest round-trip
# ---------------------------------------------------------------------------


def test_register_roundtrip_and_hash_provenance(artifact_root):
    data = b"ACME-LOGO-BYTES"
    entry = register_asset("pr_acme", data, "logo", original_name="logo.png", notes="operator drop")
    assert entry["sha256"] == hashlib.sha256(data).hexdigest()
    assert entry["kind"] == "logo"
    assert entry["source"] == "operator"
    assert entry["original_name"] == "logo.png"

    assets = list_assets("pr_acme")
    assert len(assets) == 1
    path = get_asset_path("pr_acme", entry["filename"])
    assert path is not None and path.read_bytes() == data

    # idempotent: same bytes + kind -> same entry, no duplicate
    again = register_asset("pr_acme", data, "logo", original_name="logo.png")
    assert again["sha256"] == entry["sha256"]
    assert len(list_assets("pr_acme")) == 1

    # file-path source
    f = artifact_root / "icon.svg"
    f.write_bytes(b"<svg/>")
    e2 = register_asset("pr_acme", f, "icon")
    assert e2["original_name"] == "icon.svg"
    assert len(list_assets("pr_acme")) == 2


def test_missing_store_is_empty_not_error(artifact_root):
    assert list_assets("pr_nobody") == []
    assert get_asset_path("pr_nobody", "x.png") is None


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def test_gate_drops_unmanifested_local_asset(artifact_root):
    files = {
        "index.html": '<html><img src="./assets/rogue.png"></html>',
        "assets/rogue.png": "FAKE-PNG-BYTES",
    }
    clean, violations = enforce_asset_isolation(files, "pr_b")
    assert "assets/rogue.png" not in clean
    assert "./assets/rogue.png" not in clean["index.html"]
    assert any("not in client" in v for v in violations)


def test_gate_strips_asset_registered_to_another_client(artifact_root):
    """THE core case: client B's build referencing client A's logo (by shipped
    bytes AND by path) -> stripped + violation."""
    data = b"ACME-LOGO-BYTES"
    register_asset("pr_a", data, "logo", original_name="acme-logo.png")

    files = {
        "index.html": '<img src="./assets/acme-logo.png"><a href="about.html">x</a>',
        "assets/stolen.png": data.decode("utf-8"),  # A's exact bytes shipped by B
        "about.html": "<p>fine</p>",
    }
    clean, violations = enforce_asset_isolation(files, "pr_b")
    assert "assets/stolen.png" not in clean  # bytes match -> dropped
    assert "./assets/acme-logo.png" not in clean["index.html"]  # path match -> stripped
    assert any("pr_a" in v for v in violations)
    assert clean["about.html"] == "<p>fine</p>"  # untouched

    # The SAME build for client A passes clean.
    a_files = {"index.html": '<img src="./assets/acme-logo.png">'}
    clean_a, v_a = enforce_asset_isolation(a_files, "pr_a")
    assert v_a == []
    assert "./assets/acme-logo.png" in clean_a["index.html"]


def test_gate_strips_foreign_data_uri(artifact_root):
    data = b"ACME-LOGO-BYTES"
    register_asset("pr_a", data, "logo", original_name="acme-logo.png")
    uri = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
    files = {"index.html": f'<img src="{uri}">'}

    clean, violations = enforce_asset_isolation(files, "pr_b")
    assert uri not in clean["index.html"]
    assert any("data-URI" in v and "pr_a" in v for v in violations)

    # Own client embedding own bytes: allowed.
    clean_a, v_a = enforce_asset_isolation(files, "pr_a")
    assert v_a == [] and uri in clean_a["index.html"]


def test_gate_remote_allowlist_and_strict_mode(artifact_root):
    files = {
        "index.html": (
            '<link href="https://fonts.googleapis.com/css2?family=Outfit">'
            '<script src="https://cdn.tailwindcss.com"></script>'
            '<img src="https://demo-x.pages.dev/hero.png">'
            '<img src="https://evil.example/logo.png">'
        )
    }
    clean, violations = enforce_asset_isolation(files, "pr_b", own_domains=("demo-x.pages.dev",))
    # allowlisted + own domain pass silently; unknown remote = warning, NOT stripped
    assert [v for v in violations if not v.startswith("warning:")] == []
    assert any("evil.example" in v for v in violations)
    assert "https://evil.example/logo.png" in clean["index.html"]

    clean_s, v_s = enforce_asset_isolation(
        files, "pr_b", own_domains=("demo-x.pages.dev",), strict=True
    )
    assert "https://evil.example/logo.png" not in clean_s["index.html"]
    assert any("strict" in v for v in v_s)
    assert "fonts.googleapis.com" in clean_s["index.html"]


# ---------------------------------------------------------------------------
# demo_sites integration
# ---------------------------------------------------------------------------


def _rec(pid: str, name: str):
    from backend.prospecting.models import ProspectRecord

    return ProspectRecord(
        prospect_id=pid,
        company_name=name,
        phone="555-0000",
        industry="plumbing",
        city="Yuba City",
        state="CA",
        lead_score=75,
        website_status="no_website",
        business_description="Local plumbing.",
    )


def test_demo_sites_per_client_assets_and_gate(artifact_root, monkeypatch):
    """Store with a logo for prospect A: A's build references + ships it; B's
    build does NOT, and a planted cross-reference is stripped + recorded on
    the result and the call sheet."""
    from backend.website.demo_sites import build_demo_sites
    from backend.website.site_builder import GeneratedSite

    data = b"A-REAL-LOGO"
    register_asset("pr_A", data, "logo", original_name="a-logo.png")

    def fake_build(brief, *, settings=None, media=None, public_url=""):
        media = media or {}
        hero = media.get("hero_image", "")
        # Every build PLANTS a reference to A's logo — the gate must let A's
        # own through and strip it from B's.
        html = f'<html><img src="{hero}"><img src="./assets/a-logo.png"></html>'
        return GeneratedSite(files={"index.html": html}, pages=["index.html"])

    monkeypatch.setattr("backend.website.site_builder.build_static_site", fake_build)

    settings = SimpleNamespace(
        website_demo_sites_enabled=True,
        anthropic_api_key="",
        cloudflare_api_token="",
        cloudflare_account_id="",
        demo_sites_ceiling_usd=5.0,
        website_asset_isolation_strict=False,
    )
    run = build_demo_sites(
        [_rec("pr_A", "Acme Plumbing"), _rec("pr_B", "Bravo Plumbing")],
        deploy=False,
        use_llm_copy=False,
        settings=settings,
        run_date=RUN_DATE,
    )
    assert run.built == 2 and run.failed == 0
    res_a = next(r for r in run.results if r.prospect_id == "pr_A")
    res_b = next(r for r in run.results if r.prospect_id == "pr_B")

    html_a = (Path(res_a.output_dir) / "index.html").read_text(encoding="utf-8")
    html_b = (Path(res_b.output_dir) / "index.html").read_text(encoding="utf-8")
    assert "./assets/a-logo.png" in html_a  # A keeps its own logo
    assert res_a.asset_violations == []
    assert (Path(res_a.output_dir) / "assets" / "a-logo.png").read_bytes() == data

    assert "a-logo.png" not in html_b  # cross-reuse stripped
    assert res_b.asset_violations and any("pr_a" in v.lower() for v in res_b.asset_violations)
    assert not (Path(res_b.output_dir) / "assets" / "a-logo.png").exists()

    sheet = Path(run.txt_path).read_text(encoding="utf-8")
    assert "ASSET ISOLATION" in sheet
