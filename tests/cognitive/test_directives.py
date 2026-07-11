"""Directives must ship WITH the package (not under the shadowed data volume)."""
from __future__ import annotations

from pathlib import Path

from backend.cognitive.directives import (
    _DIRECTIVE_DIRS,
    load_directive,
    strategic_intelligence_cycle,
)


def test_strategic_intelligence_cycle_loads_nonempty():
    # Regression: the samus-data volume shadows /opt/samus/data, so the directive
    # must resolve from the packaged dir, not the old data/identity/directives.
    text = strategic_intelligence_cycle()
    assert text and "Strategic Daily Intelligence" in text


def test_packaged_dir_is_first_and_not_under_data_volume():
    packaged = _DIRECTIVE_DIRS[0]
    assert packaged.name == "directives_data"
    # the packaged path must live under backend/ (never shadowed by /opt/samus/data)
    assert "backend" in packaged.parts and (packaged / "strategic_intelligence_cycle.md").is_file()


def test_missing_directive_returns_empty():
    assert load_directive("no_such_directive_xyz") == ""
