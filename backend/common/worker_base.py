"""BaseSqsWorker — abstract per doc §3.26.

Subclasses set ``service`` and implement ``handle(envelope) -> dict``. The base
class owns the receive→validate→idempotency→handle→state→event→delete loop.
Run via ``python -m backend.<service>.worker`` or in-process via ``run_forever``.

The prior shell's BaseSqsWorker used a concrete ``handlers`` dict pattern; the
doc-shape is single-method abstract. The class signature change is breaking —
each workcell's ``worker.py`` must subclass and implement ``handle``.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import signal
import threading
import time
import traceback
from abc import ABC, abstractmethod
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import events, metrics
from .aws_runtime import AwsRuntime
from .dates import iso_now
from .idempotency import GLOBAL_IDEMPOTENCY_STORE, IdempotencyStore
from .persistence import JsonlLedger
from .queue_contracts import QueueEnvelope
from .queue_signing import check_message_authenticity
from .worker_action_budget import (
    ActionContext,
    BudgetExhausted,
    PollThrottle,
    get_budget,
)

_LOG = logging.getLogger("samus.worker")

# Daily rotation cadence + retention window for the per-worker audit ledger.
_ROTATE_INTERVAL_S = 24 * 3600
_ROTATE_RETENTION_HOURS = 24 * 30  # 30 days kept hot; older archived

# SQS long-poll window. The normal value (20s) maximises receive throughput
# and minimises empty-receive cost. On shutdown we want the poll to return
# fast so the worker can break the run_forever loop without waiting up to
# 20s for the in-flight long-poll — see _poll_wait_seconds().
_POLL_WAIT_LONG_S = 20
_POLL_WAIT_SHUTDOWN_S = 0

# Infinite-loop ceiling (HOTL Tranche 5). A handler that keeps failing is
# redriven by SQS; without an explicit bound a poison message can loop until
# the queue's own maxReceiveCount (which may be unset on a self-provisioned
# queue) finally sheds it. Once SQS's ApproximateReceiveCount reaches this
# ceiling the base worker force-routes the message to the DLQ and deletes it,
# so a single message can never be reprocessed unboundedly. Kept generous so
# it never fires before the transient-retry window (2xx/5xx blips) is spent.
MAX_HANDLER_ATTEMPTS = 5


def _audit_ledger(service: str) -> JsonlLedger:
    root = os.getenv("SAMUS_DATA_ROOT", "/opt/samus/data")
    return JsonlLedger(f"{root}/{service}/{service}_audit.jsonl")


class BaseSqsWorker(ABC):
    service: str = ""

    def __init__(
        self,
        runtime: AwsRuntime,
        *,
        idempotency_store: IdempotencyStore | None = None,
    ) -> None:
        if not self.service:
            raise RuntimeError("Worker subclass must set `service`.")
        self.runtime = runtime
        self.idempotency = idempotency_store or GLOBAL_IDEMPOTENCY_STORE
        self.stop_event = threading.Event()
        # Daily audit-ledger rotation cadence. ``_last_rotate_ts`` seeds to 0
        # so the first poll-loop tick performs a rotate immediately on boot;
        # subsequent rotates fire after ``_ROTATE_INTERVAL_S`` (24h). Retention
        # is 30 days — older records spill into a sibling ``.archive.jsonl``,
        # never deleted (cognition-preserving — see persistence.rotate_by_age).
        self._last_rotate_ts: float = 0.0
        # Hoist the audit ledger to __init__ — was previously rebuilt per
        # message (mkdir(exist_ok=True) syscall + Path construction on every
        # ``_process_message`` call). One construction at boot, reused for
        # the life of the worker.
        self._ledger: JsonlLedger = _audit_ledger(self.service)
        # Detect whether the subclass's handle() accepts a ``stop_event``
        # keyword. When it does, _process_message will pass self.stop_event
        # through so the handler can short-circuit between sub-steps on
        # shutdown. Old subclasses (handle(self, envelope)) keep working —
        # the keyword is suppressed for them.
        self._handle_accepts_stop_event: bool = self._detect_kwarg("stop_event")
        self._handle_accepts_action_context: bool = self._detect_kwarg("action_context")
        self._install_signal_handlers()

    @classmethod
    def _detect_kwarg(cls, name: str) -> bool:
        """Return True if this subclass's handle() accepts kwarg *name*."""
        try:
            sig = inspect.signature(cls.handle)
        except (TypeError, ValueError):
            return False
        params = sig.parameters
        if name in params:
            return True
        # **kwargs catch-all counts — caller can safely pass the kwarg.
        return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())

    def _install_signal_handlers(self) -> None:
        def _stop(_signum, _frame):
            self.stop_event.set()

        try:
            signal.signal(signal.SIGTERM, _stop)
            signal.signal(signal.SIGINT, _stop)
        except (ValueError, AttributeError):
            pass

    @abstractmethod
    def handle(
        self,
        envelope: QueueEnvelope,
        *,
        stop_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Process one envelope; return a result dict on success.

        ``stop_event`` is an additive shutdown signal: when set (SIGTERM /
        SIGINT received), the worker is asking the handler to wind down at
        the next safe checkpoint. Handlers that run long sub-steps (LLM
        calls, crawl loops, DAG executors) SHOULD consult
        ``stop_event.is_set()`` between sub-steps and return a partial /
        no-op result so the message can be returned to the queue cleanly.
        The kwarg is back-compat: subclasses defined as
        ``handle(self, envelope)`` keep working — the base class detects
        the signature at __init__ and only passes ``stop_event`` to
        subclasses that accept it.
        """

    # --- lifecycle ---------------------------------------------------------

    def run_forever(self) -> None:
        _LOG.info("worker_start", extra={"service": self.service})
        throttle = PollThrottle()
        while not self.stop_event.is_set():
            self._maybe_rotate_ledger()
            try:
                messages = self.runtime.receive_messages(
                    wait_time=self._poll_wait_seconds(),
                )
            except TypeError:
                # Older AwsRuntime stubs in tests may not accept wait_time;
                # fall back to the default signature so test doubles still work.
                try:
                    messages = self.runtime.receive_messages()
                except Exception as exc:
                    _LOG.warning("worker_receive_failed: %s", exc)
                    time.sleep(1.0)
                    continue
            except Exception as exc:
                _LOG.warning("worker_receive_failed: %s", exc)
                time.sleep(1.0)
                continue
            if not messages:
                extra_sleep = throttle.on_idle()
                if extra_sleep > 0 and not self.stop_event.is_set():
                    _LOG.debug(
                        "worker_idle_throttle",
                        extra={
                            "service": self.service,
                            "idle_count": throttle.idle_count,
                            "extra_sleep_s": extra_sleep,
                        },
                    )
                    self.stop_event.wait(extra_sleep)
                continue
            throttle.on_work()
            for msg in messages:
                if self.stop_event.is_set():
                    break
                self._process_message(msg)
        _LOG.info("worker_stop", extra={"service": self.service})

    def _poll_wait_seconds(self) -> int:
        """SQS long-poll wait window — short-circuits on shutdown.

        Normally returns 20 (the SQS-API maximum) so an empty queue costs
        one syscall every 20s instead of busy-spinning. Once ``stop_event``
        is set the loop is winding down and we want the in-flight receive
        to return immediately rather than burn the full 20s window — return
        0 so the next receive_message call is short-poll and breaks out.
        """
        if self.stop_event.is_set():
            return _POLL_WAIT_SHUTDOWN_S
        return _POLL_WAIT_LONG_S

    def _maybe_rotate_ledger(self) -> None:
        """Fire ``JsonlLedger.rotate_by_age`` at a daily cadence.

        Called from the poll loop; cheap when not due (one ``time.time()``
        compare). When due, archives audit records older than 30 days into a
        sibling ``.archive.jsonl`` — the active log stays bounded without
        evicting cognition. Failures are swallowed: rotation is a janitorial
        nice-to-have, never a reason to halt the worker.
        """
        now = time.time()
        if now - self._last_rotate_ts < _ROTATE_INTERVAL_S:
            return
        self._last_rotate_ts = now
        try:
            archived = self._ledger.rotate_by_age(max_age_hours=_ROTATE_RETENTION_HOURS)
            if archived:
                _LOG.info(
                    "worker_ledger_rotated",
                    extra={"service": self.service, "archived": archived},
                )
        except Exception as exc:  # noqa: BLE001 — janitorial, never fatal
            _LOG.warning("worker_ledger_rotate_failed: %s", exc)

    def _process_message(self, msg: dict[str, Any]) -> None:
        receipt = msg.get("ReceiptHandle", "")
        body = msg.get("Body", "")
        ledger = self._ledger

        # 1. validate body
        try:
            envelope = QueueEnvelope.model_validate_json(body)
        except Exception as exc:
            _LOG.warning("worker_poison: %s", exc)
            metrics.SAMUS_TASK_TOTAL.labels(self.service, "poison").inc()
            self.runtime.delete_message(receipt)
            return

        # 1b. per-message authenticity (FIN-03). SQS gives only IAM-level
        # trust; an HMAC over the envelope proves the message came from a
        # holder of the inter-service key, not merely from someone with
        # sqs:SendMessage. In audit mode (SAMUS_SQS_REQUIRE_HMAC off, default)
        # a mismatch is logged but processed; in enforce mode an unsigned /
        # invalid message is dropped fail-closed (deleted — never redriven, so
        # a forged message cannot loop into the DLQ for later reprocessing).
        if not check_message_authenticity(envelope, service=self.service):
            metrics.SAMUS_TASK_TOTAL.labels(self.service, "unauthenticated").inc()
            self.runtime.delete_message(receipt)
            return

        task_id = envelope.task_id
        action = envelope.action

        try:
            # 2. processing state
            try:
                self.runtime.task_state_table().put_item(
                    Item={"task_id": task_id, "status": "processing", "ts": iso_now()},
                )
            except Exception as exc:  # pragma: no cover — best-effort
                _LOG.warning("task_state put failed: %s", exc)

            # 3. idempotency claim
            idem_key = envelope.idempotency_key or f"{self.service}:{task_id}"
            if not self.idempotency.first_seen(idem_key):
                _LOG.info(
                    "worker_duplicate_suppressed",
                    extra={
                        "service": self.service,
                        "task_id": task_id,
                    },
                )
                metrics.SAMUS_TASK_TOTAL.labels(self.service, "deduplicated").inc()
                self.runtime.delete_message(receipt)
                return

            # 4. build per-job action context from registry / envelope override
            budget_val = get_budget(
                self.service,
                action,
                override=envelope.action_budget,
            )
            action_ctx = ActionContext(self.service, action, budget_val)

            # 5. handle — pass stop_event and/or action_context when the
            # subclass declares them. Legacy handle(self, envelope) callers
            # keep working — kwargs are suppressed for them (compat).
            kwargs: dict[str, Any] = {}
            if self._handle_accepts_stop_event:
                kwargs["stop_event"] = self.stop_event
            if self._handle_accepts_action_context:
                kwargs["action_context"] = action_ctx
            result = self.handle(envelope, **kwargs) or {}

            # 6. completed state + SNS event + delete
            try:
                self.runtime.task_state_table().put_item(
                    Item={
                        "task_id": task_id,
                        "status": "completed",
                        "result": result,
                        "ts": iso_now(),
                    }
                )
            except Exception as exc:  # pragma: no cover
                _LOG.warning("task_state put failed: %s", exc)
            try:
                self.runtime.publish_event(
                    "task.completed",
                    {"service": self.service, "task_id": task_id, "action": action},
                )
            except Exception as exc:  # pragma: no cover
                _LOG.warning("sns publish failed: %s", exc)

            audit = events.build_audit_event(
                service=self.service,
                task_id=task_id,
                action=action,
                input_payload=envelope.payload,
                output_payload=result,
                status="completed",
            )
            try:
                ledger.append(audit)
            except OSError:
                pass

            self.runtime.delete_message(receipt)
            metrics.SAMUS_TASK_TOTAL.labels(self.service, "completed").inc()

        except BudgetExhausted as exc:
            # Handler ran out of action tokens — not an error, not a retry.
            # Record the partial result, delete the message (no DLQ), and emit
            # a distinct metric so operators can tune budgets if they see this
            # firing frequently on productive actions.
            _LOG.warning(
                "worker_budget_exhausted",
                extra={
                    "service": self.service,
                    "task_id": task_id,
                    "action": action,
                    "budget": exc.budget,
                    "used": exc.used,
                },
            )
            metrics.SAMUS_TASK_TOTAL.labels(self.service, "budget_exhausted").inc()
            try:
                self.runtime.task_state_table().put_item(
                    Item={
                        "task_id": task_id,
                        "status": "budget_exhausted",
                        "error": str(exc),
                        "ts": iso_now(),
                    }
                )
            except Exception:  # pragma: no cover
                pass
            audit = events.build_audit_event(
                service=self.service,
                task_id=task_id,
                action=action,
                input_payload=envelope.payload,
                output_payload={"budget_exhausted": True, "used": exc.used, "budget": exc.budget},
                status="budget_exhausted",
            )
            try:
                ledger.append(audit)
            except OSError:
                pass
            self.runtime.delete_message(receipt)

        except Exception as exc:
            stack = traceback.format_exc()
            _LOG.error("worker_handler_failed: %s\n%s", exc, stack)
            metrics.SAMUS_TASK_TOTAL.labels(self.service, "failed").inc()
            try:
                self.runtime.task_state_table().put_item(
                    Item={
                        "task_id": task_id,
                        "status": "failed",
                        "error": str(exc),
                        "ts": iso_now(),
                    }
                )
            except Exception:  # pragma: no cover
                pass
            try:
                self.runtime.publish_event(
                    "task.failed",
                    {
                        "service": self.service,
                        "task_id": task_id,
                        "action": action,
                        "error": str(exc),
                    },
                )
            except Exception:  # pragma: no cover
                pass
            audit = events.build_audit_event(
                service=self.service,
                task_id=task_id,
                action=action,
                input_payload=envelope.payload,
                output_payload={"error": str(exc)},
                status="failed",
            )
            try:
                ledger.append(audit)
            except OSError:
                pass
            # Infinite-loop ceiling (HOTL T5): once this message has been
            # received MAX_HANDLER_ATTEMPTS times it is force-routed to the DLQ
            # and deleted, so a poison message cannot redrive unboundedly even
            # if the queue has no maxReceiveCount configured. Below the ceiling
            # we do NOT delete — normal SQS redrive gives transient failures
            # their retries.
            if self._receive_count(msg) >= MAX_HANDLER_ATTEMPTS:
                self._shed_to_dlq(envelope, task_id=task_id, error=str(exc))
                self.runtime.delete_message(receipt)
                metrics.SAMUS_TASK_TOTAL.labels(self.service, "retry_exhausted").inc()

    @staticmethod
    def _receive_count(msg: dict[str, Any]) -> int:
        """SQS ApproximateReceiveCount for this delivery (1 when absent)."""
        try:
            raw = (msg.get("Attributes") or {}).get("ApproximateReceiveCount")
            return int(raw) if raw is not None else 1
        except (TypeError, ValueError):
            return 1

    def _shed_to_dlq(self, envelope: QueueEnvelope, *, task_id: str, error: str) -> None:
        """Persist a retry-exhausted message to the DLQ ledger (best-effort)."""
        try:
            from . import dlq

            dlq.enqueue_failure(
                self.service,
                task_id=task_id,
                target=self.service,
                payload=envelope.payload,
                error=f"retry ceiling ({MAX_HANDLER_ATTEMPTS}) reached: {error}",
                attempt=MAX_HANDLER_ATTEMPTS,
            )
        except Exception as exc:  # noqa: BLE001 — shedding is best-effort
            _LOG.warning("worker_dlq_shed_failed service=%s: %s", self.service, exc)


# --- health server (Cloud Run startup-probe — doc CLOUD_RUN_DEPLOY.md W-3) ---


class _HealthRequestHandler(BaseHTTPRequestHandler):
    """Trivial ``GET /health`` responder for the Cloud Run startup probe.

    A Cloud Run service must open a TCP listener on ``$PORT`` within the
    startup window or the revision is marked unhealthy. Workers run
    ``run_forever`` (an SQS poll loop) and otherwise never bind a port —
    this handler is the thin HTTP surface that satisfies the probe. The
    concrete subclass carries the ``service`` name; see ``start_health_server``.
    """

    service: str = ""

    def do_GET(self) -> None:  # noqa: N802 — http.server API name
        if self.path.rstrip("/") in ("", "/health"):
            body = json.dumps({"status": "ok", "service": self.service}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *_args: Any) -> None:
        """Silence http.server's default per-request stderr logging."""


def start_health_server(port: int, service: str) -> ThreadingHTTPServer:
    """Start a daemon-thread HTTP health server on ``port`` and return it.

    Called by :func:`serve_worker` only when ``$PORT`` is set (Cloud Run
    injects it). The server runs in a daemon thread so it never blocks
    process shutdown; ``serve_worker`` also calls ``shutdown()`` explicitly.
    Binding ``0.0.0.0`` is required for Cloud Run to reach the container.
    """
    handler = type("_HealthHandler", (_HealthRequestHandler,), {"service": service})
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    thread = threading.Thread(
        target=httpd.serve_forever,
        name=f"samus-worker-health-{service}",
        daemon=True,
    )
    thread.start()
    _LOG.info(
        "worker_health_server_listening",
        extra={"service": service, "port": httpd.server_address[1]},
    )
    return httpd


def serve_worker(worker: BaseSqsWorker) -> None:
    """Module entrypoint for ``python -m backend.<service>.worker``."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}',
    )
    # Cloud Run requires every service — workers included — to bind ``$PORT``
    # within the startup window. Workers have no HTTP surface of their own,
    # so when ``$PORT`` is present (Cloud Run injects it) start a trivial
    # health server beside the poll loop. Locally (Docker Compose) ``$PORT``
    # is unset and this is skipped, leaving the local stack unchanged.
    # See CLOUD_RUN_DEPLOY.md §2.5 / work-item W-3.
    health_server: ThreadingHTTPServer | None = None
    port_raw = os.getenv("PORT")
    if port_raw:
        try:
            health_server = start_health_server(int(port_raw), worker.service)
        except (OSError, ValueError) as exc:
            # On Cloud Run a failure to bind $PORT dooms the revision anyway;
            # surface it loudly rather than silently running portless.
            _LOG.error("worker_health_server_failed: %s", exc)
            raise
    try:
        worker.run_forever()
    finally:
        if health_server is not None:
            health_server.shutdown()
            health_server.server_close()
