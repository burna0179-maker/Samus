"""Tests for backend.outreach.sendgrid_suppression_sync — the reputation loop
that pulls SendGrid's suppression lists into the outreach suppression file.

HTTP is mocked with httpx.MockTransport (no network). The merge tests use a tmp
file and assert idempotency + dry-run-writes-nothing.
"""
from __future__ import annotations

import json

import httpx
import pytest

from backend.outreach import campaign
from backend.outreach import sendgrid_suppression_sync as s


def _mock_client(routes: dict[str, list[dict]]) -> httpx.Client:
    """httpx.Client whose GET returns routes[path] (paginated by offset)."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path  # e.g. /v3/suppression/bounces
        key = path.split("/v3/")[-1]
        offset = int(request.url.params.get("offset", "0"))
        limit = int(request.url.params.get("limit", "500"))
        rows = routes.get(key, [])
        page = rows[offset:offset + limit]
        return httpx.Response(200, json=page)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_maps_email_to_reason() -> None:
    routes = {
        "suppression/bounces": [{"email": "b1@x.com"}, {"email": "B2@x.com"}],
        "suppression/blocks": [{"email": "blk@x.com"}],
        "suppression/spam_reports": [{"email": "spam@x.com"}],
        "suppression/invalid_emails": [{"email": "inv@x.com"}],
        "suppression/unsubscribes": [{"email": "unsub@x.com"}],
    }
    with _mock_client(routes) as c:
        out = s.fetch_suppressed_emails("KEY", http_client=c)
    assert out["b1@x.com"] == "bounce"
    assert out["b2@x.com"] == "bounce"          # lower-cased
    assert out["blk@x.com"] == "block"
    assert out["spam@x.com"] == "spam"
    assert out["inv@x.com"] == "invalid"
    assert out["unsub@x.com"] == "unsubscribe"
    assert len(out) == 6


def test_fetch_paginates() -> None:
    # 501 bounces forces a second page (limit=500).
    many = [{"email": f"u{i}@x.com"} for i in range(501)]
    with _mock_client({"suppression/bounces": many}) as c:
        out = s.fetch_suppressed_emails("KEY", http_client=c)
    assert len([e for e in out if e.startswith("u")]) == 501


def test_fetch_failsoft_on_bad_group() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "blocks" in request.url.path:
            return httpx.Response(500, json={"errors": ["boom"]})
        if "bounces" in request.url.path:
            return httpx.Response(200, json=[{"email": "ok@x.com"}])
        return httpx.Response(200, json=[])
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        out = s.fetch_suppressed_emails("KEY", http_client=c)  # must not raise
    assert out == {"ok@x.com": "bounce"}


def test_sync_merges_new_only(tmp_path) -> None:
    supp = tmp_path / "emailed_emails.txt"
    supp.write_text("existing@x.com\n", encoding="utf-8")
    routes = {"suppression/bounces": [{"email": "existing@x.com"},
                                      {"email": "new@x.com"}]}
    with _mock_client(routes) as c:
        r = s.sync(path=str(supp), api_key="KEY", base_url="https://api.sendgrid.com",
                   http_client=c)
    assert r.fetched == 2
    assert r.added == 1
    assert r.already_present == 1
    # the new one is appended as a tagged JSON line and is now honored
    loaded = campaign.load_suppression(str(supp))
    assert "new@x.com" in loaded and "existing@x.com" in loaded
    lines = [l for l in supp.read_text().splitlines() if l.strip()]
    tagged = [json.loads(l) for l in lines if l.startswith("{")]
    assert any(t["email"] == "new@x.com" and t["source"] == "sendgrid" for t in tagged)


def test_sync_is_idempotent(tmp_path) -> None:
    supp = tmp_path / "emailed_emails.txt"
    routes = {"suppression/bounces": [{"email": "a@x.com"}]}
    with _mock_client(routes) as c1:
        r1 = s.sync(path=str(supp), api_key="K", http_client=c1)
    with _mock_client(routes) as c2:
        r2 = s.sync(path=str(supp), api_key="K", http_client=c2)
    assert r1.added == 1
    assert r2.added == 0   # second run adds nothing
    assert len([l for l in supp.read_text().splitlines() if l.strip()]) == 1


def test_sync_dry_run_writes_nothing(tmp_path) -> None:
    supp = tmp_path / "emailed_emails.txt"
    routes = {"suppression/bounces": [{"email": "a@x.com"}]}
    with _mock_client(routes) as c:
        r = s.sync(path=str(supp), api_key="K", dry_run=True, http_client=c)
    assert r.fetched == 1
    assert r.added == 0
    assert "a@x.com" in r.emails          # still returned for in-memory honoring
    assert not supp.exists()              # nothing written


def test_sync_no_key_returns_empty(tmp_path) -> None:
    supp = tmp_path / "emailed_emails.txt"
    r = s.sync(path=str(supp), api_key="", http_client=None)
    assert r.fetched == 0
    assert r.emails == set()
    assert not supp.exists()
