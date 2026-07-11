"""Info-gaps loader + open-gap counting."""
from __future__ import annotations


def test_missing_file_empty_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_INFO_GAPS_PATH", str(tmp_path / "x.yaml"))
    from backend.finance.info_gaps import load_registry
    reg, loaded = load_registry()
    assert loaded is False
    assert reg.gaps == []


def test_summarize_counts_open_by_priority(tmp_path, monkeypatch):
    p = tmp_path / "ig.yaml"
    p.write_text(
        "gaps:\n"
        "  - {id: G1, priority: critical, gap: 'a', how_to_close: 'b', status: open}\n"
        "  - {id: G2, priority: critical, gap: 'a', how_to_close: 'b', status: open}\n"
        "  - {id: G3, priority: high, gap: 'a', how_to_close: 'b', status: open}\n"
        "  - {id: G4, priority: medium, gap: 'a', how_to_close: 'b', status: resolved, resolved_date: 2026-05-01}\n"
        "  - {id: G5, priority: low, gap: 'a', how_to_close: 'b', status: open}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_INFO_GAPS_PATH", str(p))
    from backend.finance.info_gaps import load_registry, summarize
    reg, loaded = load_registry()
    s = summarize(reg, loaded, "2026-05-15T00:00:00Z")
    assert s.open_total == 4
    assert s.open_by_priority == {"critical": 2, "high": 1, "low": 1}
    assert len(s.critical_open) == 2
    assert all(g.priority == "critical" for g in s.critical_open)


def test_resolved_gaps_excluded_from_open_count(tmp_path, monkeypatch):
    p = tmp_path / "ig.yaml"
    p.write_text(
        "gaps:\n"
        "  - {id: G1, priority: critical, gap: 'a', how_to_close: 'b',\n"
        "     status: resolved, resolved_date: 2026-05-10,\n"
        "     resolution_note: 'closed'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_INFO_GAPS_PATH", str(p))
    from backend.finance.info_gaps import load_registry, summarize
    reg, loaded = load_registry()
    s = summarize(reg, loaded, "t")
    assert s.open_total == 0
    assert s.critical_open == []
