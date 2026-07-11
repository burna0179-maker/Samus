from backend.common.text import looks_like_email, sanitize_for_log, snippet, truncate


def test_truncate_under_limit_passes_through():
    assert truncate("hello", 10) == "hello"


def test_truncate_over_limit_appends_suffix():
    out = truncate("abcdefghij", 5)
    assert out.endswith("â¦")
    assert len(out) == 5


def test_truncate_none_returns_empty():
    assert truncate(None, 5) == ""


def test_snippet_collapses_whitespace():
    assert snippet("a   b\n\tc") == "a b c"


def test_snippet_strips_control_chars():
    assert "\x00" not in snippet("hello\x00world")


def test_snippet_truncates():
    out = snippet("a" * 300, max_len=50)
    assert len(out) == 50


def test_sanitize_for_log_handles_non_string():
    assert sanitize_for_log({"a": 1}) == "{'a': 1}"


def test_sanitize_for_log_handles_unrepr_obj():
    class Bad:
        def __str__(self):
            raise RuntimeError("nope")

    out = sanitize_for_log(Bad())
    assert "unstringable" in out


def test_looks_like_email_basic():
    assert looks_like_email("a@b.co")
    assert not looks_like_email("not an email")
    assert not looks_like_email("a@b")
    assert not looks_like_email("")
