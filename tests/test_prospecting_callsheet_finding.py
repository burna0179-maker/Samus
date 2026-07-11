"""Gatekeeper-aware opener + callsheet_finding wiring (round-2 fix 2026-07-02).

The round-2 live-call audit showed Morgan's finding-first opener getting hung
up on when a receptionist answered — the opener committed to the specific
finding before Morgan could tell a gatekeeper from an owner. The fix reshapes
`_opener` to route to the decision-maker FIRST (honest cold-call + value teaser
+ owner-ask, finding withheld) and carries the specific finding on the record
(`callsheet_finding`) so Morgan can still deliver it once the owner is reached.

These tests pin:
  * the opener asks for the owner + is honest + withholds the specific finding
    + carries no banned time-ask + has no double-subject grammar bug;
  * `_top_finding`'s specific finding survives on `callsheet_finding` (present +
    empty-safe) across security-grade / seo-score / no-website cases;
  * the owner-ask is empty-safe and names a real owner when known.
"""

from __future__ import annotations

import re

from backend.prospecting.callsheet import (
    _looks_like_real_name,
    _opener,
    _owner_ask,
    _top_finding,
    build_call_sheet,
)
from backend.prospecting.models import ProspectRecord


_BANNED_TIME_ASKS = (
    "thirty seconds",
    "30 seconds",
    "do you have a minute",
    "got a minute",
    "a quick minute",
    "quick call",
)


# ---------------------------------------------------------------------------
# The three report cases: security-grade, seo-score, no-website
# ---------------------------------------------------------------------------

_SECURITY = ProspectRecord(
    company_name="Diamond Tax",
    industry="finance",
    city="Yuba City",
    website_status="live",
    security_grade="F",
)
_SEO = ProspectRecord(
    company_name="Bright Smiles Dental",
    industry="dentist",
    city="Marysville",
    website_status="live",
    seo_score=42,
)
_NO_SITE = ProspectRecord(
    company_name="Acme Plumbing",
    industry="plumbing",
    city="Live Oak",
    website_status="no_website",
)


def _openers():
    return {
        "security": build_call_sheet(_SECURITY),
        "seo": build_call_sheet(_SEO),
        "no_site": build_call_sheet(_NO_SITE),
    }


def test_opener_asks_for_the_owner_up_front():
    for label, sheet in _openers().items():
        low = sheet.callsheet_opener.lower()
        assert "owner" in low, f"{label}: opener must ask for the owner"
        assert "who handles" in low or "whoever handles" in low, (
            f"{label}: opener must route to whoever handles the website/marketing"
        )


def test_opener_is_honest_cold_call_with_value_teaser():
    for label, sheet in _openers().items():
        low = sheet.callsheet_opener.lower()
        # Honest pattern-interrupt.
        assert "cold call" in low, f"{label}: opener must be honest it's a cold call"
        # Value teaser present (intrigue) without the concrete finding.
        assert "costing you" in low, f"{label}: opener must tease the value/loss"


def test_opener_withholds_the_specific_finding():
    """The specific finding (grade letter, seo number, 'no real website' phrase)
    must NOT appear in the opener — that's the round-2 hang-up bug."""
    sheets = _openers()
    # Security-grade case: the finding phrase / grade letter is withheld.
    sec = sheets["security"].callsheet_opener
    assert _top_finding(_SECURITY) not in sec
    assert "security warning" not in sec.lower()
    # SEO case: the numeric score is withheld.
    seo = sheets["seo"].callsheet_opener
    assert "42" not in seo
    assert _top_finding(_SEO) not in seo
    # No-website case: the 'no real website' finding phrase is withheld.
    nos = sheets["no_site"].callsheet_opener
    assert _top_finding(_NO_SITE) not in nos
    assert "no real website" not in nos.lower()


def test_opener_carries_no_banned_time_ask():
    for label, sheet in _openers().items():
        low = sheet.callsheet_opener.lower()
        for banned in _BANNED_TIME_ASKS:
            assert banned not in low, f"{label}: banned time-ask {banned!r} present"


def test_opener_has_no_double_subject_grammar_bug():
    """Round-2 grammar bug: 'there's there's no working website'. The rewritten
    opener never emits a double subject / double 'there's'."""
    for label, sheet in _openers().items():
        opener = sheet.callsheet_opener
        low = opener.lower()
        assert "there's there's" not in low, f"{label}: double there's"
        assert "there is there" not in low, f"{label}: double subject"
        # No accidental doubled article/word run either.
        assert not re.search(r"\b(\w+)\s+\1\b", low), f"{label}: repeated-word bug in {opener!r}"


# ---------------------------------------------------------------------------
# The specific finding survives on callsheet_finding (present + empty-safe)
# ---------------------------------------------------------------------------


def test_callsheet_finding_present_and_matches_top_finding():
    """The specific finding is preserved on the record for the owner beat and
    equals the deterministic _top_finding phrase for each report case."""
    assert build_call_sheet(_SECURITY).callsheet_finding == _top_finding(_SECURITY)
    assert build_call_sheet(_SEO).callsheet_finding == _top_finding(_SEO)
    assert build_call_sheet(_NO_SITE).callsheet_finding == _top_finding(_NO_SITE)


def test_callsheet_finding_carries_the_concrete_detail():
    """The finding — withheld from the opener — carries the concrete detail
    that Morgan reveals to the owner at Step 2."""
    assert "F" in build_call_sheet(_SECURITY).callsheet_finding
    assert "42" in build_call_sheet(_SEO).callsheet_finding
    assert build_call_sheet(_NO_SITE).callsheet_finding  # non-empty for no-site


def test_callsheet_finding_is_empty_safe_never_none():
    """Even a wholly-empty record gets a non-None string finding (the
    universal manual-ops hook), so the {{callsheet_finding}} var is always
    a safe string for the prompt."""
    out = build_call_sheet(ProspectRecord())
    assert isinstance(out.callsheet_finding, str)
    assert out.callsheet_finding  # _top_finding always returns a usable phrase


# ---------------------------------------------------------------------------
# Owner-ask is empty-safe + names a real owner when known
# ---------------------------------------------------------------------------


def test_owner_ask_uses_real_owner_name_when_present():
    p = ProspectRecord(company_name="Acme", owner_name="Dana Reyes")
    ask = _owner_ask(p)
    assert "Dana" in ask
    # First-name only — not the full name.
    assert "Reyes" not in ask
    assert "who handles the website" in ask.lower() or "whoever handles" in ask.lower()


def test_owner_ask_falls_back_to_role_when_owner_name_absent():
    ask = _owner_ask(ProspectRecord(company_name="Acme"))
    assert "the owner" in ask.lower()
    assert "whoever handles your website" in ask.lower()


def test_owner_ask_never_voices_a_placeholder_token():
    """A raw template token / placeholder in owner_name must never leak into
    the ask — it falls back to the role-based wording."""
    for junk in ("{{owner_name}}", "[owner]", "N/A", "owner", "", "   "):
        ask = _owner_ask(ProspectRecord(company_name="Acme", owner_name=junk))
        assert "{{" not in ask and "[" not in ask
        assert "the owner" in ask.lower()


def test_looks_like_real_name_guards():
    assert _looks_like_real_name("Dana Reyes")
    assert _looks_like_real_name("Bob")
    assert not _looks_like_real_name("")
    assert not _looks_like_real_name("   ")
    assert not _looks_like_real_name("{{owner_name}}")
    assert not _looks_like_real_name("N/A")
    assert not _looks_like_real_name("owner")
    assert not _looks_like_real_name("123")


def test_opener_stake_sentence_prepended_verbatim():
    """A stake_sentence still rides verbatim as the first line before the
    gatekeeper-aware opener."""
    out = _opener(_SECURITY, stake_sentence="This one matters — $2k on the table.")
    assert out.startswith("This one matters — $2k on the table.")
    assert "cold call" in out.lower()
    assert "owner" in out.lower()
