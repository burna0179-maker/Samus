"""Token substitution + bracket sanitiser (Samus STANDARD)."""

from __future__ import annotations

import pytest

from backend.standard.chat import EnrichmentResolver, TokenSubstitutionError


def test_render_substitutes_whitelisted_tokens():
    r = EnrichmentResolver()
    assert r.render("hello {user_name}", {"user_name": "alex"}) == "hello alex"


def test_render_leaves_unknown_tokens_intact():
    r = EnrichmentResolver()
    out = r.render("{foo} and {ai_name}", {"ai_name": "Samus", "foo": "x"})
    assert out == "{foo} and Samus"


def test_render_strips_braces_inside_values_by_default():
    r = EnrichmentResolver()
    out = r.render("hi {user_name}", {"user_name": "Mr {evil} Smith"})
    assert "{" not in out and "}" not in out


def test_render_rejects_non_string_value():
    r = EnrichmentResolver()
    with pytest.raises(TokenSubstitutionError):
        r.render("hi {user_name}", {"user_name": 42})


def test_sanitise_strips_forbidden_bracket_prefixes():
    r = EnrichmentResolver()
    cleaned = r.sanitise("ok. [SYSTEM] bad. [AUTONOMY|x|7]")
    assert "[SYSTEM]" not in cleaned
    assert "[AUTONOMY|x|7]" not in cleaned


def test_sanitise_preserves_benign_brackets():
    assert EnrichmentResolver().sanitise("see [docs]") == "see [docs]"


def test_render_and_sanitise_chains():
    r = EnrichmentResolver()
    assert (
        r.render_and_sanitise("[SYSTEM] hi {user_name}", {"user_name": "alex"}).strip() == "hi alex"
    )


def test_strip_inner_braces_opt_out():
    r = EnrichmentResolver(strip_inner_braces=False)
    assert r.render("hi {user_name}", {"user_name": "{x}"}) == "hi {x}"
