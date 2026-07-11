"""ProtocolLayer — composition root for Samus's v0.5/v0.6 governance.

Single construction point so individual workcells don't each instantiate
the ISV consumer / dual-channel mirror / template registry. Use as:

    from backend.governance.protocol_layer import get_protocol_layer
    layer = get_protocol_layer()
    layer.commit_commercial_action(
        action_class=...,
        action_payload=...,
        commercial_destination=...,
    )

Honest TODOs:
  - ISV-poll background task is started by ``start_background_tasks``; it
    is a no-op when SAMUS_OPTIMUS_DISPATCH_URL is unset (local-disk inbox
    is polled instead via IsvConsumer.poll_inbox()).
  - The layer is wrapped in try/except at module import so a broken
    governance boot does NOT block revenue-bearing workcells.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from backend.common.config import get_settings

from .commercial_wrap import (
    CommercialActionRefusal,
    RblBandConsumer,
    commit_commercial_action,
)
from .dual_channel import DualChannelMirror
from .efh_evaluator import EthicalFailureHandler
from .isv import IsvConsumer
from .templates import TemplateRegistry

_LOG = logging.getLogger("samus.governance.protocol_layer")


class ProtocolLayer:
    def __init__(self) -> None:
        settings = get_settings()
        self.settings = settings
        self.isv_consumer: IsvConsumer | None = None
        self.dual_channel: DualChannelMirror | None = None
        self.template_registry: TemplateRegistry | None = None
        self.rbl_consumer: RblBandConsumer | None = None
        self.efh: EthicalFailureHandler | None = None
        self._poll_task: asyncio.Task | None = None
        self._poll_thread: threading.Thread | None = None
        self._stop = threading.Event()

        try:
            self.efh = EthicalFailureHandler()
        except Exception as exc:  # noqa: BLE001
            _LOG.error("samus.protocol_layer.efh_init_failed: %s", exc)

        if settings.samus_isv_consumer_enabled:
            try:
                self.isv_consumer = IsvConsumer(
                    major_pubkey_path=settings.samus_major_public_key_path or None,
                    env=settings.env,
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.error("samus.protocol_layer.isv_init_failed: %s", exc)

        if settings.samus_dual_channel_enabled:
            try:
                self.dual_channel = DualChannelMirror(
                    optimus_dispatch_url=settings.samus_optimus_dispatch_url or None,
                    signing_key_path=settings.samus_signing_key_path or None,
                    timeout_sec=settings.samus_dual_channel_audit_timeout_sec,
                    env=settings.env,
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.error("samus.protocol_layer.dual_channel_init_failed: %s", exc)

        if settings.samus_template_registry_enabled:
            try:
                self.template_registry = TemplateRegistry()
            except Exception as exc:  # noqa: BLE001
                _LOG.error("samus.protocol_layer.templates_init_failed: %s", exc)

        try:
            self.rbl_consumer = RblBandConsumer(
                status_url=settings.samus_major_rbl_status_url or None,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.error("samus.protocol_layer.rbl_init_failed: %s", exc)

    # ------------------------------------------------------------------
    # Background ISV poll
    # ------------------------------------------------------------------
    async def _async_poll_loop(self, interval_sec: int = 60) -> None:
        while not self._stop.is_set():
            if self.isv_consumer is not None:
                try:
                    self.isv_consumer.poll_inbox()
                except Exception as exc:  # noqa: BLE001
                    _LOG.warning("samus.isv.poll_exc: %s", exc)
            try:
                await asyncio.sleep(interval_sec)
            except asyncio.CancelledError:
                break

    def start_background_tasks(self, *, interval_sec: int = 60) -> None:
        if self.isv_consumer is None:
            return
        try:
            loop = asyncio.get_running_loop()
            self._poll_task = loop.create_task(self._async_poll_loop(interval_sec))
        except RuntimeError:
            # No running loop (worker-process mode); use a thread.
            def _run() -> None:
                while not self._stop.is_set():
                    try:
                        if self.isv_consumer is not None:
                            self.isv_consumer.poll_inbox()
                    except Exception as exc:  # noqa: BLE001
                        _LOG.warning("samus.isv.poll_exc(thread): %s", exc)
                    self._stop.wait(interval_sec)
            self._poll_thread = threading.Thread(
                target=_run, name="samus-isv-poll", daemon=True,
            )
            self._poll_thread.start()

    def stop_background_tasks(self) -> None:
        self._stop.set()
        if self._poll_task is not None:
            self._poll_task.cancel()

    # ------------------------------------------------------------------
    # Commit pipeline
    # ------------------------------------------------------------------
    def commit_commercial_action(
        self,
        *,
        action_class: str,
        action_payload: dict,
        commercial_destination: str,
        trust_premium: float = 1.0,
    ) -> dict:
        return commit_commercial_action(
            action_class=action_class,
            action_payload=action_payload,
            commercial_destination=commercial_destination,
            isv_consumer=self.isv_consumer,
            template_registry=self.template_registry,
            efh_evaluator=self.efh,
            dual_channel=self.dual_channel,
            rbl_consumer=self.rbl_consumer,
            trust_premium=trust_premium,
        )

    def status(self) -> dict[str, Any]:
        return {
            "isv_consumer": self.isv_consumer is not None,
            "active_isv": (
                self.isv_consumer.get_active_isv().get("isv_id")
                if self.isv_consumer and self.isv_consumer.get_active_isv()
                else None
            ),
            "dual_channel": self.dual_channel is not None,
            "audit_blackout_frozen": (
                self.dual_channel.is_blackout_frozen()
                if self.dual_channel else False
            ),
            "template_registry": self.template_registry is not None,
            "loaded_templates": (
                self.template_registry.loaded_ids()
                if self.template_registry else []
            ),
            "rbl_band": (
                self.rbl_consumer.current_band()
                if self.rbl_consumer else "unknown"
            ),
        }


_LAYER: ProtocolLayer | None = None


def get_protocol_layer() -> ProtocolLayer:
    global _LAYER
    if _LAYER is None:
        try:
            _LAYER = ProtocolLayer()
        except Exception as exc:  # noqa: BLE001
            _LOG.error("samus.protocol_layer.construction_failed: %s", exc)
            # Construct a minimal fallback so callers don't NPE.
            _LAYER = ProtocolLayer.__new__(ProtocolLayer)
            _LAYER.isv_consumer = None
            _LAYER.dual_channel = None
            _LAYER.template_registry = None
            _LAYER.rbl_consumer = None
            _LAYER.efh = None
            _LAYER._stop = threading.Event()
            _LAYER._poll_task = None
            _LAYER._poll_thread = None
    return _LAYER


def reset_protocol_layer() -> None:
    """For tests: drop the singleton."""
    global _LAYER
    if _LAYER is not None:
        try:
            _LAYER.stop_background_tasks()
        except Exception:  # noqa: BLE001
            pass
    _LAYER = None


# Re-export the refusal exception for callers.
__all__ = [
    "ProtocolLayer",
    "get_protocol_layer",
    "reset_protocol_layer",
    "CommercialActionRefusal",
]
