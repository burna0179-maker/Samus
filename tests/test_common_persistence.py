"""JsonlLedger — append + tail round trip."""

from __future__ import annotations


def test_jsonl_ledger_append_and_tail(tmp_path):
    from backend.common.persistence import JsonlLedger

    ledger = JsonlLedger(tmp_path / "audit.jsonl")
    ledger.append({"event_id": "e1", "msg": "first"})
    ledger.append({"event_id": "e2", "msg": "second"})
    ledger.append({"event_id": "e3", "msg": "third"})

    tail = ledger.tail(limit=2)
    assert len(tail) == 2
    assert tail[0]["event_id"] == "e2"
    assert tail[1]["event_id"] == "e3"


def test_jsonl_ledger_tail_empty_when_no_file(tmp_path):
    from backend.common.persistence import JsonlLedger

    ledger = JsonlLedger(tmp_path / "never_written.jsonl")
    assert ledger.tail() == []


def test_jsonl_ledger_unicode_preserved(tmp_path):
    from backend.common.persistence import JsonlLedger

    ledger = JsonlLedger(tmp_path / "ledger.jsonl")
    ledger.append({"msg": "café — résumé"})
    out = ledger.tail()
    assert out[0]["msg"] == "café — résumé"


# ---------------------------------------------------------------------------
# rotate_by_age — regression for the shadowed duplicate definition. A second
# rotate_by_age once silently overrode the first; the survivor hardcoded the
# 'ts' field and rejected a ts_field= kwarg, so finance/webhook.py's
# rotate(..., ts_field="received_at") raised TypeError (swallowed) and Stripe
# event-log rotation became a no-op. These pin the surviving signature.
# ---------------------------------------------------------------------------


def _iso(delta_days: int = 0):
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(days=delta_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_rotate_by_age_honours_custom_ts_field(tmp_path):
    """Called exactly as finance/webhook.py does: positional hours + ts_field
    kwarg on a row keyed by 'received_at' (not 'ts')."""
    from backend.common.persistence import JsonlLedger

    path = tmp_path / "stripe_events.jsonl"
    ledger = JsonlLedger(path)
    ledger.append({"event_id": "old", "received_at": _iso(-40)})
    ledger.append({"event_id": "new", "received_at": _iso(0)})

    archived = ledger.rotate_by_age(24 * 30, ts_field="received_at")

    assert archived == 1
    assert {r["event_id"] for r in ledger.scan()} == {"new"}
    # Aged row is archived to the sibling, not destroyed.
    archive_path = path.with_name("stripe_events.archive.jsonl")
    assert archive_path.exists()
    assert "old" in archive_path.read_text(encoding="utf-8")


def test_rotate_by_age_defaults_to_ts_field(tmp_path):
    """The worker/voice callers rely on the default ts_field staying 'ts'."""
    from backend.common.persistence import JsonlLedger

    ledger = JsonlLedger(tmp_path / "audit.jsonl")
    ledger.append({"id": "old", "ts": _iso(-40)})
    ledger.append({"id": "new", "ts": _iso(0)})

    assert ledger.rotate_by_age(max_age_hours=24 * 30) == 1
    assert {r["id"] for r in ledger.scan()} == {"new"}


def test_rotate_by_age_keeps_records_without_parseable_ts(tmp_path):
    """Missing/garbled timestamps mean unknown age -> kept (never archived)."""
    from backend.common.persistence import JsonlLedger

    ledger = JsonlLedger(tmp_path / "audit.jsonl")
    ledger.append({"id": "no_ts"})
    ledger.append({"id": "bad_ts", "ts": "not-a-date"})

    assert ledger.rotate_by_age(max_age_hours=1) == 0
    assert {r["id"] for r in ledger.scan()} == {"no_ts", "bad_ts"}
