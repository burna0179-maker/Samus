"""Pattern B heartbeat — file write + signature + daemon lifecycle."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from backend.common import heartbeat


def _cfg(tmp_path: Path, *, secret: str = "x" * 32) -> heartbeat.HeartbeatConfig:
    return heartbeat.HeartbeatConfig(
        agent_id="samus",
        interval_sec=0.1,
        file_path=tmp_path / "heartbeat.json",
        sentinel_url="http://127.0.0.1:1",  # unreachable on purpose — best-effort
        hmac_secret=secret,
        http_timeout_sec=0.1,
    )


def test_write_once_creates_file_with_envelope(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    out = heartbeat.write_once(cfg)
    assert cfg.file_path.exists()
    data = json.loads(cfg.file_path.read_text(encoding="utf-8"))
    assert data["agent_id"] == "samus"
    assert isinstance(data["ts"], (int, float))
    assert isinstance(data["process_pid"], int)
    assert "signature" in data
    assert data["signature"] == out["signature"]


def test_write_once_no_signature_when_secret_empty(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, secret="")
    heartbeat.write_once(cfg)
    data = json.loads(cfg.file_path.read_text(encoding="utf-8"))
    assert "signature" not in data


def test_write_once_atomic_replace(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.file_path.write_text("garbage-from-old-process", encoding="utf-8")
    heartbeat.write_once(cfg)
    data = json.loads(cfg.file_path.read_text(encoding="utf-8"))
    assert data["agent_id"] == "samus"


def test_envelope_signature_changes_per_epoch(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, secret="abc12345" * 4)
    env_a = {"agent_id": "samus", "ts": 100.0, "process_pid": 1, "hostname": "h"}
    env_b = {"agent_id": "samus", "ts": 100.0 + 86400 * 2, "process_pid": 1, "hostname": "h"}
    sig_a = heartbeat._sign_envelope(cfg.hmac_secret, env_a)
    sig_b = heartbeat._sign_envelope(cfg.hmac_secret, env_b)
    assert sig_a != sig_b


def test_daemon_start_stop_is_idempotent(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert heartbeat.start(cfg) is True
    try:
        # Second start is a no-op while the first is alive.
        assert heartbeat.start(cfg) is False
        assert heartbeat.is_running()
        # Wait for at least one beat to land on disk.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if cfg.file_path.exists():
                break
            time.sleep(0.05)
        assert cfg.file_path.exists()
    finally:
        heartbeat.stop(timeout=1.0)
    assert not heartbeat.is_running()


def test_file_channel_works_when_http_endpoint_down(tmp_path: Path) -> None:
    """Best-effort HTTP failure must not stop the file write."""
    cfg = _cfg(tmp_path)  # sentinel_url is 127.0.0.1:1 — unreachable
    heartbeat.write_once(cfg)
    assert cfg.file_path.exists()
