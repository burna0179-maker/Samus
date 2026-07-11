"""Guard + dedup tests for stake_sentence."""

from __future__ import annotations

import pytest

from backend.common import stake_sentence_guard as g


_VALID = (
    "Alex picked you because your Yuba City HVAC ranks for fewer keywords "
    "than two of your neighbors combined."
)


def _redirect_dedup(monkeypatch, tmp_path):
    path = tmp_path / "dedup.json"
    monkeypatch.setenv("SAMUS_STAKE_SENTENCE_DEDUP_PATH", str(path))
    g.reset_dedup_ledger()
    return path


def test_validate_too_short_rejects():
    with pytest.raises(g.StakeSentenceRejected) as exc:
        g.validate_stake_sentence("Short Note")
    assert "too_short" in exc.value.reason


def test_validate_too_long_rejects():
    long = "A" + ("a" * 280) + "."
    with pytest.raises(g.StakeSentenceRejected) as exc:
        g.validate_stake_sentence(long)
    assert "too_long" in exc.value.reason


@pytest.mark.parametrize(
    "phrase",
    ["i hope this finds you well", "we help businesses", "synergy"],
)
def test_validate_banned_phrase_rejects(phrase):
    text = f"Hello Acme Plumbing, {phrase} drive everything we ship here."
    with pytest.raises(g.StakeSentenceRejected) as exc:
        g.validate_stake_sentence(text)
    assert "banned_phrase" in exc.value.reason


def test_validate_all_lowercase_rejects():
    text = (
        "alex picked you because your hvac ranks for fewer keywords than two "
        "of your neighbors combined."
    )
    with pytest.raises(g.StakeSentenceRejected) as exc:
        g.validate_stake_sentence(text)
    assert "all_lowercase" in exc.value.reason


def test_validate_repeated_whitespace_rejects():
    text = "Alex picked you because Acme Plumbing            has a real opening."
    with pytest.raises(g.StakeSentenceRejected) as exc:
        g.validate_stake_sentence(text)
    assert "repeated_whitespace" in exc.value.reason


def test_validate_non_ascii_ratio_rejects():
    text = "Alex picked you " + ("🚀" * 50)
    with pytest.raises(g.StakeSentenceRejected) as exc:
        g.validate_stake_sentence(text)
    assert "non_ascii_ratio" in exc.value.reason


def test_validate_valid_passes():
    g.validate_stake_sentence(_VALID)


def test_dedup_hit(tmp_path, monkeypatch):
    _redirect_dedup(monkeypatch, tmp_path)
    g.record_hash(_VALID)
    assert g.is_duplicate(_VALID) is True
    assert (
        g.is_duplicate("Different Alex picked you because Acme has the worst homepage I have seen.")
        is False
    )


def test_dedup_normalization_collapses_whitespace_and_case(tmp_path, monkeypatch):
    _redirect_dedup(monkeypatch, tmp_path)
    g.record_hash(_VALID)
    variant = _VALID.upper().replace(" ", "  ")
    assert g.is_duplicate(variant) is True


def test_reset_dedup_clears(tmp_path, monkeypatch):
    _redirect_dedup(monkeypatch, tmp_path)
    g.record_hash(_VALID)
    g.reset_dedup_ledger()
    assert g.is_duplicate(_VALID) is False
