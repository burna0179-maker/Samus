"""Canonical hash-chained audit ledger (Hustleforge agent canonical §6).

Append-only JSONL file under ``<ledger_root>/audit/ledger-YYYYMMDD.jsonl``
(by default — caller controls path). Each entry references the previous
entry's HMAC via ``prev_hash``; the file is therefore tamper-evident —
modifying a line breaks the chain at that point.

Record schema (canonical, matches Anita's EvidenceLedger):

    {
        "seq":       <int>,
        "ts":        <float unix>,
        "type":      <event name string>,
        "prev_hash": <sha256 hex of previous entry's hmac; 64-zero for genesis>,
        "payload":   <dict>,
        "hmac":      <HMAC-SHA256 over canonical(seq,ts,type,prev_hash,payload)>
    }

The HMAC key rotates on a 24h epoch with a 5min overlap window
(``samus_ledger_epoch_sec`` + ``samus_ledger_epoch_overlap_sec`` in
:class:`Settings`). Per-epoch keys are derived via HKDF-SHA256
(RFC 5869) from a long-lived secret:

    epoch_key = HKDF-SHA256(
        ikm   = samus_ledger_secret_key (falls back to samus_shared_hmac_key),
        salt  = b"samus-ledger-v1",
        info  = f"epoch-{n}".encode(),
        length= 32,
    )

The split between ``samus_ledger_secret_key`` and
``samus_shared_hmac_key`` is deliberate: rotating the inter-service
HMAC key (replay protection, nonces, request signing) does NOT
invalidate the audit chain. When the dedicated ledger key is unset the
ledger transparently falls back to the shared HMAC for back-compat.

Verification accepts an entry if its hmac validates under the epoch
matching its ``ts`` OR either neighbouring epoch (accommodates clock
skew + rollover).

The ledger is the canonical sink for any security-relevant Samus event:
dispatch verdicts, governance decisions, auth events, screen results,
heartbeats — anything :mod:`backend.common.audit` writes.

Threading model: a single re-entrant lock around append + tip reload.
Concurrent writers are serialised; readers (verify, tail) take the same
lock so they never observe a half-written record.

Asymmetric reconcile-on-open:
  * Legacy ``{event, prev_hash, entry_hash}`` schema, chain valid → migrate
    in-place under canonical schema + current epoch key.
  * Canonical schema, chain valid → use as-is.
  * Canonical chain links intact but every HMAC fails → key-change
    rotation, archive ``_pre_keychange_`` and start fresh genesis.
  * Anything else → archive ``_pre_canonical_`` and start fresh genesis.

Port of :class:`backend.core.security.evidence_ledger.EvidenceLedger`
from Anita round-5 (2026-05-17).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .kdf import SAMUS_LEDGER_SALT_V1, hkdf_sha256

log = logging.getLogger(__name__)

_GENESIS = "0" * 64
_DEFAULT_EPOCH_SEC = 86400
_DEFAULT_EPOCH_OVERLAP_SEC = 300

# D8-03 — per-day ledger filename convention written by get_default_ledger:
# ``ledger-YYYYMMDD.jsonl``. The chain is continuous ACROSS day boundaries:
# a new day's first entry seeds its prev_hash from the PRIOR day's last-good
# hmac (not _GENESIS), so deleting a whole day's file breaks the link the
# next day's first prev_hash expects — making whole-day suppression
# tamper-evident exactly like a within-file edit. A path that does NOT match
# this pattern (e.g. an explicit test path) is treated as a standalone
# single-file ledger and starts from _GENESIS as before (no behaviour change).
_DAILY_NAME_RE = re.compile(r"^ledger-(\d{8})$")


@dataclass
class LedgerState:
    """Result of a verify() pass.

    * ``ok`` — True iff every entry chains correctly AND HMAC verifies.
    * ``chain_length`` — number of entries read before the verifier stopped.
    * ``last_hash`` — last-good hmac (or _GENESIS for an empty/never-written file).
    * ``broken_at`` — 0-based index of the first bad entry, or -1.
    * ``detail`` — human-readable reason for breakage.
    """

    ok: bool
    chain_length: int
    last_hash: str
    broken_at: int = -1
    detail: str = ""


class LedgerTamperError(RuntimeError):
    """Raised by :meth:`AuditLedger.assert_chain_intact` when verify() fails."""


# ---------- module-level helpers ------------------------------------------


def _canonical_material(seq: int, ts: float, type_: str, prev_hash: str, payload: Dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "seq": seq,
            "ts": ts,
            "type": type_,
            "prev_hash": prev_hash,
            "payload": payload,
        },
        default=str,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _epoch_number(ts: float, epoch_sec: int) -> int:
    return int(ts // epoch_sec)


def _epoch_key(secret: bytes, epoch_number: int) -> bytes:
    """Derive the per-epoch HMAC key via HKDF-SHA256 (RFC 5869).

    Salt is the per-ledger namespace constant ``SAMUS_LEDGER_SALT_V1`` so
    the derived key cannot be replayed in another HKDF context using the
    same secret. Info binds the output to a specific epoch number so
    adjacent epochs get independent keys despite sharing an IKM and salt.
    """
    return hkdf_sha256(
        ikm=secret,
        salt=SAMUS_LEDGER_SALT_V1,
        info=f"epoch-{epoch_number}".encode("utf-8"),
        length=32,
    )


def _candidate_epochs(ts: float, epoch_sec: int, overlap_sec: int) -> List[int]:
    """Epoch(s) to try when verifying an entry's hmac.

    Inside the overlap window at either end of the entry's home epoch we
    must also try the neighbouring epoch, since a writer near the boundary
    may have signed under the previous key while a clock-skewed verifier
    reads under the next (or vice versa).
    """
    home = _epoch_number(ts, epoch_sec)
    offset_in_epoch = ts - (home * epoch_sec)
    candidates = [home]
    if offset_in_epoch < overlap_sec:
        candidates.append(home - 1)
    if offset_in_epoch > (epoch_sec - overlap_sec):
        candidates.append(home + 1)
    return candidates


def _decode_secret(secret_str: str) -> bytes:
    """Accept either a base64-url-encoded secret or raw UTF-8."""
    try:
        return base64.urlsafe_b64decode(secret_str.encode())
    except Exception:
        return secret_str.encode("utf-8")


def _read_jsonl(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue


def _read_last_jsonl_record(path: Path) -> Optional[dict]:
    """Return the last well-formed JSON record without reading the whole file.

    Reads the file tail in growing chunks from the end (reverse seek) until a
    complete final line is found, then parses it. Used to seed the O(1) tip
    cache (R1) at open. Returns ``None`` for a missing / empty file or when no
    trailing line parses as JSON.
    """
    if not path.exists():
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None
    chunk = 4096
    with path.open("rb") as fh:
        buf = b""
        pos = size
        while pos > 0:
            read_size = min(chunk, pos)
            pos -= read_size
            fh.seek(pos)
            buf = fh.read(read_size) + buf
            # We need at least one newline preceding a non-empty trailing line.
            # Strip trailing whitespace/newlines, then look for the last line.
            stripped = buf.rstrip(b"\r\n")
            nl = stripped.rfind(b"\n")
            if nl != -1:
                last_line = stripped[nl + 1:]
                try:
                    return json.loads(last_line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # Trailing line is garbage — fall back to full scan below.
                    break
            if pos == 0:
                # Whole file is a single line.
                try:
                    return json.loads(stripped.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    break
    # Reverse-seek failed to find a parseable tail — fall back to a full scan
    # (bounded, runs at most once at open for a malformed/odd file).
    last: Optional[dict] = None
    for rec in _read_jsonl(path):
        last = rec
    return last


def _read_first_jsonl_record(path: Path) -> Optional[dict]:
    """Return the first well-formed JSON record (or None for empty/missing).

    Used at open to learn what a per-day file's own first entry links back to
    (its recorded ``prev_hash``) so single-file verification self-validates
    regardless of whether earlier day-files still exist — the cross-day
    linkage check lives in :meth:`AuditLedger.verify_cross_day`.
    """
    for rec in _read_jsonl(path):
        return rec
    return None


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, default=str, sort_keys=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _atomic_write_text(path: Path, content: str) -> None:
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------- module-level helpers: per-day chaining (D8-03) -----------------


def _daily_stamp(path: Path) -> Optional[str]:
    """Return the ``YYYYMMDD`` stamp of a per-day ledger file, or None.

    None means the path is NOT a dated daily ledger (e.g. an explicit test
    path or a custom sink) and must be treated as a standalone single-file
    ledger that starts from _GENESIS.
    """
    m = _DAILY_NAME_RE.match(path.stem)
    return m.group(1) if m else None


def _sibling_daily_files(path: Path) -> List[Tuple[str, Path]]:
    """All dated ``ledger-YYYYMMDD.jsonl`` siblings of ``path``, oldest first.

    Includes ``path`` itself when it exists. Archived/rotated files (which
    carry a ``_pre_*`` suffix and therefore don't match the strict daily
    pattern) are excluded so they never get mistaken for an active day.
    """
    out: List[Tuple[str, Path]] = []
    parent = path.parent
    if not parent.exists():
        # No directory yet -> path itself may still be a (not-yet-created)
        # daily file; the caller handles the empty case.
        stamp = _daily_stamp(path)
        return [(stamp, path)] if stamp else []
    for child in parent.iterdir():
        if child.suffix != path.suffix:
            continue
        stamp = _daily_stamp(child)
        if stamp is not None:
            out.append((stamp, child))
    out.sort(key=lambda t: t[0])
    return out


# ---------- public class ---------------------------------------------------


class AuditLedger:
    """Canonical hash-chained audit ledger.

    Construct with either an explicit ``secret_key`` (tests) or let it pull
    from :func:`backend.common.config.get_settings`.

    Instances are safe to share across threads — every public method takes
    a re-entrant lock and the tip hash is reloaded from disk on every
    append (never trust an in-memory cache for ``prev_hash``).
    """

    def __init__(
        self,
        ledger_path: str | Path,
        *,
        secret_key: bytes | None = None,
        epoch_sec: int | None = None,
        epoch_overlap_sec: int | None = None,
    ) -> None:
        self._path = Path(ledger_path)
        # Re-entrant so a recovery callback that itself records an event
        # (e.g. ``audit_ledger.record("recover.success")`` from inside a
        # health-check that also took the lock) doesn't self-deadlock.
        self._lock = threading.RLock()
        # D8-02 — remember the deployment env so reconcile can fail closed on a
        # genuine canonical-chain break outside development (operator-gated
        # recovery). Defaults to development for the explicit-key (test) path.
        self._env_name = "development"

        if secret_key is None:
            from .config import get_settings

            s = get_settings()
            # Effective ledger secret: prefer the dedicated
            # samus_ledger_secret_key so rotating samus_shared_hmac_key
            # (inter-service signing) does NOT invalidate the audit chain.
            # Fall back to shared_hmac_key when no dedicated ledger key is
            # sealed — existing deployments keep working without any
            # config change.
            self._env_name = getattr(s, "env", "development") or "development"
            ledger_secret_str = (
                getattr(s, "samus_ledger_secret_key", "") or s.shared_hmac_key
            )
            if not ledger_secret_str:
                # Fail closed outside development: an unconfigured ledger key
                # means every chain entry is signed with a known plaintext
                # secret, defeating tamper-evidence entirely.
                _env_name = getattr(s, "env", "development") or "development"
                if _env_name != "development":
                    raise RuntimeError(
                        "samus.audit_ledger.key_missing: "
                        "SAMUS_SHARED_HMAC_KEY or SAMUS_LEDGER_SECRET_KEY "
                        "must be set in non-development environments. "
                        "Refusing to start with the dev fallback key."
                    )
                ledger_secret_str = "samus-dev-ledger-fallback"
            self._secret = _decode_secret(ledger_secret_str)
            self._epoch_sec = (
                epoch_sec
                if epoch_sec is not None
                else int(getattr(s, "samus_ledger_epoch_sec", _DEFAULT_EPOCH_SEC))
            )
            self._epoch_overlap_sec = (
                epoch_overlap_sec
                if epoch_overlap_sec is not None
                else int(
                    getattr(s, "samus_ledger_epoch_overlap_sec", _DEFAULT_EPOCH_OVERLAP_SEC)
                )
            )
        else:
            self._secret = secret_key
            self._epoch_sec = epoch_sec if epoch_sec is not None else _DEFAULT_EPOCH_SEC
            self._epoch_overlap_sec = (
                epoch_overlap_sec
                if epoch_overlap_sec is not None
                else _DEFAULT_EPOCH_OVERLAP_SEC
            )

        # O(1) tip cache (R1). ``_scan_tip_hash`` / ``_scan_next_seq`` each did
        # a full-file O(n) read on EVERY append, making a busy ledger O(n^2)
        # over its lifetime. The single re-entrant lock already serialises all
        # writers in-process (the ledger is per-process — see get_default_ledger
        # which keys on PID-local singleton), so the in-memory tip IS
        # authoritative once seeded. Seed once from disk at open; thereafter
        # ``record`` updates the cache in place and reads no longer scan.
        self._last_hash: str = _GENESIS
        self._last_seq: int = 0
        # D8-03 — two related-but-distinct anchors:
        #   * ``_chain_seed`` — the prev_hash a NEW (first) entry should link
        #     to when this file is empty. For a standalone ledger that's
        #     _GENESIS; for a per-day file it's the PRIOR day's last-good hmac,
        #     so a new day continues yesterday's chain instead of resetting.
        #   * ``_self_start`` — the prev_hash THIS file's own first record
        #     already claims. Single-file verify/reconcile validate against
        #     this so an existing day still self-validates even if an earlier
        #     day-file was later deleted (that deletion is the job of the
        #     cross-day check to surface, NOT a reason to archive this file).
        # For an empty/new file the two coincide.
        self._chain_seed: str = _GENESIS
        self._self_start: str = _GENESIS
        with self._lock:
            self._chain_seed = self._compute_chain_seed()
            first = _read_first_jsonl_record(self._path)
            if first is not None:
                fp = first.get("prev_hash")
                self._self_start = fp if isinstance(fp, str) else self._chain_seed
            else:
                self._self_start = self._chain_seed
            self._reconcile_on_open()
            self._seed_tip_cache()

    # ---- public API -----------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def last_hash(self) -> str:
        return self._last_hash

    def record(self, event: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Append a canonical entry under the lock.

        ``event`` is the canonical ``type`` field. Returns the full record
        as it was written so callers can inspect ``seq`` / ``hmac``.
        """
        with self._lock:
            # O(1) tip (R1): trust the in-memory cache for prev_hash + seq.
            # The cache is seeded from disk at open and updated after every
            # successful append below, so it always reflects the on-disk tip
            # for this single-process ledger. No per-append full-file scan.
            prev_hash = self._last_hash
            seq = self._last_seq + 1
            ts = time.time()
            payload_dict = dict(payload or {})
            record = {
                "seq": seq,
                "ts": ts,
                "type": event,
                "prev_hash": prev_hash,
                "payload": payload_dict,
            }
            record["hmac"] = self._sign(seq, ts, event, prev_hash, payload_dict)
            _append_jsonl(self._path, record)
            # Update the cache only AFTER the append succeeds — a write that
            # raises leaves the tip pointing at the last durable record.
            self._last_hash = record["hmac"]
            self._last_seq = seq
            return record

    def tail(self, n: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            records: List[Dict[str, Any]] = list(_read_jsonl(self._path))
            return records[-n:]

    def verify(self) -> LedgerState:
        with self._lock:
            return self._verify_path(self._path)

    def assert_chain_intact(self) -> None:
        """Raise :class:`LedgerTamperError` if verify() fails.

        Use this at boot to fail loud — INV-3 (audit chain HMAC integrity).
        """
        state = self.verify()
        if not state.ok:
            raise LedgerTamperError(
                f"audit ledger broken at seq={state.broken_at} "
                f"(chain_length={state.chain_length}): {state.detail}"
            )

    def verify_cross_day(self, *, alert: bool = True) -> LedgerState:
        """Verify the WHOLE per-day file sequence as one continuous chain.

        Walks every ``ledger-YYYYMMDD.jsonl`` sibling oldest→newest. Each
        file's first entry must link to the previous file's last-good hmac;
        within a file the ordinary forward chain must hold. A break in EITHER
        — an intra-file tamper, OR a missing/deleted intervening day whose
        tail the next day's first ``prev_hash`` no longer matches — fails the
        verification. This closes whole-day-deletion suppression that a
        single-file :meth:`verify` cannot see.

        Returns the aggregate :class:`LedgerState` (``chain_length`` = total
        entries across all days; ``last_hash`` = newest day's tail). For a
        standalone (non-dated) ledger this degrades to a single-file verify.

        When ``alert`` is True a detected break is announced on an INDEPENDENT
        sink (module logger CRITICAL + stderr) — mirroring
        :meth:`_alert_canonical_break` (the D8-02 alert path) — and, outside
        development, raises :class:`LedgerTamperError` (fail-closed, operator-
        gated recovery; the files are preserved on disk for forensics).
        """
        with self._lock:
            siblings = _sibling_daily_files(self._path)
            if not siblings:
                # Not a dated ledger -> fall back to single-file verify.
                return self.verify()

            prev = _GENESIS
            total = 0
            for stamp, path in siblings:
                state = self._verify_path(path, start_prev=prev)
                total += state.chain_length
                if not state.ok:
                    detail = f"day={stamp}: {state.detail}"
                    if alert:
                        self._alert_cross_day_break(stamp, path, detail)
                    return LedgerState(
                        ok=False,
                        chain_length=total,
                        last_hash=prev,
                        broken_at=state.broken_at,
                        detail=detail,
                    )
                # A day-file with content advances the link; an empty day-file
                # legitimately contributes nothing and leaves ``prev`` intact.
                if state.last_hash != prev or state.chain_length:
                    prev = state.last_hash
            return LedgerState(ok=True, chain_length=total, last_hash=prev)

    def _alert_cross_day_break(self, stamp: str, path: Path, detail: str) -> None:
        """D8-03 — announce a cross-day chain break on an independent sink.

        Reuses the D8-02 alert idiom: log CRITICAL + direct stderr (NOT the
        ledger, which is the thing under suspicion), then fail closed outside
        development. A broken cross-day link is the signature of a deleted /
        suppressed whole day, so it is treated with the same severity as an
        intra-file tamper.
        """
        import sys

        msg = (
            "audit_ledger: CROSS-DAY CHAIN BREAK detected "
            f"(day={stamp} path={path}). {detail}. "
            "A missing intervening day or a broken day-to-day link suggests "
            "whole-day suppression; files are preserved on disk for forensics."
        )
        log.critical(msg)
        try:
            print(f"[SECURITY] {msg}", file=sys.stderr, flush=True)
        except Exception:  # noqa: BLE001 - alerting must never mask the break
            pass
        try:  # real-time operator push (fail-soft; import lazy to avoid cycle)
            from .notify import notify_operator
            notify_operator("Audit ledger tamper", msg, severity="critical", dedup_key="ledger_tamper")
        except Exception:  # noqa: BLE001 - notify must never mask the break
            pass
        if self._env_name != "development":
            raise LedgerTamperError(msg)

    # ---- internal: tip cache (R1, O(1) append) -------------------------

    def _seed_tip_cache(self) -> None:
        """Seed ``_last_hash`` / ``_last_seq`` from the on-disk tail once.

        Reads only the LAST record via a bounded reverse-seek (not the whole
        file) so seeding is O(tail) rather than O(n). Falls back to a full
        scan only if the tail read can't be interpreted (tiny/odd files),
        which is bounded by file size and happens at most once per open.
        """
        rec = _read_last_jsonl_record(self._path)
        if rec is not None:
            h = rec.get("hmac") or rec.get("entry_hash")
            if isinstance(h, str):
                self._last_hash = h
            try:
                self._last_seq = int(rec.get("seq", 0))
            except (TypeError, ValueError):
                self._last_seq = 0
            return
        # Empty / unreadable tail -> the cross-day chain seed (D8-03). For a
        # standalone ledger this IS _GENESIS; for a per-day file it's the prior
        # day's last-good hmac, so the new day's first ``record`` links back to
        # yesterday instead of resetting to genesis.
        self._last_hash = self._chain_seed
        self._last_seq = 0

    # ---- internal: cross-day chaining (D8-03) --------------------------

    def _compute_chain_seed(self) -> str:
        """Return the prev_hash this file's first entry must link back to.

        For a standalone (non-dated) ledger, that's _GENESIS. For a per-day
        ``ledger-YYYYMMDD.jsonl`` file, it's the last-good hmac of the most
        recent EARLIER day-file that has content — making the hash chain
        continuous across day boundaries. Returns _GENESIS when this is the
        first dated file (true genesis day) or when no earlier file has any
        records.

        Reads only the prior file's tail (bounded reverse-seek), so this is
        O(1) per open regardless of history depth.
        """
        stamp = _daily_stamp(self._path)
        if stamp is None:
            return _GENESIS
        prior_tail = _GENESIS
        for sib_stamp, sib_path in _sibling_daily_files(self._path):
            if sib_stamp >= stamp:
                continue
            rec = _read_last_jsonl_record(sib_path)
            if rec is None:
                continue
            h = rec.get("hmac") or rec.get("entry_hash")
            if isinstance(h, str):
                prior_tail = h  # keep walking; last (newest) prior wins
        return prior_tail

    # ---- internal: disk scan (verify / legacy paths only) --------------

    def _scan_tip_hash(self) -> str:
        last = _GENESIS
        for rec in _read_jsonl(self._path):
            h = rec.get("hmac") or rec.get("entry_hash")
            if isinstance(h, str):
                last = h
        return last

    def _scan_next_seq(self) -> int:
        seq = 0
        for rec in _read_jsonl(self._path):
            seq = max(seq, int(rec.get("seq", 0)))
        return seq + 1

    # ---- internal: signing + verification ------------------------------

    def _sign(self, seq: int, ts: float, type_: str, prev_hash: str, payload: Dict[str, Any]) -> str:
        material = _canonical_material(seq, ts, type_, prev_hash, payload)
        epoch = _epoch_number(ts, self._epoch_sec)
        key = _epoch_key(self._secret, epoch)
        return hmac.new(key, material, hashlib.sha256).hexdigest()

    def _verify_hmac(self, rec: Dict[str, Any]) -> bool:
        ts = float(rec.get("ts", 0.0))
        seq = int(rec.get("seq", 0))
        type_ = str(rec.get("type", ""))
        prev_hash = str(rec.get("prev_hash", _GENESIS))
        payload = rec.get("payload") or {}
        stored = rec.get("hmac")
        if not isinstance(stored, str):
            return False
        material = _canonical_material(seq, ts, type_, prev_hash, payload)
        for epoch in _candidate_epochs(ts, self._epoch_sec, self._epoch_overlap_sec):
            key = _epoch_key(self._secret, epoch)
            expected = hmac.new(key, material, hashlib.sha256).hexdigest()
            if hmac.compare_digest(expected, stored):
                return True
        return False

    def _verify_path(self, path: Path, start_prev: Optional[str] = None) -> LedgerState:
        # D8-03 — single-file verify validates against what this file's own
        # first record claims (``_self_start``), so a per-day file self-
        # validates whether or not earlier day-files still exist. The cross-day
        # linkage is checked separately by verify_cross_day(), which passes an
        # explicit ``start_prev`` = the prior file's actual tail.
        prev = self._self_start if start_prev is None else start_prev
        count = 0
        for i, rec in enumerate(_read_jsonl(path)):
            count += 1
            stored_prev = rec.get("prev_hash")
            if stored_prev != prev:
                return LedgerState(
                    ok=False,
                    chain_length=count,
                    last_hash=prev,
                    broken_at=i,
                    detail=f"prev_hash mismatch at seq={rec.get('seq')}",
                )
            if "hmac" not in rec:
                return LedgerState(
                    ok=False,
                    chain_length=count,
                    last_hash=prev,
                    broken_at=i,
                    detail=f"hmac missing at seq={rec.get('seq')}",
                )
            if not self._verify_hmac(rec):
                return LedgerState(
                    ok=False,
                    chain_length=count,
                    last_hash=prev,
                    broken_at=i,
                    detail=f"hmac mismatch at seq={rec.get('seq')}",
                )
            prev = rec["hmac"]
        return LedgerState(ok=True, chain_length=count, last_hash=prev)

    # ---- internal: open-time reconciliation ----------------------------

    def _reconcile_on_open(self) -> None:
        """Asymmetric migration WHY: a broken chain cannot be salvaged
        (post-break entries' prev_hash is wrong by definition), so we
        archive it for forensics and start a fresh genesis. A healthy
        chain still on legacy field names CAN be re-signed because we
        trust the chain itself.

        A third class of failure also lands here: the prev_hash chain is
        internally consistent (no tampering) BUT every HMAC fails to
        verify under the current effective key. That signature means the
        operator rotated ``samus_ledger_secret_key``. The chain is
        genuine but unverifiable going forward — archive it under a
        distinct ``_pre_keychange_`` name and start a fresh genesis.
        """
        if not self._path.exists():
            return

        records = list(_read_jsonl(self._path))
        if not records:
            return

        uses_legacy = any("event" in r and "type" not in r for r in records)

        if uses_legacy:
            legacy_state = self._verify_legacy_chain(records)
            if legacy_state.ok:
                self._migrate_legacy_in_place(records)
                log.info(
                    "audit_ledger: migrated %d legacy entries to canonical schema",
                    len(records),
                )
                return
            self._alert_canonical_break(legacy_state.chain_length, legacy_state.broken_at)
            self._archive_and_reset(legacy_state.chain_length, legacy_state.broken_at)
            return

        state = self._verify_path(self._path)
        if state.ok:
            return

        if self._chain_links_intact(records) and self._all_hmacs_fail(records):
            self._archive_and_reset(
                state.chain_length,
                state.broken_at,
                reason="keychange",
            )
            return

        self._alert_canonical_break(state.chain_length, state.broken_at)
        self._archive_and_reset(state.chain_length, state.broken_at)

    def _alert_canonical_break(self, chain_length: int, broken_at: int) -> None:
        """D8-02 — a genuine canonical-chain break (tamper/corruption, NOT a
        legacy migration or an operator key-change) is a security event. Emit a
        HIGH-severity alert to a SINK INDEPENDENT of the ledger about to be
        reset (the module logger / stderr — not the ledger file), so a
        tamper-then-restart cannot silently launder the break. Outside
        development, FAIL CLOSED: refuse to auto-reset and require operator-
        gated recovery (the broken chain is preserved on disk for forensics).
        """
        import sys

        msg = (
            "audit_ledger: CANONICAL CHAIN BREAK detected on open "
            f"(path={self._path} chain_length={chain_length} broken_at={broken_at}). "
            "This is a tamper/corruption signal, not a key-change or legacy "
            "migration. The broken chain is preserved on disk for forensics."
        )
        # Independent sink: log at CRITICAL + a direct stderr write (the ledger
        # file itself is NOT trustworthy here and is about to be archived).
        log.critical(msg)
        try:
            print(f"[SECURITY] {msg}", file=sys.stderr, flush=True)
        except Exception:  # noqa: BLE001 - alerting must never mask the break
            pass
        try:  # real-time operator push (fail-soft; import lazy to avoid cycle)
            from .notify import notify_operator
            notify_operator("Audit ledger tamper", msg, severity="critical", dedup_key="ledger_tamper")
        except Exception:  # noqa: BLE001 - notify must never mask the break
            pass
        if self._env_name != "development":
            raise LedgerTamperError(msg)

    def _chain_links_intact(self, records: List[Dict[str, Any]]) -> bool:
        """True iff every record's ``prev_hash`` matches the previous
        row's stored ``hmac`` (or the cross-day seed for the first row).
        Independent of whether the HMACs themselves verify.

        D8-03 — the first row links to ``self._self_start`` (what this file's
        own first record claims), so a key-change on a non-genesis day is
        still recognised as "links intact, sigs fail".
        """
        prev = self._self_start
        for rec in records:
            if rec.get("prev_hash") != prev:
                return False
            h = rec.get("hmac")
            if not isinstance(h, str):
                return False
            prev = h
        return True

    def _all_hmacs_fail(self, records: List[Dict[str, Any]]) -> bool:
        """True iff NO record's hmac verifies under the current effective
        key. Used to distinguish key-change (every signature wrong, but
        consistently so) from corruption (a single tampered row in an
        otherwise valid chain).
        """
        if not records:
            return False
        for rec in records:
            if self._verify_hmac(rec):
                return False
        return True

    @staticmethod
    def _verify_legacy_chain(records: List[Dict[str, Any]]) -> LedgerState:
        """Verify a legacy ``{event, prev_hash, entry_hash}`` chain.

        Samus's prior shape (``events.build_audit_event``) only hashed
        the payload, not the chain — so there's no entry_hash to verify.
        This helper covers the Anita-style legacy used by other agents;
        for Samus's own legacy (which has no chain), files are simply
        treated as "no chain" and the verifier returns ok=True with
        chain_length=0 when no legacy records present.
        """
        prev = _GENESIS
        count = 0
        for i, rec in enumerate(records):
            count += 1
            if rec.get("prev_hash") != prev:
                return LedgerState(
                    ok=False,
                    chain_length=count,
                    last_hash=prev,
                    broken_at=i,
                    detail=f"legacy prev_hash mismatch at seq={rec.get('seq')}",
                )
            material = json.dumps(
                {
                    "seq": rec.get("seq"),
                    "ts": rec.get("ts"),
                    "event": rec.get("event"),
                    "payload": rec.get("payload"),
                    "prev_hash": rec.get("prev_hash"),
                    "id": rec.get("id"),
                },
                default=str,
                sort_keys=True,
            ).encode("utf-8")
            expected = hashlib.sha256(material).hexdigest()
            if expected != rec.get("entry_hash"):
                return LedgerState(
                    ok=False,
                    chain_length=count,
                    last_hash=prev,
                    broken_at=i,
                    detail=f"legacy entry_hash mismatch at seq={rec.get('seq')}",
                )
            prev = rec["entry_hash"]
        return LedgerState(ok=True, chain_length=count, last_hash=prev)

    def _migrate_legacy_in_place(self, records: List[Dict[str, Any]]) -> None:
        new_lines: List[str] = []
        prev_hash = _GENESIS
        for rec in records:
            seq = int(rec.get("seq", 0))
            ts = float(rec.get("ts", time.time()))
            type_ = str(rec.get("event", ""))
            payload = dict(rec.get("payload") or {})
            entry = {
                "seq": seq,
                "ts": ts,
                "type": type_,
                "prev_hash": prev_hash,
                "payload": payload,
            }
            entry["hmac"] = self._sign(seq, ts, type_, prev_hash, payload)
            new_lines.append(json.dumps(entry, default=str, sort_keys=True))
            prev_hash = entry["hmac"]
        _atomic_write_text(self._path, "\n".join(new_lines) + ("\n" if new_lines else ""))

    def _archive_and_reset(
        self,
        chain_length: int,
        broken_at: int,
        *,
        reason: str = "canonical",
    ) -> None:
        """Archive the on-disk chain and start a fresh genesis.

        ``reason`` selects the archive filename suffix so forensics can
        tell at a glance WHY the rescue ran:
          * ``"canonical"`` — broken chain (corruption or legacy-schema
            chain whose chain itself didn't verify).
          * ``"keychange"`` — internally consistent chain whose HMACs
            don't verify under the current key; the archive is genuine
            forensic evidence signed under the old key, preserved so it
            can be re-verified by an operator who still holds that key.

        Per ``feedback_samus_retain_rollback_artifacts``: the broken
        chain is NOT deleted. It's renamed with a timestamped suffix so
        the operator can rotate keys instead of losing evidence.
        """
        archive = self._path.with_name(
            f"{self._path.stem}_pre_{reason}_{int(time.time())}{self._path.suffix}"
        )
        try:
            self._path.replace(archive)
        except OSError as exc:
            log.error("audit_ledger: archive rename failed (%s); falling back to copy", exc)
            data = self._path.read_bytes()
            archive.write_bytes(data)
            self._path.unlink()
        if reason == "keychange":
            log.warning(
                "audit_ledger: KEY CHANGE DETECTED — chain (length=%d) verified "
                "internally but no HMAC validates under current effective key. "
                "Archived to %s and starting fresh genesis under new key. "
                "Preserve the archive + old key for any forensic re-verification.",
                chain_length,
                archive,
            )
        else:
            log.warning(
                "audit_ledger: archived broken chain (length=%d, broken_at=%d) "
                "to %s and starting fresh genesis under canonical schema",
                chain_length,
                broken_at,
                archive,
            )


# ---------- module-level convenience singleton ----------------------------

_default_ledger: Optional[AuditLedger] = None
_default_lock = threading.Lock()

# Default data root when ``SAMUS_DATA_ROOT`` is unset. The Windows value is
# the operator's host store; the POSIX value matches the writable
# ``samus-data`` volume the compose stack mounts at ``/opt/samus/data``
# (mirrors ``backend/common/worker_base.py`` + ``state_paths.py``). Using the
# host Windows path on a Linux container turned ``E:\Hustleforge\Samus\data``
# into a bogus relative component whose ``mkdir(parents=True)`` walked up into
# the read-only ``/opt/samus`` mount and raised ``OSError: [Errno 30]
# Read-only file system`` — silently dropping tamper-evident audit records.
_DEFAULT_DATA_ROOT_WINDOWS = r"E:\Hustleforge\Samus\data"
_DEFAULT_DATA_ROOT_POSIX = "/opt/samus/data"


def _is_windows_host() -> bool:
    """True on a Windows host. Isolated so tests can force either platform
    without mutating the process-global ``os.name`` (which would break
    ``pathlib`` flavour selection)."""
    return os.name == "nt"


def _default_data_root() -> str:
    """Resolve a platform-sane default for the audit ledger data root.

    Honors ``SAMUS_DATA_ROOT`` when set (compose points it at the mounted
    volume). Falls back to the host Windows store on Windows and to the
    container's writable ``/opt/samus/data`` volume everywhere else, so an
    unset env var in any future container/CI context no longer constructs an
    unwritable ``E:\\...`` Windows path on a POSIX host.
    """
    override = os.getenv("SAMUS_DATA_ROOT")
    if override:
        return override
    return _DEFAULT_DATA_ROOT_WINDOWS if _is_windows_host() else _DEFAULT_DATA_ROOT_POSIX


def get_default_ledger() -> AuditLedger:
    """Lazy singleton for the daily audit ledger.

    Path defaults to ``<SAMUS_DATA_ROOT or platform default>
    /evidence/audit/ledger-YYYYMMDD.jsonl``, where the platform default is
    the host Windows store on Windows and the container's writable
    ``/opt/samus/data`` volume on POSIX (see :func:`_default_data_root`).
    Override the full path via ``SAMUS_AUDIT_LEDGER_PATH`` env (read fresh on
    first call).
    """
    global _default_ledger
    if _default_ledger is not None:
        return _default_ledger
    with _default_lock:
        if _default_ledger is not None:
            return _default_ledger
        import os
        from datetime import datetime, timezone

        explicit = os.getenv("SAMUS_AUDIT_LEDGER_PATH")
        if explicit:
            path = Path(explicit)
        else:
            root = _default_data_root()
            day = datetime.now(timezone.utc).strftime("%Y%m%d")
            path = Path(root) / "evidence" / "audit" / f"ledger-{day}.jsonl"
        _default_ledger = AuditLedger(path)
        return _default_ledger


def reset_default_ledger() -> None:
    """Test helper — drop the cached singleton so the next call rebuilds."""
    global _default_ledger
    with _default_lock:
        _default_ledger = None
