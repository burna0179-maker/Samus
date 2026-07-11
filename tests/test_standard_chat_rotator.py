"""Spice rotator (Samus STANDARD)."""

from __future__ import annotations

import pytest

from backend.standard.chat import SpicePool, SpiceRotator
from backend.standard.chat.spice_rotator import SpiceState


def _pool() -> SpicePool:
    return SpicePool(categories={"default": ["a", "b", "c"], "empty": []})


def test_first_call_picks_first_line():
    rot = SpiceRotator(pool=_pool(), spice_turns=3)
    s = SpiceState(category_id="default")
    assert rot.next_spice(s) == "a"


def test_rotation_advances_every_n_turns():
    rot = SpiceRotator(pool=_pool(), spice_turns=2)
    s = SpiceState(category_id="default")
    assert rot.next_spice(s) == "a"
    assert rot.next_spice(s) == "a"
    assert rot.next_spice(s) == "b"


def test_empty_category_yields_empty_string():
    assert SpiceRotator(pool=_pool()).next_spice(SpiceState(category_id="empty")) == ""


def test_unknown_category_yields_empty_string():
    assert SpiceRotator(pool=_pool()).next_spice(SpiceState(category_id="ghost")) == ""


def test_spice_turns_zero_rejected():
    with pytest.raises(ValueError):
        SpiceRotator(pool=_pool(), spice_turns=0)


def test_peek_does_not_advance():
    rot = SpiceRotator(pool=_pool(), spice_turns=3)
    s = SpiceState(category_id="default")
    rot.next_spice(s)
    t = s.turn
    rot.peek(s)
    assert s.turn == t
