"""Tests for the shared email suppression-read helper (fail-open)."""

from __future__ import annotations

import backend.common.suppression as supp


class _FakeTable:
    def __init__(self, items: set[str] | None = None, raise_exc: Exception | None = None) -> None:
        self._items = {e.lower() for e in (items or set())}
        self._raise = raise_exc

    def get_item(self, Key=None):  # noqa: N803 — boto3 kwarg name is Key
        if self._raise is not None:
            raise self._raise
        email = (Key or {}).get("email", "")
        return {"Item": {"email": email}} if email in self._items else {}


def _patch_table(monkeypatch, table) -> None:
    monkeypatch.setattr(supp.aws, "table", lambda *a, **k: table)


def test_suppressed_email_returns_true(monkeypatch):
    _patch_table(monkeypatch, _FakeTable({"blocked@example.com"}))
    assert supp.is_email_suppressed("blocked@example.com") is True


def test_unsuppressed_email_returns_false(monkeypatch):
    _patch_table(monkeypatch, _FakeTable({"blocked@example.com"}))
    assert supp.is_email_suppressed("fresh@example.com") is False


def test_email_is_lowercased_and_stripped(monkeypatch):
    _patch_table(monkeypatch, _FakeTable({"blocked@example.com"}))
    assert supp.is_email_suppressed("  Blocked@Example.COM ") is True


def test_empty_email_returns_false_without_touching_table(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("table() must not be called for an empty address")

    monkeypatch.setattr(supp.aws, "table", _boom)
    assert supp.is_email_suppressed("") is False
    assert supp.is_email_suppressed("   ") is False


def test_fail_open_on_get_item_error(monkeypatch):
    # A read error must NOT block a send: fail-open returns False, never raises.
    _patch_table(monkeypatch, _FakeTable(raise_exc=RuntimeError("ddb unavailable")))
    assert supp.is_email_suppressed("anyone@example.com") is False


def test_fail_open_on_table_factory_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("boto bootstrap failed")

    monkeypatch.setattr(supp.aws, "table", _boom)
    assert supp.is_email_suppressed("anyone@example.com") is False
