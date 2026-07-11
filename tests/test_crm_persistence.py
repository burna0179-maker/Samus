"""CRM persistence — safe_scan filtered-pagination regression coverage.

Covers the DynamoDB Scan semantics bug where ``Limit`` caps the *scan window*,
not the match count, so a filtered query could return zero rows even when a
matching row existed just past the first window. See the note above
``_SCAN_PAGE_CAP`` in :mod:`backend.crm.persistence`.

The fake table here models real DynamoDB Scan: ``Limit`` caps items READ, the
``FilterExpression`` is applied to that window, and pagination runs through
``ExclusiveStartKey`` / ``LastEvaluatedKey``. (The shim in test_crm_service.py
applies the filter first, which is exactly why it never caught this bug.)
"""

from __future__ import annotations

from typing import Any

import backend.crm.persistence as p
from backend.crm.persistence import _SCAN_PAGE_CAP, safe_scan


class _DdbAccurateTable:
    """Fake DynamoDB table with real Scan ordering: Limit-then-filter, with
    ExclusiveStartKey / LastEvaluatedKey pagination over insertion order."""

    def __init__(self, rows: list[dict[str, Any]], page_size: int = 25):
        self._rows = list(rows)  # scan order == insertion order
        self._page_size = page_size

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        fe = kwargs.get("FilterExpression")
        names = kwargs.get("ExpressionAttributeNames", {}) or {}
        vals = kwargs.get("ExpressionAttributeValues", {}) or {}
        start = kwargs.get("ExclusiveStartKey")
        # DynamoDB caps a page at 1MB; emulate with a fixed window so the test
        # can drive multi-page pagination deterministically. An explicit Limit
        # (the unfiltered path) shrinks the window further, never grows it.
        window_size = min(self._page_size, int(kwargs.get("Limit", self._page_size)))

        begin = (start["_idx"] + 1) if start is not None else 0
        window = self._rows[begin : begin + window_size]

        # The filter is applied AFTER the window is read — as real DDB does.
        if fe:
            attr = names.get("#f")
            target = str(vals.get(":v", ""))
            items = [r for r in window if str(r.get(attr, "")) == target]
        else:
            items = list(window)

        resp: dict[str, Any] = {"Items": items}
        if begin + window_size < len(self._rows):
            resp["LastEvaluatedKey"] = {"_idx": begin + len(window) - 1}
        return resp


def test_list_conversations_finds_row_beyond_first_scan_window(monkeypatch):
    """A matching conversation past the first Limit-sized window is still found.

    Pre-fix, list_conversations applied DynamoDB's Limit to the scan window
    before the prospect_id filter, so a match at row 59 of 60 returned zero
    rows — the row existed but the query silently reported none.
    """
    rows = [{"conversation_id": f"cv_{i}", "prospect_id": "pr_other"} for i in range(59)]
    rows.append({"conversation_id": "cv_target", "prospect_id": "pr_target"})
    fake = _DdbAccurateTable(rows, page_size=25)
    monkeypatch.setattr(p, "_conversations_table", lambda: fake)

    from backend.crm.service import list_conversations

    out = list_conversations(prospect_id="pr_target", limit=50)

    assert out.count == 1
    assert out.conversations[0].conversation_id == "cv_target"
    assert out.ddb_error is None


def test_safe_scan_filtered_caps_at_limit_and_flags_truncated():
    """With more matches than `limit`, return exactly `limit` and flag truncated."""
    rows = [{"conversation_id": f"cv_{i}", "prospect_id": "pr_target"} for i in range(40)]
    fake = _DdbAccurateTable(rows, page_size=25)

    items, truncated, err = safe_scan(
        fake,
        limit=10,
        filter_expression="#f = :v",
        expression_attribute_values={":v": "pr_target"},
        expression_attribute_names={"#f": "prospect_id"},
    )
    assert err is None
    assert len(items) == 10
    assert truncated is True


def test_safe_scan_filtered_exhausts_table_when_no_match():
    """A filter matching nothing pages the whole table, returns [] untruncated."""
    rows = [{"conversation_id": f"cv_{i}", "prospect_id": "pr_other"} for i in range(60)]
    fake = _DdbAccurateTable(rows, page_size=25)

    items, truncated, err = safe_scan(
        fake,
        limit=50,
        filter_expression="#f = :v",
        expression_attribute_values={":v": "pr_missing"},
        expression_attribute_names={"#f": "prospect_id"},
    )
    assert items == []
    assert truncated is False
    assert err is None


def test_safe_scan_unfiltered_limit_is_result_count():
    """With no filter, `limit` maps straight onto the returned row count."""
    rows = [{"conversation_id": f"cv_{i}"} for i in range(60)]
    fake = _DdbAccurateTable(rows, page_size=25)

    items, truncated, err = safe_scan(fake, limit=10)
    assert len(items) == 10
    assert truncated is True  # 50 rows still unscanned
    assert err is None


def test_safe_scan_filtered_respects_page_cap():
    """A sparse filter over a table larger than the page cap stops bounded."""
    page_size = 25
    # One page more than the cap can scan, none matching — proves the loop is
    # bounded rather than walking an arbitrarily large table to the end.
    rows = [
        {"conversation_id": f"cv_{i}", "prospect_id": "pr_other"}
        for i in range(_SCAN_PAGE_CAP * page_size + page_size)
    ]
    fake = _DdbAccurateTable(rows, page_size=page_size)

    items, truncated, err = safe_scan(
        fake,
        limit=50,
        filter_expression="#f = :v",
        expression_attribute_values={":v": "pr_missing"},
        expression_attribute_names={"#f": "prospect_id"},
    )
    assert items == []
    assert truncated is True  # stopped at the cap, table not exhausted
    assert err is None


def test_safe_scan_filtered_returns_error_on_table_failure():
    """A boto / IAM failure mid-scan degrades to ([], False, error)."""

    class _BrokenTable:
        def scan(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("simulated AWS down")

    items, truncated, err = safe_scan(
        _BrokenTable(),
        limit=50,
        filter_expression="#f = :v",
        expression_attribute_values={":v": "pr_x"},
        expression_attribute_names={"#f": "prospect_id"},
    )
    assert items == []
    assert truncated is False
    assert err is not None and "ddb_scan_failed" in err


# ---------------------------------------------------------------------------
# safe_put float -> Decimal coercion. DynamoDB rejects native Python floats
# outright; Opportunity rows carry deal_size_usd / close_probability /
# won_amount_usd, so every Opportunity write would silently fail without this.
# ---------------------------------------------------------------------------


class _CapturingTable:
    """Fake table that records the Item handed to put_item."""

    def __init__(self) -> None:
        self.put_item_arg: Any = None

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        self.put_item_arg = kwargs.get("Item")
        return {}


def _contains_float(value: Any) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(_contains_float(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_float(v) for v in value)
    return False


def test_coerce_floats_converts_scalars_and_nested():
    from decimal import Decimal

    from backend.crm.persistence import _coerce_floats

    out = _coerce_floats(
        {
            "close_probability": 0.35,
            "deal_size_usd": 0.0,
            "nested": {"won_amount_usd": 149.0},
            "tags": [1.5, "x", {"p": 2.5}],
            "name": "Acme",
            "count": 7,
        }
    )
    assert out["close_probability"] == Decimal("0.35")
    assert out["deal_size_usd"] == Decimal("0.0")
    assert out["nested"]["won_amount_usd"] == Decimal("149.0")
    assert out["tags"][0] == Decimal("1.5")
    assert out["tags"][2]["p"] == Decimal("2.5")
    # non-floats pass through untouched
    assert out["name"] == "Acme"
    assert isinstance(out["count"], int) and out["count"] == 7


def test_safe_put_coerces_floats_before_put_item():
    """An Opportunity-shaped item with float fields persists -- safe_put must
    hand DynamoDB Decimals, never native floats."""
    from decimal import Decimal

    from backend.crm.persistence import safe_put

    table = _CapturingTable()
    ok, err = safe_put(
        table,
        {
            "opportunity_id": "op_x",
            "deal_size_usd": 0.0,
            "close_probability": 0.35,
            "won_amount_usd": 149.0,
        },
    )
    assert ok is True and err is None
    assert not _contains_float(table.put_item_arg)
    assert table.put_item_arg["close_probability"] == Decimal("0.35")


def test_safe_put_degrades_cleanly_on_table_failure():
    from backend.crm.persistence import safe_put

    class _BrokenTable:
        def put_item(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("simulated AWS down")

    ok, err = safe_put(_BrokenTable(), {"opportunity_id": "op_y"})
    assert ok is False
    assert err is not None and "ddb_put_failed" in err
