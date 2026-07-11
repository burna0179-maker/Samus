"""Filesystem watchdog for auto-reloading the Codex registry.

Polls `docs/codex/` (chapters + `_drafts/`) at a fixed interval and triggers
`REGISTRY.reload()` when a modification is detected. Polling is the
canonical path — no `watchdog` pip dependency is required. If the
`watchdog` package is installed it will be preferred (real inotify-style
events); otherwise the polling thread handles it.

Reload semantics:

* Boot-time load is fail-CLOSED (`app_factory._ensure_codex_loaded`).
* Hot-reload here is fail-OPEN: a parse error during a watcher-triggered
  reload leaves the previously-loaded registry in place and logs loudly.
  Rationale: a half-written edit must not yank the rule set out from
  under a running workcell.

Enable / disable: env `SAMUS_CODEX_AUTO_RELOAD` (default `"1"`). Set to
`"0"` to skip watcher startup entirely.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .registry import REGISTRY, CodexRegistry, _default_codex_dir


_LOG = logging.getLogger("samus.codex.watchdog")

_DEFAULT_POLL_INTERVAL_SECONDS = 60.0
_ENV_ENABLED = "SAMUS_CODEX_AUTO_RELOAD"
_ENV_INTERVAL = "SAMUS_CODEX_AUTO_RELOAD_INTERVAL"


@dataclass
class WatcherHandle:
    """Opaque handle returned by :func:`start_codex_watcher`."""

    thread: threading.Thread
    stop_event: threading.Event
    codex_dir: Path
    poll_interval: float
    backend: str
    reload_count: int = 0
    last_error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def is_alive(self) -> bool:
        return self.thread.is_alive()


def _watcher_enabled() -> bool:
    raw = os.getenv(_ENV_ENABLED, "1").strip().lower()
    return raw in ("1", "true", "yes", "on", "y")


def _poll_interval() -> float:
    raw = os.getenv(_ENV_INTERVAL, "").strip()
    if not raw:
        return _DEFAULT_POLL_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_POLL_INTERVAL_SECONDS
    return max(0.1, value)


def _snapshot_mtimes(codex_dir: Path) -> dict[str, float]:
    """Return {relative_path: mtime} for every .md / .gitkeep file under codex_dir."""
    snapshot: dict[str, float] = {}
    if not codex_dir.is_dir():
        return snapshot
    for path in codex_dir.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in (".md", ""):
            # We track .md chapters/drafts and any sentinel files (.gitkeep).
            continue
        try:
            snapshot[str(path.relative_to(codex_dir))] = path.stat().st_mtime
        except OSError:
            continue
    return snapshot


def _attempt_reload(
    handle: WatcherHandle,
    registry: CodexRegistry,
    codex_dir: Path,
) -> None:
    try:
        registry.reload(codex_dir)
    except Exception as exc:  # noqa: BLE001 — fail-OPEN: log + keep old registry
        with handle._lock:
            handle.last_error = f"{type(exc).__name__}: {exc}"
        _LOG.error(
            "samus.codex.watchdog.reload_failed: dir=%s err=%s — keeping "
            "previously-loaded registry in place (fail-OPEN)",
            codex_dir, exc,
        )
        return
    with handle._lock:
        handle.reload_count += 1
        handle.last_error = None
    _LOG.info(
        "samus.codex.watchdog.reload_ok: dir=%s guardrails=%d adrs=%d",
        codex_dir,
        len(registry.guardrails()),
        len(registry.adrs()),
    )


def _polling_loop(
    handle: WatcherHandle,
    registry: CodexRegistry,
    codex_dir: Path,
    on_iteration: Callable[[], None] | None = None,
    initial_snapshot: dict[str, float] | None = None,
) -> None:
    # Caller may pass a pre-thread-start snapshot to eliminate the race
    # where the test (or any fast caller) modifies a file before the thread
    # gets scheduled and takes its first mtime baseline.
    last = initial_snapshot if initial_snapshot is not None else _snapshot_mtimes(codex_dir)
    while not handle.stop_event.is_set():
        if handle.stop_event.wait(timeout=handle.poll_interval):
            break
        try:
            current = _snapshot_mtimes(codex_dir)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning(
                "samus.codex.watchdog.snapshot_failed: dir=%s err=%s",
                codex_dir, exc,
            )
            current = last
        if current != last:
            _LOG.info(
                "samus.codex.watchdog.change_detected: dir=%s "
                "tracked_files=%d", codex_dir, len(current),
            )
            _attempt_reload(handle, registry, codex_dir)
            last = current
        if on_iteration is not None:
            on_iteration()


def start_codex_watcher(
    codex_dir: Path | None = None,
    *,
    registry: CodexRegistry | None = None,
    poll_interval: float | None = None,
    force: bool = False,
) -> WatcherHandle | None:
    """Start a daemon thread that watches the codex dir and reloads on change.

    Returns the :class:`WatcherHandle` or ``None`` if the watcher is disabled
    via ``SAMUS_CODEX_AUTO_RELOAD=0`` (and ``force`` is False).
    """
    if not force and not _watcher_enabled():
        _LOG.info(
            "samus.codex.watchdog.disabled: env %s=0 — skipping watcher",
            _ENV_ENABLED,
        )
        return None
    target_dir = codex_dir or _default_codex_dir()
    target_registry = registry or REGISTRY
    interval = poll_interval if poll_interval is not None else _poll_interval()
    stop_event = threading.Event()
    handle = WatcherHandle(
        thread=threading.Thread(),  # placeholder, replaced below
        stop_event=stop_event,
        codex_dir=target_dir,
        poll_interval=interval,
        backend="polling",
    )
    # Take the baseline mtime snapshot SYNCHRONOUSLY before the thread is
    # scheduled, so a caller that modifies the codex immediately after this
    # function returns can never race the watcher's first read.
    baseline = _snapshot_mtimes(target_dir)
    thread = threading.Thread(
        target=_polling_loop,
        args=(handle, target_registry, target_dir),
        kwargs={"initial_snapshot": baseline},
        name="samus-codex-watchdog",
        daemon=True,
    )
    handle.thread = thread
    thread.start()
    _LOG.info(
        "samus.codex.watchdog.started: dir=%s interval=%.2fs backend=%s",
        target_dir, interval, handle.backend,
    )
    return handle


def stop_codex_watcher(handle: WatcherHandle | None, timeout: float = 5.0) -> None:
    if handle is None:
        return
    handle.stop_event.set()
    handle.thread.join(timeout=timeout)
    if handle.thread.is_alive():
        _LOG.warning(
            "samus.codex.watchdog.stop_timeout: thread did not exit within %.1fs",
            timeout,
        )
    else:
        _LOG.info(
            "samus.codex.watchdog.stopped: reload_count=%d last_error=%s",
            handle.reload_count, handle.last_error,
        )


__all__ = [
    "WatcherHandle",
    "start_codex_watcher",
    "stop_codex_watcher",
]
