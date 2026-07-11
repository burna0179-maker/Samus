"""Pattern B heartbeat — HTTP + observable file (Hustleforge ecosystem §9).

Samus emits a heartbeat on two parallel paths every ``interval_sec``:

  1. **HTTP (canonical):** ``POST {SECURITY_SENTINEL_URL}/heartbeat`` with
     an HMAC-SHA256-signed envelope. The signing key is derived from
     ``samus_shared_hmac_key`` and a 24-hour rotating epoch
     (``floor(time.time() / 86400)``). Failures here are silent — Major
     may not exist yet during agent-skeleton bring-up.

  2. **File (observable):** the same envelope written atomically to the
     ``samus_heartbeat_path`` — the in-container data root
     ``/opt/samus/data/coordination/samus_heartbeat.json``, bind-mounted
     from the host (``D:/Hustleforge/Samus/.data/`` on Windows-dev or
     ``/home/hustleforge/data/samus/`` on the VM). Peer agents read this
     without consulting Major. Cloud Run is stateless and emits only the
     HTTP path.

Envelope payload is intentionally minimal — ``{agent_id, ts, process_pid}``.
Docker telemetry is NOT included because the heartbeat envelope shape must
be identical across every deployment target.

Replay-cache hazard (preserved from arch doc §5): if a future module
ever extends Major's watchdog to read the observable file AND the HTTP
handler validates the same envelope, ``verify()`` must be called from
exactly one owner per envelope. This writer does NOT call ``verify()``;
only Major's HTTP handler should.

Lifecycle:
  * :func:`start` — idempotent; spawns one daemon thread per process.
    Safe to call multiple times — subsequent calls are no-ops.
  * :func:`stop` — sets a stop flag; the daemon exits within
    ``interval_sec``. Mostly used by tests.
  * :func:`write_once` — synchronous single emit, used by tests and the
    boot path before the daemon thread starts.

The daemon thread is best-effort by design — any exception in either
channel is logged and swallowed. The shell must never crash because Major
is reachable or the shared coordination directory is unwritable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import socket
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("samus.heartbeat")

_DEFAULT_AGENT_ID = "samus"
_DEFAULT_INTERVAL_SEC = 30.0
_DEFAULT_PATH_WINDOWS = r"E:\Hustleforge\_shared\coordination\Samus\samus_heartbeat.json"
_DEFAULT_PATH_POSIX = "/home/hustleforge/data/_shared/coordination/Samus/samus_heartbeat.json"
_DEFAULT_SENTINEL_URL = "http://localhost:8434"

# Epoch rotation matches the audit ledger epoch (24h) but with a separate
# derivation context so a leaked heartbeat key cannot forge audit signatures.
_HEARTBEAT_EPOCH_SEC = 86400
_HEARTBEAT_INFO = b"samus-heartbeat-v1"


@dataclass
class HeartbeatConfig:
    agent_id: str = _DEFAULT_AGENT_ID
    interval_sec: float = _DEFAULT_INTERVAL_SEC
    file_path: Path = field(
        default_factory=lambda: Path(
            os.getenv(
                "SAMUS_HEARTBEAT_PATH",
                _DEFAULT_PATH_WINDOWS if os.name == "nt" else _DEFAULT_PATH_POSIX,
            )
        )
    )
    sentinel_url: str = field(
        default_factory=lambda: os.getenv("SECURITY_SENTINEL_URL", _DEFAULT_SENTINEL_URL)
    )
    # Empty string disables HTTP signing — the file is still written.
    hmac_secret: str = ""
    # http_timeout MUST stay short — the heartbeat is best-effort; we'd
    # rather miss one beat than block the daemon for 30s waiting on a
    # hung Major.
    http_timeout_sec: float = 2.0


def _epoch_number(ts: float) -> int:
    return int(ts // _HEARTBEAT_EPOCH_SEC)


def _derive_heartbeat_key(secret: str, ts: float) -> bytes:
    """Derive the per-epoch heartbeat-signing key.

    Uses HKDF-SHA256 with a heartbeat-namespaced info so a leaked beat
    signature cannot be replayed against the audit ledger (which uses
    salt ``samus-ledger-v1``).
    """
    # Defer HKDF import to runtime so the module loads even if kdf was
    # somehow stripped (graceful degrade — fall back to raw HMAC).
    try:
        from .kdf import hkdf_sha256

        return hkdf_sha256(
            ikm=secret.encode("utf-8"),
            salt=b"samus-heartbeat-salt-v1",
            info=_HEARTBEAT_INFO + f"-epoch-{_epoch_number(ts)}".encode("utf-8"),
            length=32,
        )
    except Exception:
        # Raw HMAC fallback — still epoch-bound via the epoch number.
        return hmac.new(
            secret.encode("utf-8"),
            f"heartbeat-epoch-{_epoch_number(ts)}".encode("utf-8"),
            hashlib.sha256,
        ).digest()


def _sign_envelope(secret: str, envelope: dict) -> str:
    """HMAC-SHA256 hex over canonical(envelope), keyed by epoch."""
    ts = float(envelope.get("ts", time.time()))
    key = _derive_heartbeat_key(secret, ts)
    material = json.dumps(envelope, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return hmac.new(key, material, hashlib.sha256).hexdigest()


def _build_envelope(config: HeartbeatConfig) -> dict:
    return {
        "agent_id": config.agent_id,
        "ts": time.time(),
        "process_pid": os.getpid(),
        "hostname": socket.gethostname(),
    }


def _write_file(path: Path, payload: dict) -> None:
    """Atomic write: tmp + replace. Never partial on disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, sort_keys=True, default=str, indent=2)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                # fsync can fail on some networked / tmpfs mounts; the
                # replace below is still atomic enough for the observer.
                pass
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _post_http(config: HeartbeatConfig, envelope: dict, signature: str) -> None:
    """Best-effort POST; any exception is logged and swallowed."""
    try:
        import httpx
    except Exception:
        # httpx may not be installed in stripped-down environments.
        return
    url = config.sentinel_url.rstrip("/") + "/heartbeat"
    try:
        headers = {"Content-Type": "application/json"}
        if signature:
            headers["X-Samus-Heartbeat-Signature"] = signature
            headers["X-Samus-Heartbeat-Epoch"] = str(_epoch_number(envelope["ts"]))
        httpx.post(url, json=envelope, headers=headers, timeout=config.http_timeout_sec)
    except Exception as exc:
        log.debug("heartbeat HTTP post to %s failed (best-effort): %s", url, exc)


def write_once(config: Optional[HeartbeatConfig] = None) -> dict:
    """Synchronous single heartbeat emit on both channels.

    Returns the envelope (with signature attached if a secret was
    configured) so callers can inspect / assert in tests.
    """
    cfg = config or _resolve_config()
    env = _build_envelope(cfg)
    signature = _sign_envelope(cfg.hmac_secret, env) if cfg.hmac_secret else ""
    file_payload = dict(env)
    if signature:
        file_payload["signature"] = signature
    # File first — peer agents read this without Major, so it's the
    # higher-trust channel for cross-agent observability.
    try:
        _write_file(cfg.file_path, file_payload)
    except Exception as exc:
        log.warning("heartbeat file write failed: %s", exc)
    _post_http(cfg, env, signature)
    return file_payload


def _resolve_config(config: Optional[HeartbeatConfig] = None) -> HeartbeatConfig:
    if config is not None:
        return config
    cfg = HeartbeatConfig()
    try:
        from .config import get_settings

        s = get_settings()
        cfg.hmac_secret = getattr(s, "shared_hmac_key", "") or ""
    except Exception:
        pass
    return cfg


# ---------- daemon ----------------------------------------------------------

_thread: Optional[threading.Thread] = None
_stop_event: Optional[threading.Event] = None
_thread_lock = threading.Lock()


def start(config: Optional[HeartbeatConfig] = None) -> bool:
    """Spawn the heartbeat daemon. Idempotent.

    Returns True if a fresh thread was started, False if one was already
    running (subsequent calls don't spawn a second daemon).
    """
    global _thread, _stop_event
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return False
        cfg = _resolve_config(config)
        _stop_event = threading.Event()
        stop_event = _stop_event

        def _loop() -> None:
            log.info(
                "heartbeat daemon started (interval=%.1fs, agent=%s)",
                cfg.interval_sec,
                cfg.agent_id,
            )
            while not stop_event.is_set():
                try:
                    write_once(cfg)
                except Exception as exc:
                    log.exception("heartbeat write_once raised (continuing): %s", exc)
                stop_event.wait(cfg.interval_sec)
            log.info("heartbeat daemon stopped")

        t = threading.Thread(target=_loop, name="samus-heartbeat", daemon=True)
        t.start()
        _thread = t
        return True


def stop(timeout: float = 5.0) -> None:
    """Signal the daemon to exit and wait briefly for it to do so."""
    global _thread, _stop_event
    with _thread_lock:
        if _stop_event is None:
            return
        _stop_event.set()
        t = _thread
    if t is not None:
        t.join(timeout=timeout)
    with _thread_lock:
        _thread = None
        _stop_event = None


def is_running() -> bool:
    with _thread_lock:
        return _thread is not None and _thread.is_alive()
