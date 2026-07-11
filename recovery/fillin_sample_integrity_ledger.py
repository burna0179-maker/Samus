"""Integrity ledger — hash-chained tamper-evident event store.

Canonical §6 data plane ledger schema with rotating epoch HMAC keys. Used by
governance, security, and self-heal subsystems to record auditable events
that must survive tampering attempts.

Target path: backend/standard/data/ledgers/integrity.py
Source recovery: forensic_ledger_chain_model.md (chats 26, 30)

Adaptations applied:
  - Concept→code primary fill (no recovery .py exists yet)
  - Canonical ledger schema (seq + ts + type + prev_hash + payload + hmac)
  - Canonical JSON encoding (sort_keys=True, separators=(",", ":"))
  - Rotating epoch key derivation (24h primary + 5min overlap)
  - Genesis hash = 64 zeros (per canonical §6)
  - Verify-at-boot fail-closed (LedgerTamperError per canonical exception catalog)
  - @mutation_scope state-only; append-only file write
  - Module-level singleton accessor
  - Async append/verify to match DataPlane protocol
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.core.configuration.settings import get_settings
from backend.core.infrastructure.filesystem import get_paths
from backend.core.mutation.scope import mutation_scope, MutationType
from backend.core.protocols import HealthReport, HealthStatus

__plane__ = "data"
__layer__ = "L5_data"

_log = logging.getLogger("samus.data.ledgers.integrity")

GENESIS_HASH = "0" * 64


class LedgerTamperError(RuntimeError):
    """Raised when chain verification detects a broken link.

    Per canonical §5 exception catalog.
    """
    def __init__(self, broken_seq: int, reason: str) -> None:
        super().__init__(f"Ledger tamper at seq={broken_seq}: {reason}")
        self.broken_seq = broken_seq
        self.reason = reason


@dataclass(frozen=True)
class LedgerEntry:
    """Canonical ledger entry per §6 data plane spec."""
    seq: int
    ts: float
    type: str
    prev_hash: str
    payload: dict
    hmac: str

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "type": self.type,
            "prev_hash": self.prev_hash,
            "payload": self.payload,
            "hmac": self.hmac,
        }

    def to_jsonl(self) -> str:
        return _canonical_dumps(self.to_dict()) + "\n"

    @classmethod
    def from_dict(cls, d: dict) -> LedgerEntry:
        return cls(
            seq=int(d["seq"]),
            ts=float(d["ts"]),
            type=str(d["type"]),
            prev_hash=str(d["prev_hash"]),
            payload=d.get("payload") or {},
            hmac=str(d["hmac"]),
        )


def _canonical_dumps(obj: Any) -> str:
    """Canonical JSON: sorted keys, no whitespace. Used for HMAC input."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _signing_input(entry_payload: dict, prev_hash: str) -> bytes:
    """HMAC input = canonical(entry_without_hmac) + prev_hash."""
    body = _canonical_dumps(entry_payload).encode("utf-8")
    return body + prev_hash.encode("utf-8")


def _entry_hash(entry_dict: dict) -> str:
    """SHA-256 of canonical entry — used as `prev_hash` for next entry."""
    return hashlib.sha256(_canonical_dumps(entry_dict).encode("utf-8")).hexdigest()


def _derive_epoch_key(secret: bytes, epoch: int) -> bytes:
    """24h rotating epoch key: sha256(secret || epoch)."""
    return hashlib.sha256(secret + str(epoch).encode("utf-8")).digest()


@mutation_scope(
    state_paths=("data/integrity_ledger/**",),
    mutation_types={MutationType.STATE},
)
class IntegrityLedger:
    """Append-only hash-chained ledger with rotating HMAC.

    Threading: file appends use a lock; chain verification is read-only.
    Storage: JSONL at `data/integrity_ledger/ledger.jsonl`.

    Settings driven:
        sn_ledger_hmac_key_rotation_sec — epoch length (default 86400)
        sn_ledger_hmac_overlap_sec      — verification overlap window (default 300)
        sn_ledger_verify_at_boot        — fail-closed boot check (default True)
        sn_ledger_verify_max_entries    — last-N entries to verify (default 100)
    """

    plane_name = "data:ledgers:integrity"

    def __init__(self, secret: bytes | None = None, path: Path | None = None) -> None:
        cfg = get_settings()
        self._rotation_sec = int(getattr(cfg, "sn_ledger_hmac_key_rotation_sec", 86400))
        self._overlap_sec = int(getattr(cfg, "sn_ledger_hmac_overlap_sec", 300))
        self._verify_at_boot = bool(getattr(cfg, "sn_ledger_verify_at_boot", True))
        self._verify_max = int(getattr(cfg, "sn_ledger_verify_max_entries", 100))

        if secret is None:
            secret_env = os.environ.get("SN_LEDGER_HMAC_SECRET", "samus-default-not-for-prod")
            secret = secret_env.encode("utf-8")
        self._secret = secret

        if path is None:
            path = get_paths().data / "integrity_ledger" / "ledger.jsonl"
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

        self._lock = asyncio.Lock()
        self._tip_seq: int = 0
        self._tip_hash: str = GENESIS_HASH
        self._load_tip()

        if self._verify_at_boot:
            result = self.verify_chain(max_entries=self._verify_max)
            if not result["ok"]:
                raise LedgerTamperError(
                    broken_seq=result["broken_seq"],
                    reason=result["reason"],
                )

    def _load_tip(self) -> None:
        """Read tip seq + hash from file. O(file_size) but only at startup."""
        if self.path.stat().st_size == 0:
            self._tip_seq = 0
            self._tip_hash = GENESIS_HASH
            return
        last_line = ""
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last_line = line
        if last_line:
            entry = LedgerEntry.from_dict(json.loads(last_line))
            self._tip_seq = entry.seq
            self._tip_hash = _entry_hash({
                "seq": entry.seq, "ts": entry.ts, "type": entry.type,
                "prev_hash": entry.prev_hash, "payload": entry.payload,
                "hmac": entry.hmac,
            })

    async def append(self, event_type: str, payload: dict) -> LedgerEntry:
        """Append signed entry. Async-safe via lock."""
        async with self._lock:
            seq = self._tip_seq + 1
            ts = time.time()
            epoch = int(ts // self._rotation_sec)
            key = _derive_epoch_key(self._secret, epoch)

            entry_payload = {
                "seq": seq, "ts": ts, "type": event_type,
                "prev_hash": self._tip_hash, "payload": payload,
            }
            sig = hmac.new(
                key, _signing_input(entry_payload, self._tip_hash),
                hashlib.sha256,
            ).hexdigest()

            entry = LedgerEntry(
                seq=seq, ts=ts, type=event_type,
                prev_hash=self._tip_hash, payload=payload, hmac=sig,
            )

            # Atomic append with fsync
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(entry.to_jsonl())
                fh.flush()
                os.fsync(fh.fileno())

            self._tip_seq = seq
            self._tip_hash = _entry_hash(entry.to_dict())
            return entry

    def verify_chain(self, max_entries: int = 100) -> dict:
        """Walk from genesis to tip (or last `max_entries`). Verify each link.

        Returns: {"ok": bool, "broken_seq": int | None, "reason": str}
        """
        if self.path.stat().st_size == 0:
            return {"ok": True, "broken_seq": None, "reason": "empty_ledger"}

        prev_hash = GENESIS_HASH
        prev_seq = 0
        verified = 0
        all_lines = self.path.read_text(encoding="utf-8").splitlines()
        if max_entries > 0:
            all_lines = all_lines[-max_entries:]

        for line_no, line in enumerate(all_lines, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = LedgerEntry.from_dict(json.loads(line))
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                return {
                    "ok": False, "broken_seq": prev_seq + 1,
                    "reason": f"json_parse:{exc}",
                }

            if line_no == 1 and entry.prev_hash != prev_hash and len(all_lines) == self._verify_max:
                # Tail-only verify; seed prev_hash from this entry's prev_hash
                prev_hash = entry.prev_hash
            elif entry.prev_hash != prev_hash:
                return {
                    "ok": False, "broken_seq": entry.seq,
                    "reason": f"prev_hash_mismatch: expected={prev_hash[:16]}, got={entry.prev_hash[:16]}",
                }

            # Re-derive HMAC under entry's epoch
            epoch = int(entry.ts // self._rotation_sec)
            primary_key = _derive_epoch_key(self._secret, epoch)
            primary_sig = hmac.new(
                primary_key,
                _signing_input({
                    "seq": entry.seq, "ts": entry.ts, "type": entry.type,
                    "prev_hash": entry.prev_hash, "payload": entry.payload,
                }, entry.prev_hash),
                hashlib.sha256,
            ).hexdigest()

            sig_ok = hmac.compare_digest(primary_sig, entry.hmac)
            # Check overlap window — entry might have been signed under prev epoch
            if not sig_ok:
                overlap_key = _derive_epoch_key(self._secret, epoch - 1)
                overlap_sig = hmac.new(
                    overlap_key,
                    _signing_input({
                        "seq": entry.seq, "ts": entry.ts, "type": entry.type,
                        "prev_hash": entry.prev_hash, "payload": entry.payload,
                    }, entry.prev_hash),
                    hashlib.sha256,
                ).hexdigest()
                sig_ok = hmac.compare_digest(overlap_sig, entry.hmac)

            if not sig_ok:
                return {
                    "ok": False, "broken_seq": entry.seq,
                    "reason": "hmac_mismatch",
                }

            prev_hash = _entry_hash(entry.to_dict())
            prev_seq = entry.seq
            verified += 1

        return {"ok": True, "broken_seq": None, "reason": "ok", "verified": verified}

    async def query(self, event_type: str | None = None, limit: int = 100) -> list[dict]:
        """Read tail-N entries, optionally filtered by event type."""
        all_lines = self.path.read_text(encoding="utf-8").splitlines()
        results: list[dict] = []
        for line in reversed(all_lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry_dict = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event_type is None or entry_dict.get("type") == event_type:
                results.append(entry_dict)
            if len(results) >= limit:
                break
        return list(reversed(results))

    def health(self) -> HealthReport:
        try:
            result = self.verify_chain(max_entries=10)
            if result["ok"]:
                return HealthReport(
                    status=HealthStatus.OK,
                    detail=f"tip_seq={self._tip_seq}, verified=10",
                    metrics={"tip_seq": float(self._tip_seq), "verified": 10.0},
                )
            return HealthReport(
                status=HealthStatus.CRITICAL,
                detail=f"chain_break at seq={result['broken_seq']}: {result['reason']}",
            )
        except Exception as exc:
            return HealthReport(
                status=HealthStatus.CRITICAL,
                detail=f"verify_failed: {type(exc).__name__}",
            )


_instance: IntegrityLedger | None = None


def get_integrity_ledger() -> IntegrityLedger:
    """Module-level singleton accessor — matches canonical pattern."""
    global _instance
    if _instance is None:
        _instance = IntegrityLedger()
    return _instance
