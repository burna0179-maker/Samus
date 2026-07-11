"""Append-only ledger persistence — pluggable jsonl / Firestore backend.

``JsonlLedger`` is the original local backend: an append-only JSONL file with
thread-safe writes + tail reads (per doc §3.9). It is used by audit ledgers,
the DLQ, replay archives, the finance Stripe-event log and the upsell queue.

On GCP Cloud Run the container filesystem is ephemeral + per-instance, so a
JSONL ledger on a bind mount silently corrupts on any scale-to-zero. The
:class:`~backend.common.firestore_ledger.FirestoreLedger` sibling backend is
the durable cross-instance replacement.

Callers that need backend-portability go through :func:`open_ledger`, which
selects the backend from the ``SAMUS_LEDGER_BACKEND`` env var (``jsonl`` —
the default — or ``firestore``). Both backends honour the :class:`Ledger`
contract: ``append`` / ``scan`` / ``tail`` / ``claim``.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Ledger(Protocol):
    """The append-only ledger contract shared by every backend.

    * ``append`` — write one record (append-only; never updates a prior row).
    * ``scan``   — every record, oldest first.
    * ``tail``   — the last ``limit`` records, oldest first.
    * ``claim``  — atomically claim a key; True iff this caller won. The
                   cross-instance idempotency primitive.
    """

    def append(self, record: dict[str, Any]) -> None: ...
    def scan(self) -> list[dict[str, Any]]: ...
    def tail(self, limit: int = 50) -> list[dict[str, Any]]: ...
    def claim(self, key: str) -> bool: ...
    # Reservation/terminal-marker support (PATCH-RACE-11). ``claim_age_seconds``
    # reports how long an existing reservation has been held (``None`` =
    # unclaimed); ``reclaim_expired`` atomically takes over a reservation whose
    # owner the caller has judged expired. Together they let a claim-then-crash
    # be reprocessed instead of permanently dropped.
    def claim_age_seconds(self, key: str) -> float | None: ...
    def reclaim_expired(self, key: str) -> bool: ...


class JsonlLedger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the last ``limit`` records, oldest-first.

        Seeks from EOF in fixed-size blocks instead of reading the whole file
        into memory — important for audit ledgers that accumulate millions of
        lines across long-running workers. The previous ``readlines()`` whole-
        file read was the dominant resident-set offender on hot ledgers.
        """
        if not self.path.exists():
            return []
        if limit <= 0:
            return []
        block_size = 8192
        lines: list[bytes] = []
        with self.path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            remaining = fh.tell()
            buf = b""
            # Read backwards from EOF until we have limit+1 newlines (so the
            # first partial line — which may be in the middle of a record —
            # can be safely discarded) or hit BOF.
            while remaining > 0 and buf.count(b"\n") <= limit:
                read = min(block_size, remaining)
                remaining -= read
                fh.seek(remaining)
                buf = fh.read(read) + buf
            # Split, drop any trailing empty (file ends with newline), keep
            # the last ``limit`` candidates.
            split_lines = buf.splitlines()
            if remaining > 0 and len(split_lines) > limit:
                # Drop the first chunk — it may be a partial record because
                # we did not read to start of file.
                split_lines = split_lines[1:]
            lines = split_lines[-limit:]
        out: list[dict[str, Any]] = []
        for raw_bytes in lines:
            raw = raw_bytes.decode("utf-8", errors="replace").strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return out

    def rotate_by_age(self, max_age_hours: float = 24 * 30, ts_field: str = "ts") -> int:
        """Archive records older than ``max_age_hours`` into a sibling file.

        The active ledger is rewritten to contain only records whose
        ``ts_field`` timestamp is within the retention window; older records
        are appended to ``<stem>.archive.jsonl`` next to the active file.
        Records lacking a parseable ``ts_field`` are treated as fresh and kept
        in the active log (never silently archived — cognition-preserving).

        ``ts_field`` selects which record field carries the timestamp. It
        defaults to ``"ts"`` (the audit/voice ledgers), but finance's Stripe
        event log stamps ``"received_at"`` instead, so callers pass it
        explicitly. The value may be epoch seconds (int/float) or an ISO-8601
        string with a ``Z`` or ``+00:00`` suffix.

        Returns the count of records archived. No-op (returns 0) when the
        ledger does not exist or holds no aged records. Safe to call on a
        daily cadence from a worker's poll loop.

        Held under ``self._lock`` so an in-flight :meth:`append` never sees
        the file mid-rewrite.
        """
        if max_age_hours <= 0 or not self.path.exists():
            return 0
        from datetime import datetime, timezone

        cutoff = time.time() - max_age_hours * 3600
        archive_path = self.path.with_name(f"{self.path.stem}.archive.jsonl")
        kept: list[str] = []
        archived: list[str] = []

        def _ts_to_epoch(value: Any) -> float | None:
            if isinstance(value, (int, float)):
                return float(value)
            if not isinstance(value, str) or not value:
                return None
            try:
                # iso_now() produces ISO-8601 with a trailing Z or +00:00.
                normalized = value.replace("Z", "+00:00")
                dt = datetime.fromisoformat(normalized)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                return None

        with self._lock:
            with self.path.open("r", encoding="utf-8") as fh:
                for raw in fh:
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        record = json.loads(stripped)
                    except json.JSONDecodeError:
                        # Preserve malformed lines — never silently drop.
                        kept.append(stripped)
                        continue
                    ts_epoch = _ts_to_epoch(record.get(ts_field))
                    if ts_epoch is not None and ts_epoch < cutoff:
                        archived.append(stripped)
                    else:
                        kept.append(stripped)
            if not archived:
                return 0
            # Append-then-replace: write archive first so a crash leaves a
            # superset (records present in both files), never a deficit.
            with archive_path.open("a", encoding="utf-8") as ah:
                for line in archived:
                    ah.write(line + "\n")
            tmp_path = self.path.with_name(self.path.name + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as tf:
                for line in kept:
                    tf.write(line + "\n")
            os.replace(tmp_path, self.path)
        return len(archived)

    def scan(self) -> list[dict[str, Any]]:
        """Every record in the log, oldest first. Malformed lines are skipped.

        The full-log read the finance idempotency + upsell-fold paths need;
        :meth:`tail` is the bounded variant.
        """
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
        out: list[dict[str, Any]] = []
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return out

    def claim(self, key: str) -> bool:
        """Atomically claim ``key`` before its side effects run.

        Returns True if this caller won the claim, False if ``key`` was
        already claimed. Uses ``os.open(..., O_CREAT | O_EXCL)`` — the OS
        makes exactly one creator win even under concurrent callers on one
        host. (Cross-*instance* atomicity needs the Firestore backend; on a
        single Docker host this is the real guarantee.)

        Claim markers live in a ``<stem>_claims`` directory beside the log so
        they share its volume + lifecycle. A claim-store ``OSError`` fails
        **OPEN** (returns True) so a disk hiccup can never drop a genuine
        event — the caller's scan-based duplicate check still catches
        sequential retries.
        """
        if not key:
            return True
        claim_dir = self.path.parent / f"{self.path.stem}_claims"
        safe = hashlib.sha256(key.encode("utf-8")).hexdigest()
        claim_path = claim_dir / f"{safe}.claim"
        try:
            claim_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False  # already claimed by a concurrent / prior caller
        except OSError:
            return True  # fail OPEN — see docstring
        try:
            os.write(fd, str(time.time()).encode("utf-8"))
        finally:
            os.close(fd)
        return True

    def claim_age_seconds(self, key: str) -> float | None:
        """Age (seconds) of an EXISTING claim marker for ``key``, else ``None``.

        Used by the reservation/terminal-marker scheme (PATCH-RACE-11): a claim
        that won but never wrote a terminal record may belong to a crashed
        owner. The age (read from the marker's recorded claim timestamp, falling
        back to its filesystem mtime) lets the caller decide whether the
        reservation has expired and is reclaimable. ``None`` means no marker
        exists (the key is unclaimed). An unreadable/garbled marker returns its
        mtime age so a corrupt marker can still expire rather than wedging the
        event forever.
        """
        if not key:
            return None
        claim_dir = self.path.parent / f"{self.path.stem}_claims"
        safe = hashlib.sha256(key.encode("utf-8")).hexdigest()
        claim_path = claim_dir / f"{safe}.claim"
        try:
            raw = claim_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        now = time.time()
        try:
            claimed_at = float(raw)
        except ValueError:
            try:
                claimed_at = claim_path.stat().st_mtime
            except OSError:
                return None
        return max(0.0, now - claimed_at)

    def reclaim_expired(self, key: str) -> bool:
        """Atomically take over an EXPIRED reservation for ``key``.

        Rewrites the existing claim marker's timestamp to now under the same
        lock the writers use, so exactly one concurrent reclaimer wins the
        takeover (the others see a fresh timestamp on their next age check).
        Returns True if this caller took over the reservation, False if it
        could not (marker vanished, or another reclaimer already refreshed it).

        Only the caller's expiry policy decides *whether* to reclaim — this
        method just performs the atomic timestamp takeover. A claim-store
        ``OSError`` returns False (the normal ``claim``/scan guards still
        apply) so a disk hiccup never silently double-processes.
        """
        if not key:
            return False
        claim_dir = self.path.parent / f"{self.path.stem}_claims"
        safe = hashlib.sha256(key.encode("utf-8")).hexdigest()
        claim_path = claim_dir / f"{safe}.claim"
        with self._lock:
            # Re-confirm the marker still exists; if it vanished, fall back to a
            # fresh claim so we don't resurrect a deleted reservation here.
            if not claim_path.exists():
                return False
            try:
                claim_path.write_text(str(time.time()), encoding="utf-8")
            except OSError:
                return False
        return True


def ledger_backend() -> str:
    """The configured ledger backend: ``jsonl`` (default) or ``firestore``.

    Read fresh from ``SAMUS_LEDGER_BACKEND`` on every call so tests can
    monkeypatch the env mid-process.
    """
    return (os.getenv("SAMUS_LEDGER_BACKEND", "jsonl") or "jsonl").strip().lower()


def open_ledger(*, jsonl_path: str | Path, collection: str) -> Ledger:
    """Open a ledger using the backend selected by ``SAMUS_LEDGER_BACKEND``.

    ``jsonl_path`` is the on-disk path used by the ``jsonl`` backend;
    ``collection`` is the Firestore collection name used by the ``firestore``
    backend. A caller supplies both so the same call site works under either
    backend without branching.
    """
    backend = ledger_backend()
    if backend == "jsonl":
        return JsonlLedger(jsonl_path)
    if backend == "firestore":
        # Imported lazily — the jsonl path must never need google-cloud-firestore.
        from backend.common.firestore_ledger import FirestoreLedger

        return FirestoreLedger(collection)
    raise ValueError(f"unknown SAMUS_LEDGER_BACKEND={backend!r} (expected 'jsonl' or 'firestore')")


__all__ = ["Ledger", "JsonlLedger", "ledger_backend", "open_ledger"]
