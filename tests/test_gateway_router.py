"""Unit tests for backend.gateway.router.resolve_target."""
from __future__ import annotations

import pytest

from backend.common.settings import reload_settings
from backend.gateway.router import resolve_target


def test_resolve_target_returns_url(monkeypatch):
    monkeypatch.setenv("LEADGEN_URL", "http://leadgen.internal:8000")
    monkeypatch.setenv("PROSPECTING_URL", "http://prospecting.internal:8000")
    reload_settings()
    assert resolve_target("leadgen") == "http://leadgen.internal:8000"
    assert resolve_target("prospecting") == "http://prospecting.internal:8000"


def test_resolve_target_unknown_raises_keyerror(monkeypatch):
    monkeypatch.setenv("LEADGEN_URL", "http://leadgen.internal:8000")
    reload_settings()
    with pytest.raises(KeyError):
        resolve_target("nonexistent_service")


def test_resolve_target_empty_settings_raises(monkeypatch):
    monkeypatch.delenv("LEADGEN_URL", raising=False)
    monkeypatch.delenv("PROSPECTING_URL", raising=False)
    monkeypatch.delenv("SCAFFOLD_URL", raising=False)
    monkeypatch.delenv("FULFILLMENT_URL", raising=False)
    monkeypatch.delenv("MEMORY_URL", raising=False)
    monkeypatch.delenv("SAMUS_GATEWAY_URLS", raising=False)
    reload_settings()
    with pytest.raises(KeyError):
        resolve_target("anything")
