"""Samus-side MCP client for the cross-agent Quorum Hub.

Mirror of `_shared/quorum_hub/client.py`, vendored into Samus so containers
don't need a host bind-mount of `_shared/`. Stdlib-only (urllib + hmac +
hashlib + json) so it works in every workcell image without extra deps.

Hub contract
------------
- POST {QUORUM_HUB_URL}/mcp  — JSON-RPC 2.0
- Tools: governance_publish, governance_log, governance_stats
- HMAC: if SAMUS_QUORUM_HUB_HMAC_KEY (hex) is set, every body is signed and
  carried in `X-Hub-HMAC`.

Defaults
--------
- URL: http://host.docker.internal:8090 (compose extra_hosts maps this to
  the host gateway; Docker Desktop resolves natively).
- Publishing OFF unless SAMUS_QUORUM_PUBLISH_ENABLED is truthy. Mirrors the
  dormancy posture of SAMUS_AUTHZ_MODE / SAMUS_DISCIPLINE_MODE.

Fail-open
---------
Every public method returns a quiet sentinel (False / [] / {}) on transport
or hub-side failure. The hub is an advisory observer — Samus never crashes
or stalls when it's down or misconfigured.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from typing import Any

from .net_limits import QUORUM_HUB_MAX_BYTES, ResponseTooLarge, read_capped

_LOG = logging.getLogger("samus.quorum_client")

_DEFAULT_URL = "http://host.docker.internal:8090"
_REQUEST_TIMEOUT_S = 3


def _publish_enabled() -> bool:
    """Read SAMUS_QUORUM_PUBLISH_ENABLED at call time (env can change in tests)."""
    return os.environ.get("SAMUS_QUORUM_PUBLISH_ENABLED", "").lower() in {"1", "true", "yes"}


class QuorumHubClient:
    """Sync client for POST /mcp on the cross-agent quorum hub."""

    def __init__(
        self,
        base_url: str | None = None,
        hmac_key: bytes | None = None,
        timeout_s: int = _REQUEST_TIMEOUT_S,
    ) -> None:
        self._base_url = (base_url or os.environ.get("SAMUS_QUORUM_HUB_URL", _DEFAULT_URL)).rstrip("/")
        if hmac_key is None:
            key_hex = os.environ.get("SAMUS_QUORUM_HUB_HMAC_KEY", "").strip()
            hmac_key = bytes.fromhex(key_hex) if key_hex else None
        self._hmac_key = hmac_key
        self._timeout = timeout_s

    # ------------------------------------------------------------------
    # Public API — every method fails open.
    # ------------------------------------------------------------------

    def publish(
        self,
        *,
        caller: str,
        action: str,
        risk_score: float,
        approved: bool,
        approval_score: float,
        threshold: float,
        votes: list[dict[str, Any]],
        reason: str,
    ) -> bool:
        """Publish one governance event. Returns False on any failure or when disabled."""
        if not _publish_enabled():
            return False
        payload = self._rpc(
            "tools/call",
            {
                "name": "governance_publish",
                "arguments": {
                    "caller": caller,
                    "action": action,
                    "risk_score": float(risk_score),
                    "approved": bool(approved),
                    "approval_score": float(approval_score),
                    "threshold": float(threshold),
                    "votes": list(votes),
                    "reason": reason,
                },
            },
        )
        if payload is None or "error" in payload:
            if payload and "error" in payload:
                _LOG.warning("governance_publish error: %s", payload["error"])
            return False
        return True

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Recent events. Returns [] on failure or when disabled."""
        if not _publish_enabled():
            return []
        payload = self._rpc(
            "tools/call",
            {"name": "governance_log", "arguments": {"limit": int(limit)}},
        )
        if payload is None:
            return []
        try:
            text = payload["result"]["content"][0]["text"]
            data = json.loads(text)
            return list(data.get("events") or [])
        except Exception:  # noqa: BLE001
            return []

    def stats(self) -> dict[str, Any]:
        """Hub stats. Returns {} on failure or when disabled."""
        if not _publish_enabled():
            return {}
        payload = self._rpc("tools/call", {"name": "governance_stats", "arguments": {}})
        if payload is None:
            return {}
        try:
            text = payload["result"]["content"][0]["text"]
            return json.loads(text) or {}
        except Exception:  # noqa: BLE001
            return {}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }, separators=(",", ":")).encode()

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._hmac_key:
            sig = _hmac.new(self._hmac_key, body, hashlib.sha256).hexdigest()
            headers["X-Hub-HMAC"] = sig

        req = urllib.request.Request(
            f"{self._base_url}/mcp",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
                # S3: cap the hub response so a hostile/compromised hub cannot
                # OOM the workcell with a multi-GB body. Hub JSON-RPC replies
                # are tiny; the ceiling is generous headroom.
                raw = read_capped(
                    resp, max_bytes=QUORUM_HUB_MAX_BYTES, source="quorum_hub",
                )
            return json.loads(raw)
        except ResponseTooLarge as exc:
            _LOG.warning("quorum hub response over cap: %s", exc)
            return None
        except urllib.error.URLError as exc:
            _LOG.debug("quorum hub unreachable: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("quorum hub client error: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_client: QuorumHubClient | None = None
_client_lock = threading.Lock()


def get_quorum_client() -> QuorumHubClient:
    """Return the process-wide QuorumHubClient. Reads env on first call."""
    global _client  # noqa: PLW0603
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = QuorumHubClient()
    return _client


def _reset_for_tests() -> None:
    """Test-only singleton reset."""
    global _client  # noqa: PLW0603
    with _client_lock:
        _client = None


__all__ = ["QuorumHubClient", "get_quorum_client"]
