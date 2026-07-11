"""needs_warm_path divert + list + promote — JSON-fallback path."""
from __future__ import annotations

import json
import os

import pytest

from backend.crm import needs_warm_path


@pytest.fixture()
def isolated_artifact_root(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    # Force JSON path: empty table name.
    monkeypatch.setenv("DDB_NEEDS_WARM_PATH_TABLE", "")
    # Ensure get_settings re-reads.
    from backend.common import config as cfg
    cfg.get_settings.cache_clear()  # type: ignore[attr-defined]
    yield tmp_path
    cfg.get_settings.cache_clear()  # type: ignore[attr-defined]


class _Prospect:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def model_dump(self):  # mimic pydantic
        return {k: v for k, v in self.__dict__.items()}


def test_divert_writes_json_when_no_ddb(isolated_artifact_root):
    p = _Prospect(prospect_id="p1", company="Acme", email="a@b.com",
                  phone="555-0100", city="Yuba City", industry="hvac")
    rec = needs_warm_path.divert("p1", prospect=p)
    assert rec.prospect_id == "p1"
    assert rec.status == "pending"
    raw = json.loads(open(os.path.join(isolated_artifact_root, "needs_warm_path.json"),
                          encoding="utf-8").read())
    assert len(raw) == 1
    assert raw[0]["prospect_id"] == "p1"
    assert raw[0]["company"] == "Acme"


def test_divert_dedupes_by_prospect_id(isolated_artifact_root):
    p = _Prospect(prospect_id="p1", company="Acme")
    needs_warm_path.divert("p1", prospect=p)
    needs_warm_path.divert("p1", prospect=p)
    raw = json.loads(open(os.path.join(isolated_artifact_root, "needs_warm_path.json"),
                          encoding="utf-8").read())
    assert len(raw) == 1


def test_divert_refuses_empty_prospect_id(isolated_artifact_root):
    with pytest.raises(needs_warm_path.NeedsWarmPathPersistError):
        needs_warm_path.divert("", prospect=_Prospect(prospect_id=""))


def test_list_pending_returns_diverted(isolated_artifact_root):
    needs_warm_path.divert("p1", prospect=_Prospect(prospect_id="p1", company="A"))
    needs_warm_path.divert("p2", prospect=_Prospect(prospect_id="p2", company="B"))
    items = needs_warm_path.list_pending()
    assert {i.prospect_id for i in items} == {"p1", "p2"}


def test_promote_marks_record_and_carries_signal(isolated_artifact_root):
    needs_warm_path.divert("p1", prospect=_Prospect(prospect_id="p1"))
    rec = needs_warm_path.promote(
        "p1",
        signal_kind="rfp",
        signal_source="https://example.gov/rfp/123",
        operator_id="alex",
    )
    assert rec.status == "promoted"
    assert rec.promoted_signal_kind == "rfp"
    # And list_pending excludes it.
    assert all(i.prospect_id != "p1" for i in needs_warm_path.list_pending())


def test_promote_missing_record_raises(isolated_artifact_root):
    with pytest.raises(needs_warm_path.NeedsWarmPathPersistError):
        needs_warm_path.promote(
            "ghost", signal_kind="rfp",
            signal_source="x", operator_id="alex",
        )
