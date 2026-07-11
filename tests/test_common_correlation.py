from backend.common import correlation


def setup_function(_fn):
    correlation.set_trace_id(None)  # type: ignore[arg-type]


def test_new_trace_id_is_hex_32():
    tid = correlation.new_trace_id()
    assert len(tid) == 32
    int(tid, 16)  # raises if not hex


def test_ensure_trace_id_creates_if_missing():
    tid = correlation.ensure_trace_id()
    assert tid == correlation.get_trace_id()


def test_ensure_trace_id_idempotent():
    a = correlation.ensure_trace_id()
    b = correlation.ensure_trace_id()
    assert a == b


def test_set_and_get_round_trip():
    correlation.set_trace_id("custom-trace")
    assert correlation.get_trace_id() == "custom-trace"


def test_headers_includes_correlation_id():
    correlation.set_trace_id("hdr-trace")
    h = correlation.headers()
    assert h["X-Correlation-Id"] == "hdr-trace"
    assert h["X-Trace-Id"] == "hdr-trace"


def test_with_trace_merges_extra():
    correlation.set_trace_id("merge-trace")
    out = correlation.with_trace({"k": "v"})
    assert out["trace_id"] == "merge-trace"
    assert out["k"] == "v"
