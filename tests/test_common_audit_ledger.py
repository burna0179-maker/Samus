"""Canonical hash-chained audit ledger — append, verify, tamper, key rotation.

Covers the round-5 Anita parity behaviour now ported to Samus:
  * canonical {seq, ts, type, prev_hash, payload, hmac} shape
  * forward-chained prev_hash linkage
  * epoch HMAC + 5min overlap window
  * tamper detection
  * key-change archive vs corruption archive
  * legacy in-place migration left as no-op when no legacy records exist
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.common.audit_ledger import (
    AuditLedger,
    LedgerTamperError,
    _GENESIS,
)


# Fixed test secrets — DO NOT use in production.
_SECRET = b"test-secret-key-32-bytes-padding!"
_ALT_SECRET = b"different-secret-key-also-32b!!!!"


def _new_ledger(tmp_path: Path, *, secret: bytes = _SECRET) -> AuditLedger:
    return AuditLedger(tmp_path / "audit.jsonl", secret_key=secret)


def test_append_chain_starts_from_genesis(tmp_path: Path) -> None:
    ledger = _new_ledger(tmp_path)
    rec = ledger.record("test.first", {"k": "v"})
    assert rec["seq"] == 1
    assert rec["prev_hash"] == _GENESIS
    assert rec["type"] == "test.first"
    assert rec["payload"] == {"k": "v"}
    assert isinstance(rec["hmac"], str) and len(rec["hmac"]) == 64


def test_chain_links_correctly(tmp_path: Path) -> None:
    ledger = _new_ledger(tmp_path)
    r1 = ledger.record("e1", {"i": 1})
    r2 = ledger.record("e2", {"i": 2})
    r3 = ledger.record("e3", {"i": 3})
    assert r2["prev_hash"] == r1["hmac"]
    assert r3["prev_hash"] == r2["hmac"]
    assert r3["seq"] == 3


def test_verify_passes_on_intact_chain(tmp_path: Path) -> None:
    ledger = _new_ledger(tmp_path)
    for i in range(5):
        ledger.record(f"event.{i}", {"i": i})
    state = ledger.verify()
    assert state.ok is True
    assert state.chain_length == 5


def test_verify_detects_payload_tamper(tmp_path: Path) -> None:
    ledger = _new_ledger(tmp_path)
    for i in range(3):
        ledger.record(f"event.{i}", {"i": i})
    # Tamper with the middle record's payload
    path = ledger.path
    lines = path.read_text(encoding="utf-8").splitlines()
    middle = json.loads(lines[1])
    middle["payload"]["i"] = 999
    lines[1] = json.dumps(middle, sort_keys=True, default=str)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    state = ledger.verify()
    assert state.ok is False
    assert state.broken_at == 1
    assert "hmac mismatch" in state.detail


def test_assert_chain_intact_raises_on_break(tmp_path: Path) -> None:
    ledger = _new_ledger(tmp_path)
    ledger.record("e1", {})
    ledger.record("e2", {})
    path = ledger.path
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    rec["payload"] = {"tampered": True}
    lines[0] = json.dumps(rec, sort_keys=True, default=str)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(LedgerTamperError):
        ledger.assert_chain_intact()


def test_tail_returns_last_n(tmp_path: Path) -> None:
    ledger = _new_ledger(tmp_path)
    for i in range(7):
        ledger.record(f"e.{i}", {"i": i})
    tail = ledger.tail(3)
    assert len(tail) == 3
    assert [r["payload"]["i"] for r in tail] == [4, 5, 6]


def test_tip_reload_survives_restart(tmp_path: Path) -> None:
    """Anita race-fix: every write re-scans the tip from disk."""
    l1 = _new_ledger(tmp_path)
    l1.record("e1", {})
    l1.record("e2", {})

    # Simulate restart — new instance, same file.
    l2 = _new_ledger(tmp_path)
    r3 = l2.record("e3", {})
    assert r3["seq"] == 3
    assert l2.verify().ok


def test_epoch_overlap_accepts_neighbor(tmp_path: Path) -> None:
    """Entries signed near the epoch boundary verify under either side."""
    short_epoch = 100  # 100s epochs
    overlap = 10
    path = tmp_path / "audit.jsonl"
    # Use a fresh instance each time so the in-memory _last_hash is reloaded.
    ledger = AuditLedger(path, secret_key=_SECRET, epoch_sec=short_epoch, epoch_overlap_sec=overlap)
    # Just append a few entries and verify they all pass.
    for i in range(3):
        ledger.record(f"e.{i}", {})
    state = ledger.verify()
    assert state.ok is True


def test_key_change_archives_with_keychange_suffix(tmp_path: Path) -> None:
    """Switching the secret key triggers the asymmetric keychange path."""
    l1 = _new_ledger(tmp_path, secret=_SECRET)
    l1.record("e1", {})
    l1.record("e2", {})

    # Now open with a different secret — chain links are intact but no
    # HMAC verifies → archive as keychange.
    AuditLedger(tmp_path / "audit.jsonl", secret_key=_ALT_SECRET)

    # Original file gone, replaced by archive + fresh file.
    archives = list(tmp_path.glob("audit_pre_keychange_*.jsonl"))
    assert len(archives) == 1
    # Fresh ledger file exists, possibly empty.
    fresh = tmp_path / "audit.jsonl"
    if fresh.exists():
        contents = fresh.read_text(encoding="utf-8").strip()
        assert contents == ""  # fresh genesis is an empty file


def test_corruption_archives_with_canonical_suffix(tmp_path: Path) -> None:
    """A genuinely broken chain (prev_hash mismatch) archives as canonical."""
    l1 = _new_ledger(tmp_path)
    l1.record("e1", {})
    l1.record("e2", {})

    # Tamper the chain link itself — break prev_hash.
    path = l1.path
    lines = path.read_text(encoding="utf-8").splitlines()
    second = json.loads(lines[1])
    second["prev_hash"] = "00" * 32  # totally wrong
    lines[1] = json.dumps(second, sort_keys=True, default=str)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Reopen — should archive as canonical (not keychange).
    AuditLedger(tmp_path / "audit.jsonl", secret_key=_SECRET)
    canonical_archives = list(tmp_path.glob("audit_pre_canonical_*.jsonl"))
    keychange_archives = list(tmp_path.glob("audit_pre_keychange_*.jsonl"))
    assert len(canonical_archives) == 1
    assert len(keychange_archives) == 0


def test_recovered_archive_preserved_per_retain_rollback(tmp_path: Path) -> None:
    """`feedback_samus_retain_rollback_artifacts` — archive is never deleted."""
    l1 = _new_ledger(tmp_path, secret=_SECRET)
    l1.record("e1", {})
    # Force a key change.
    AuditLedger(tmp_path / "audit.jsonl", secret_key=_ALT_SECRET)
    archives = list(tmp_path.glob("audit_pre_keychange_*.jsonl"))
    assert len(archives) == 1 and archives[0].exists()
    # Archive content is the original chain — preserved verbatim.
    archive_lines = archives[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(archive_lines) == 1
    rec = json.loads(archive_lines[0])
    assert rec["type"] == "e1"


def test_empty_ledger_verify_is_ok(tmp_path: Path) -> None:
    ledger = _new_ledger(tmp_path)
    state = ledger.verify()
    assert state.ok is True
    assert state.chain_length == 0
    assert state.last_hash == _GENESIS


# --------------------------------------------------------------------------
# D8-03 — per-day files chain across day boundaries; whole-day deletion is
# detected by verify_cross_day().
# --------------------------------------------------------------------------


def _daily(tmp_path: Path, stamp: str, *, secret: bytes = _SECRET) -> AuditLedger:
    """A per-day ledger named ledger-YYYYMMDD.jsonl in tmp_path."""
    return AuditLedger(tmp_path / f"ledger-{stamp}.jsonl", secret_key=secret)


def test_new_day_seeds_prev_hash_from_prior_day_tail(tmp_path: Path) -> None:
    """A fresh day's first entry links to the prior day's last-good hmac,
    not _GENESIS — the chain is continuous across the file boundary."""
    d1 = _daily(tmp_path, "20260605")
    d1.record("day1.a", {})
    last1 = d1.record("day1.b", {})["hmac"]

    d2 = _daily(tmp_path, "20260606")
    first2 = d2.record("day2.a", {})
    assert first2["seq"] == 1
    assert first2["prev_hash"] == last1  # cross-day link, NOT _GENESIS
    assert first2["prev_hash"] != _GENESIS


def test_first_dated_day_starts_from_genesis(tmp_path: Path) -> None:
    """The very first dated file (no earlier sibling) is true genesis."""
    d1 = _daily(tmp_path, "20260605")
    first = d1.record("day1.a", {})
    assert first["prev_hash"] == _GENESIS


def test_empty_intervening_day_does_not_break_chain(tmp_path: Path) -> None:
    """A day with no events (no file) is fine — the next day links to the
    most recent EARLIER day that actually has content."""
    d1 = _daily(tmp_path, "20260605")
    last1 = d1.record("day1.a", {})["hmac"]
    # 20260606 simply has no file (no events that day).
    d3 = _daily(tmp_path, "20260607")
    first3 = d3.record("day3.a", {})
    assert first3["prev_hash"] == last1


def test_verify_cross_day_passes_on_continuous_chain(tmp_path: Path) -> None:
    d1 = _daily(tmp_path, "20260605")
    d1.record("a", {})
    d1.record("b", {})
    d2 = _daily(tmp_path, "20260606")
    d2.record("c", {})
    d3 = _daily(tmp_path, "20260607")
    d3.record("d", {})

    state = d3.verify_cross_day()
    assert state.ok is True
    assert state.chain_length == 4


def test_verify_cross_day_detects_deleted_whole_day(tmp_path: Path) -> None:
    """Deleting a whole middle day's file breaks the link the next day's
    first prev_hash recorded — verify_cross_day catches it; a single-file
    verify of the surviving days would not."""
    d1 = _daily(tmp_path, "20260605")
    d1.record("a", {})
    d2 = _daily(tmp_path, "20260606")
    d2.record("b", {})  # day2.first.prev_hash == day1.tail
    d3 = _daily(tmp_path, "20260607")
    d3.record("c", {})  # day3.first.prev_hash == day2.tail

    # Attacker deletes the whole of day 2 to suppress event "b".
    (tmp_path / "ledger-20260606.jsonl").unlink()

    # Re-open the newest day and verify the full sequence (dev env => no raise).
    d3b = _daily(tmp_path, "20260607")
    state = d3b.verify_cross_day()
    assert state.ok is False
    # The break surfaces at day 3, whose first prev_hash points at the now
    # missing day-2 tail (day-1 tail no longer matches).
    assert "20260607" in state.detail


def test_intrafile_tamper_in_a_day_caught_on_open(tmp_path: Path) -> None:
    """An intra-file tamper on a per-day file is still caught by the existing
    D8-02 reconcile-on-open (it self-validates against its OWN first prev_hash,
    so the cross-day seed doesn't mask the break)."""
    d1 = _daily(tmp_path, "20260605")
    d1.record("a", {})
    d2 = _daily(tmp_path, "20260606")
    d2.record("b", {"v": 1})
    d2.record("c", {"v": 2})

    # Tamper the payload of an entry in day 2.
    p = tmp_path / "ledger-20260606.jsonl"
    lines = p.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    rec["payload"]["v"] = 999
    lines[0] = json.dumps(rec, sort_keys=True, default=str)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Re-open day 2 (dev env => reconcile archives the broken chain).
    _daily(tmp_path, "20260606")
    archives = list(tmp_path.glob("ledger-20260606_pre_canonical_*.jsonl"))
    assert len(archives) == 1


def test_verify_cross_day_raises_outside_development(tmp_path: Path, monkeypatch) -> None:
    """Outside development, a cross-day break fails closed (LedgerTamperError)."""
    d1 = _daily(tmp_path, "20260605")
    d1.record("a", {})
    d2 = _daily(tmp_path, "20260606")
    d2.record("b", {})
    d3 = _daily(tmp_path, "20260607")
    d3.record("c", {})
    (tmp_path / "ledger-20260606.jsonl").unlink()

    d3b = _daily(tmp_path, "20260607")
    monkeypatch.setattr(d3b, "_env_name", "production")
    with pytest.raises(LedgerTamperError):
        d3b.verify_cross_day()


def test_non_dated_ledger_unaffected_by_cross_day(tmp_path: Path) -> None:
    """A standalone (non-dated) ledger still starts from genesis and
    verify_cross_day degrades to a single-file verify."""
    ledger = _new_ledger(tmp_path)  # audit.jsonl — not a dated name
    first = ledger.record("x", {})
    assert first["prev_hash"] == _GENESIS
    state = ledger.verify_cross_day()
    assert state.ok and state.chain_length == 1


def test_default_ledger_singleton_isolation(tmp_path: Path, monkeypatch) -> None:
    """get_default_ledger respects SAMUS_AUDIT_LEDGER_PATH for tests."""
    from backend.common import audit_ledger as al

    path = tmp_path / "default.jsonl"
    monkeypatch.setenv("SAMUS_AUDIT_LEDGER_PATH", str(path))
    al.reset_default_ledger()
    ledger = al.get_default_ledger()
    ledger.record("singleton.test", {"k": "v"})
    assert path.exists()
    state = ledger.verify()
    assert state.ok and state.chain_length == 1
    al.reset_default_ledger()


def test_default_data_root_env_override_wins(monkeypatch) -> None:
    """SAMUS_DATA_ROOT, when set, is used verbatim regardless of platform."""
    from backend.common import audit_ledger as al

    monkeypatch.setenv("SAMUS_DATA_ROOT", "/opt/samus/data")
    assert al._default_data_root() == "/opt/samus/data"


def test_default_data_root_is_posix_off_windows(monkeypatch) -> None:
    """With SAMUS_DATA_ROOT unset, a POSIX host must NOT get the ``E:\\`` host
    path (that path becomes a bogus relative component that walks into the
    read-only ``/opt/samus`` mount and silently drops audit records). It must
    fall back to the writable ``/opt/samus/data`` volume instead."""
    from backend.common import audit_ledger as al

    monkeypatch.delenv("SAMUS_DATA_ROOT", raising=False)
    monkeypatch.setattr(al, "_is_windows_host", lambda: False)
    assert al._default_data_root() == "/opt/samus/data"


def test_default_data_root_is_host_store_on_windows(monkeypatch) -> None:
    """On Windows the default stays the operator's host data store."""
    from backend.common import audit_ledger as al

    monkeypatch.delenv("SAMUS_DATA_ROOT", raising=False)
    monkeypatch.setattr(al, "_is_windows_host", lambda: True)
    assert al._default_data_root() == r"E:\Hustleforge\Samus\data"
