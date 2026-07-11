"""CA SOS lookup — deterministic parse, miss handling, network-error handling."""

from __future__ import annotations

from backend.prospecting.sources.ca_sos import lookup_ca_sos


class _Resp:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


class _OkHttp:
    def __init__(self, html: str):
        self._html = html
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):  # noqa: D401
        self.calls.append((url, dict(params or {})))
        return _Resp(200, self._html)


class _MissHttp:
    def get(self, url, params=None, timeout=None):
        return _Resp(200, "<html><body>No results found.</body></html>")


class _BoomHttp:
    def get(self, url, params=None, timeout=None):
        raise RuntimeError("network down")


_MATCH_HTML = """
<html><body>
  <h2>Acme Widgets Inc</h2>
  <div>Entity Number: C1234567</div>
  <div>Entity Status: Active</div>
  <div>Registration Date: 03/14/2017</div>
</body></html>
"""


def test_ca_sos_parses_match_deterministically():
    http = _OkHttp(_MATCH_HTML)
    sig = lookup_ca_sos("Acme Widgets Inc", http=http)
    assert sig is not None
    assert sig.kind == "public_registry"
    assert sig.confidence == "high"
    assert sig.evidence["sos_number"] == "C1234567"
    assert sig.evidence["entity_status"].lower().startswith("active")
    assert sig.evidence["filing_date"] == "03/14/2017"
    assert sig.evidence["registry"] == "ca_sos"
    assert http.calls and "q" in http.calls[0][1]


def test_ca_sos_returns_none_on_miss():
    assert lookup_ca_sos("Nonexistent Corp", http=_MissHttp()) is None


def test_ca_sos_returns_none_on_network_error():
    assert lookup_ca_sos("Acme", http=_BoomHttp()) is None


def test_ca_sos_returns_none_on_empty_input():
    assert lookup_ca_sos("", http=_OkHttp(_MATCH_HTML)) is None
