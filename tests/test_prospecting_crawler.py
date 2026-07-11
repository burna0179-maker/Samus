"""Tests for backend.prospecting.crawler."""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx

from backend.prospecting import crawler


def test_fetch_homepage_success(monkeypatch):
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.text = "<html><body>hi</body></html>"
    fake_response.url = "https://example.com/"

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def get(self, url):
            return fake_response

    monkeypatch.setattr(crawler.httpx, "Client", _FakeClient)

    result = crawler.fetch_homepage("https://example.com")
    assert result["status_code"] == 200
    assert result["html"] == "<html><body>hi</body></html>"
    assert result["final_url"] == "https://example.com/"
    assert result["fetch_error"] is None


def test_fetch_homepage_404(monkeypatch):
    fake_response = MagicMock()
    fake_response.status_code = 404
    fake_response.text = "Not Found"
    fake_response.url = "https://example.com/missing"

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def get(self, url):
            return fake_response

    monkeypatch.setattr(crawler.httpx, "Client", _FakeClient)

    result = crawler.fetch_homepage("https://example.com/missing")
    assert result["status_code"] == 404
    assert result["html"] is None


def test_fetch_homepage_timeout(monkeypatch):
    monkeypatch.setattr(crawler.time, "sleep", lambda _s: None)

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def get(self, url):
            raise httpx.TimeoutException("read timeout")

    monkeypatch.setattr(crawler.httpx, "Client", _FakeClient)

    result = crawler.fetch_homepage("https://slow.example/")
    assert result["status_code"] == 0
    assert result["html"] is None
    assert result["fetch_error"] and "timeout" in result["fetch_error"].lower()


def test_fetch_homepage_empty_url():
    result = crawler.fetch_homepage("")
    assert result["fetch_error"] == "empty_url"


def test_is_dead_or_junk_status_4xx():
    page = {"final_url": "https://x.com", "status_code": 404, "html": None, "fetch_error": None}
    assert crawler.is_dead_or_junk(page) is True


def test_is_dead_or_junk_no_html():
    page = {"final_url": "https://x.com", "status_code": 200, "html": None, "fetch_error": None}
    assert crawler.is_dead_or_junk(page) is True


def test_is_dead_or_junk_facebook():
    page = {
        "final_url": "https://www.facebook.com/some-biz",
        "status_code": 200,
        "html": "<html>fb page</html>",
        "fetch_error": None,
    }
    assert crawler.is_dead_or_junk(page) is True


def test_is_dead_or_junk_yelp():
    page = {
        "final_url": "https://www.yelp.com/biz/acme-roofing",
        "status_code": 200,
        "html": "<html>yelp listing</html>",
        "fetch_error": None,
    }
    assert crawler.is_dead_or_junk(page) is True


def test_is_dead_or_junk_yellowpages():
    page = {
        "final_url": "https://www.yellowpages.com/some-biz",
        "status_code": 200,
        "html": "<html>...</html>",
        "fetch_error": None,
    }
    assert crawler.is_dead_or_junk(page) is True


def test_is_dead_or_junk_instagram():
    page = {
        "final_url": "https://www.instagram.com/somebiz/",
        "status_code": 200,
        "html": "<html>insta</html>",
        "fetch_error": None,
    }
    assert crawler.is_dead_or_junk(page) is True


def test_is_dead_or_junk_parked_marker():
    page = {
        "final_url": "https://acme-roofing-park.com/",
        "status_code": 200,
        "html": "<html>This domain is parked. Buy this domain!</html>",
        "fetch_error": None,
    }
    assert crawler.is_dead_or_junk(page) is True


def test_is_dead_or_junk_real_site_ok():
    page = {
        "final_url": "https://www.acmeroofing.com/",
        "status_code": 200,
        "html": "<html><body><h1>Acme Roofing</h1></body></html>",
        "fetch_error": None,
    }
    assert crawler.is_dead_or_junk(page) is False


# ---------------------------------------------------------------------------
# classify_website — granular reachability status
# ---------------------------------------------------------------------------

def test_classify_website_live():
    page = {
        "final_url": "https://www.acmeroofing.com/",
        "status_code": 200,
        "html": "<html><body><h1>Acme Roofing</h1></body></html>",
        "fetch_error": None,
    }
    assert crawler.classify_website(page) == "live"


def test_classify_website_domain_unresolved():
    page = {
        "final_url": "http://www.showcaseagent.com/",
        "status_code": 0,
        "html": None,
        "fetch_error": "ConnectError: [Errno 11001] getaddrinfo failed",
    }
    assert crawler.classify_website(page) == "domain_unresolved"


def test_classify_website_unreachable_timeout():
    page = {
        "final_url": "http://slow.example/",
        "status_code": 0,
        "html": None,
        "fetch_error": "timeout: timed out",
    }
    assert crawler.classify_website(page) == "unreachable_timeout"


def test_classify_website_unreachable_other():
    page = {
        "final_url": "http://x.example/",
        "status_code": 0,
        "html": None,
        "fetch_error": "ConnectError: connection refused",
    }
    assert crawler.classify_website(page) == "unreachable"


def test_classify_website_http_error():
    page = {"final_url": "https://x.com/missing", "status_code": 404,
            "html": None, "fetch_error": None}
    assert crawler.classify_website(page) == "http_error"


def test_classify_website_server_error():
    page = {"final_url": "https://x.com/", "status_code": 503,
            "html": None, "fetch_error": None}
    assert crawler.classify_website(page) == "server_error"


def test_classify_website_empty():
    page = {"final_url": "https://x.com/", "status_code": 200,
            "html": "   ", "fetch_error": None}
    assert crawler.classify_website(page) == "empty"


def test_classify_website_social_only():
    page = {"final_url": "https://www.facebook.com/some-biz", "status_code": 200,
            "html": "<html>fb</html>", "fetch_error": None}
    assert crawler.classify_website(page) == "social_only"


def test_classify_website_parked():
    page = {"final_url": "https://acme-park.com/", "status_code": 200,
            "html": "<html>Buy this domain! This domain is parked.</html>",
            "fetch_error": None}
    assert crawler.classify_website(page) == "parked"


def test_classify_website_non_dict_is_unreachable():
    assert crawler.classify_website(None) == "unreachable"


def test_is_dead_or_junk_agrees_with_classify_website():
    """is_dead_or_junk is exactly `classify_website(page) != "live"`."""
    pages = [
        {"final_url": "https://ok.com/", "status_code": 200,
         "html": "<h1>real</h1>", "fetch_error": None},
        {"final_url": "https://x.com/", "status_code": 404,
         "html": None, "fetch_error": None},
        {"final_url": "http://x.com/", "status_code": 0,
         "html": None, "fetch_error": "getaddrinfo failed"},
    ]
    for page in pages:
        assert crawler.is_dead_or_junk(page) == (
            crawler.classify_website(page) != "live"
        )


# ---------------------------------------------------------------------------
# 2026-05-21 hardening — browser UA, retry, access_blocked / gone classes
# ---------------------------------------------------------------------------

def _seq_client(monkeypatch, statuses):
    """Install a fake httpx.Client whose successive GETs yield `statuses`
    (each an int status code, or an Exception instance to raise). Returns a
    one-element list whose [0] is the GET call count."""
    calls = [0]
    seq = list(statuses)

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def get(self, url):
            i = calls[0]
            calls[0] += 1
            item = seq[min(i, len(seq) - 1)]
            if isinstance(item, Exception):
                raise item
            resp = MagicMock()
            resp.status_code = item
            resp.text = "<html><body>ok</body></html>" if item < 400 else "err"
            resp.url = url
            return resp

    monkeypatch.setattr(crawler.httpx, "Client", _FakeClient)
    monkeypatch.setattr(crawler.time, "sleep", lambda _s: None)
    return calls


def test_default_headers_send_a_browser_user_agent():
    """The crawler must not self-identify as a bot — WAFs 403 bot UAs."""
    ua = crawler._DEFAULT_HEADERS["User-Agent"]
    assert "bot" not in ua.lower()
    assert "Mozilla/5.0" in ua and "Chrome/" in ua


def test_classify_website_access_blocked_403():
    page = {"final_url": "https://x.com/", "status_code": 403,
            "html": None, "fetch_error": None}
    assert crawler.classify_website(page) == "access_blocked"


def test_classify_website_access_blocked_429():
    page = {"final_url": "https://x.com/", "status_code": 429,
            "html": None, "fetch_error": None}
    assert crawler.classify_website(page) == "access_blocked"


def test_classify_website_gone_410():
    page = {"final_url": "https://x.com/", "status_code": 410,
            "html": None, "fetch_error": None}
    assert crawler.classify_website(page) == "gone"


def test_classify_website_404_still_http_error():
    page = {"final_url": "https://x.com/", "status_code": 404,
            "html": None, "fetch_error": None}
    assert crawler.classify_website(page) == "http_error"


def test_fetch_homepage_retries_transient_then_succeeds(monkeypatch):
    """A 503 on the first attempt is retried; the 200 on retry is returned."""
    calls = _seq_client(monkeypatch, [503, 200])
    result = crawler.fetch_homepage("https://x.example/")
    assert calls[0] == 2
    assert result["status_code"] == 200
    assert result["html"]


def test_fetch_homepage_retries_a_403(monkeypatch):
    """403 (access_blocked) is non-terminal — retried once before giving up."""
    calls = _seq_client(monkeypatch, [403, 403])
    result = crawler.fetch_homepage("https://blocked.example/")
    assert calls[0] == 2
    assert result["status_code"] == 403
    assert crawler.classify_website(result) == "access_blocked"


def test_fetch_homepage_does_not_retry_404(monkeypatch):
    """404 is terminal — the server's definitive 'not here', no retry."""
    calls = _seq_client(monkeypatch, [404, 200])
    result = crawler.fetch_homepage("https://x.example/missing")
    assert calls[0] == 1
    assert result["status_code"] == 404


def test_fetch_homepage_does_not_retry_410(monkeypatch):
    calls = _seq_client(monkeypatch, [410, 200])
    result = crawler.fetch_homepage("https://x.example/gone")
    assert calls[0] == 1
    assert result["status_code"] == 410


def test_fetch_homepage_does_not_retry_success(monkeypatch):
    calls = _seq_client(monkeypatch, [200, 500])
    result = crawler.fetch_homepage("https://x.example/")
    assert calls[0] == 1
    assert result["status_code"] == 200


def test_fetch_homepage_retries_timeout_then_succeeds(monkeypatch):
    calls = _seq_client(monkeypatch, [httpx.TimeoutException("t"), 200])
    result = crawler.fetch_homepage("https://x.example/")
    assert calls[0] == 2
    assert result["status_code"] == 200


def test_fetch_homepage_blocks_ssrf_loopback():
    # RT NET-01: a poisoned website URL pointing at an internal address must be
    # refused before any connection (here a loopback IP literal needs no DNS).
    result = crawler.fetch_homepage("http://127.0.0.1:8420/admin")
    assert result["status_code"] == 0
    assert result["html"] is None
    assert "ssrf_blocked" in (result["fetch_error"] or "")


def test_fetch_homepage_blocks_ssrf_link_local_imds():
    result = crawler.fetch_homepage("http://169.254.169.254/latest/meta-data/")
    assert "ssrf_blocked" in (result["fetch_error"] or "")
