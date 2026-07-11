"""Cloudflare Pages deploy adapter — build dir, _headers, wrangler deploy, dormancy."""

from __future__ import annotations

from types import SimpleNamespace

from backend.website import deploy_cloudflare as dc
from backend.website.site_builder import GeneratedSite


def _site():
    return GeneratedSite(
        files={"index.html": "<html><body>Sample Cleaning</body></html>"},
        taste_audit={"passed": True, "grade": "A"},
        pages=["index.html"],
    )


def _settings(**over):
    base = dict(
        website_deploy_enabled=True,
        website_deploy_host="cloudflare",
        cloudflare_api_token="cf-token",
        cloudflare_account_id="acct-1",
        cloudflare_pages_project="sample-cleaning",
    )
    base.update(over)
    return SimpleNamespace(**base)


# --- _headers (the security-grade-A unlock) --------------------------------


def test_security_headers_has_all_six():
    h = dc.build_security_headers()
    assert h.startswith("/*")
    for name in (
        "Content-Security-Policy",
        "Strict-Transport-Security",
        "X-Frame-Options",
        "X-Content-Type-Options",
        "Referrer-Policy",
        "Permissions-Policy",
    ):
        assert name in h
    # indented header lines under the /* rule
    assert "\n  Content-Security-Policy:" in h


# --- build dir -------------------------------------------------------------


def test_prepare_build_dir_writes_site_and_headers(tmp_path):
    out = dc.prepare_build_dir(_site(), out_dir=tmp_path / "build")
    assert (out / "index.html").read_text(encoding="utf-8").startswith("<html>")
    assert (out / "_headers").exists()
    assert "Strict-Transport-Security" in (out / "_headers").read_text(encoding="utf-8")


def test_prepare_build_dir_can_skip_headers(tmp_path):
    out = dc.prepare_build_dir(_site(), out_dir=tmp_path / "b", security_headers=False)
    assert not (out / "_headers").exists()


# --- wrangler deploy -------------------------------------------------------


def test_deploy_via_wrangler_parses_url(monkeypatch, tmp_path):
    def fake_run(cmd, *, env, timeout):
        assert env["CLOUDFLARE_API_TOKEN"] == "cf-token"
        assert env["CLOUDFLARE_ACCOUNT_ID"] == "acct-1"
        return SimpleNamespace(
            returncode=0,
            stdout="Uploading... ✨ Deployment complete! https://abcd1234.sample-cleaning.pages.dev\n",
            stderr="",
        )

    monkeypatch.setattr(dc, "_run", fake_run)
    res = dc.deploy_via_wrangler(
        tmp_path, project="sample-cleaning", api_token="cf-token", account_id="acct-1"
    )
    assert res.ok and res.url == "https://abcd1234.sample-cleaning.pages.dev"


def test_deploy_via_wrangler_nonzero_exit_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(
        dc, "_run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="auth error")
    )
    res = dc.deploy_via_wrangler(tmp_path, project="p", api_token="t", account_id="a")
    assert res.ok is False and "exit 1" in res.error


def test_deploy_via_wrangler_missing_wrangler(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise FileNotFoundError("npx")

    monkeypatch.setattr(dc, "_run", boom)
    res = dc.deploy_via_wrangler(tmp_path, project="p", api_token="t", account_id="a")
    assert res.ok is False and "not found" in res.error


# --- orchestrator dormancy -------------------------------------------------


def test_deploy_site_disabled():
    res = dc.deploy_site(_site(), settings=_settings(website_deploy_enabled=False))
    assert res.ok is False and res.error == "disabled"


def test_deploy_site_no_creds():
    res = dc.deploy_site(_site(), settings=_settings(cloudflare_api_token=""))
    assert res.ok is False and "unset" in res.error


def test_deploy_site_unsupported_host():
    res = dc.deploy_site(_site(), settings=_settings(website_deploy_host="geocities"))
    assert res.ok is False and "unsupported" in res.error


def test_deploy_site_runs_when_armed(monkeypatch, tmp_path):
    monkeypatch.setattr(dc, "create_project", lambda **k: {"status": 200})
    monkeypatch.setattr(
        dc,
        "_run",
        lambda *a, **k: SimpleNamespace(
            returncode=0, stdout="https://x.sample-cleaning.pages.dev", stderr=""
        ),
    )
    res = dc.deploy_site(_site(), settings=_settings(), out_dir=tmp_path / "build")
    assert res.ok and res.url.endswith(".pages.dev")
    assert (tmp_path / "build" / "_headers").exists()
