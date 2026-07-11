"""R1 — AuditLedger O(1) cached tip regression.

Before the fix ``record()`` did a full-file O(n) scan (``_scan_tip_hash`` +
``_scan_next_seq``) on EVERY append, making a busy ledger O(n^2). The fix
caches the tip in memory (seeded from a reverse-seek tail read at open) and
updates it on append. These tests assert:
  * seq + prev_hash chaining stays correct under the cache;
  * a freshly constructed ledger over an existing file seeds its tip from the
    on-disk tail (so a new process continues the chain);
  * record() no longer scans the whole file per append.
"""

from __future__ import annotations

from pathlib import Path

from backend.common import audit_ledger
from backend.common.audit_ledger import AuditLedger, _read_last_jsonl_record

_SECRET = b"r1-test-secret-key-32-bytes-pad!!"


def _ledger(tmp_path: Path) -> AuditLedger:
    return AuditLedger(tmp_path / "audit.jsonl", secret_key=_SECRET)


def test_cached_tip_chains_correctly(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    r1 = led.record("a", {"i": 1})
    r2 = led.record("b", {"i": 2})
    r3 = led.record("c", {"i": 3})
    assert [r1["seq"], r2["seq"], r3["seq"]] == [1, 2, 3]
    assert r2["prev_hash"] == r1["hmac"]
    assert r3["prev_hash"] == r2["hmac"]
    assert led.last_hash == r3["hmac"]
    # Chain still verifies end-to-end.
    assert led.verify().ok


def test_reopen_seeds_tip_from_disk_tail(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.record("a", {"i": 1})
    last = led.record("b", {"i": 2})

    # A fresh ledger over the same file (new process) must seed from the tail
    # and continue the chain — not restart at seq 1 / genesis.
    reopened = _ledger(tmp_path)
    assert reopened.last_hash == last["hmac"]
    nxt = reopened.record("c", {"i": 3})
    assert nxt["seq"] == 3
    assert nxt["prev_hash"] == last["hmac"]
    assert reopened.verify().ok


def test_record_does_not_full_scan_per_append(tmp_path: Path, monkeypatch) -> None:
    led = _ledger(tmp_path)
    led.record("seed", {})  # warm the file

    calls = {"n": 0}
    real_read = audit_ledger._read_jsonl

    def _counting_read(path):
        calls["n"] += 1
        return real_read(path)

    monkeypatch.setattr(audit_ledger, "_read_jsonl", _counting_read)
    for i in range(25):
        led.record("evt", {"i": i})
    # The O(1) path must not call the full-file scanner on the append hot path.
    assert calls["n"] == 0


def test_reverse_seek_tail_reader(tmp_path: Path) -> None:
    p = tmp_path / "j.jsonl"
    p.write_text('{"seq":1,"hmac":"a"}\n{"seq":2,"hmac":"b"}\n', encoding="utf-8")
    rec = _read_last_jsonl_record(p)
    assert rec is not None and rec["seq"] == 2 and rec["hmac"] == "b"
    # Missing file -> None.
    assert _read_last_jsonl_record(tmp_path / "nope.jsonl") is None
    # Single-line file -> that line.
    p2 = tmp_path / "single.jsonl"
    p2.write_text('{"seq":7,"hmac":"z"}\n', encoding="utf-8")
    assert _read_last_jsonl_record(p2)["seq"] == 7
