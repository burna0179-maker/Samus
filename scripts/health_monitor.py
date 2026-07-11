"""Bare-metal Samus health monitor.

Checks the gateway's `/health` endpoint and (optionally) Major's `/health`
endpoint, records every status transition to the audit ledger, persists
state between runs at `logs/health_state.json`.

Designed to run from Windows Task Scheduler every 5 minutes via:
  D:\\Hustleforge\\Samus\\.venv\\Scripts\\python.exe -m scripts.health_monitor
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from backend.common import audit
from backend.common.settings import bootstrap_settings

_LOG = logging.getLogger("samus.health_monitor")
_STATE_PATH = Path("logs/health_state.json")
_HTTP_TARGETS = [
    ("gateway", "http://localhost:8080/health"),
]


def _load_state() -> dict[str, Any]:
    if _STATE_PATH.exists():
        try:
            return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_state(state: dict[str, Any]) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state, default=str, indent=2), encoding="utf-8")


def _check_http(_name: str, url: str) -> bool:
    try:
        return httpx.get(url, timeout=3.0).status_code == 200
    except Exception:
        return False


def main() -> None:
    state = _load_state()
    s = bootstrap_settings()

    for name, url in _HTTP_TARGETS:
        ok = _check_http(name, url)
        per = state.setdefault(f"http:{name}", {"last_status": "ok"})
        prev = per["last_status"]
        new = "ok" if ok else "down"
        if prev != new:
            audit.record("health.transition", target=name, from_=prev, to=new, url=url)
        per["last_status"] = new

    # Best-effort Major heartbeat probe â failure is silent because Major may
    # not exist yet during early agent-skeleton bring-up.
    if s.security_sentinel_url:
        target_url = f"{s.security_sentinel_url.rstrip('/')}/health"
        major_ok = _check_http("major", target_url)
        per = state.setdefault("http:major", {"last_status": "ok"})
        prev = per["last_status"]
        new = "ok" if major_ok else "down"
        if prev != new:
            audit.record("health.transition", target="major", from_=prev, to=new, url=target_url)
        per["last_status"] = new

    _save_state(state)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    main()
