"""``create_base_app()`` — canonical FastAPI bootstrap per doc §3.1.

Composes:
- ``CorrelationMiddleware`` — ``X-Samus-Trace-Id`` propagation via ContextVar.
- ``MetricsMiddleware`` — ``samus_http_requests_total`` + ``..._duration_seconds``.
- ``VerifyHMACMiddleware`` — timestamp + nonce + HMAC verification on every
  inbound request (default ON as of the 2026-05-19 boundary-contract flip).
  Workcells that legitimately serve public unsigned traffic (Stripe webhook,
  Vapi webhook, AWS SNS feedback, intake form receiver) either:
    (a) pass ``add_hmac_middleware=False`` if the workcell is *exclusively*
        public-facing, or
    (b) pass ``hmac_exempt_paths=("/stripe_webhook", ...)`` to carve out the
        specific paths whose handlers already enforce their own signature
        scheme. The handler is then the boundary contract for those paths.
- ``GET /health`` and ``GET /metrics`` endpoints.

Default-on rationale: the audit reviewer found that every workcell that
forgot to call ``app.add_middleware(VerifyHMACMiddleware)`` was reachable
without HMAC verification — an opt-in boundary is a boundary that's off by
default. Flip the default. Tests opt out via the
``SAMUS_DISABLE_HMAC_MIDDLEWARE`` env var which conftest.py sets globally so
the existing unsigned-request test suite still passes.

Fail-closed behaviour: in non-development environments, calling this with
HMAC middleware enabled but ``shared_hmac_key`` empty raises ``RuntimeError``
at app construction. Development keeps the per-request 503 path so local
work / tests aren't blocked by an unset key. The gateway workcell sets its
own ``require_env`` after this call as a belt-and-suspenders check.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Iterable

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import correlation
from .config import get_settings
from .logging import configure_logging
from .metrics import metrics_response
from .body_size_limit import BodySizeLimitMiddleware
from .middleware import (
    CorrelationMiddleware,
    IdempotencyMiddleware,
    MetricsMiddleware,
    VerifyHMACMiddleware,
)
from .security_headers import SecurityHeadersMiddleware

_LOG = logging.getLogger("samus.app_factory")

# Workcells that are exclusively the *signer* side of the inter-service mesh
# (the gateway dispatches signed requests to other workcells but does not
# accept signed requests from them). Mirrors VerifyHMACMiddleware's
# ``exempt_services`` default. Listed here so the fail-closed startup check
# below knows not to require ``shared_hmac_key`` for gateway-only apps.
_HMAC_SIGNER_SERVICES: frozenset[str] = frozenset({"gateway"})


def _ensure_codex_loaded(workcell_name: str) -> None:
    """Codex Validation Layer boot (chapter 12 / ADR-011).

    Parse docs/codex/ once per app construction. Idempotent across
    workcells in the same process. Fail-CLOSED: if the Codex cannot be
    parsed, the workcell refuses to boot rather than start with rule
    enforcement silently disabled.
    """
    try:
        from .codex import REGISTRY
    except ImportError:
        _LOG.error(
            "samus.app_factory.codex_import_failed: workcell=%s — "
            "Codex package missing, refusing to boot.",
            workcell_name,
        )
        raise
    if REGISTRY.is_loaded():
        return
    try:
        REGISTRY.load()
    except Exception as exc:  # noqa: BLE001
        _LOG.error(
            "samus.app_factory.codex_load_failed: workcell=%s reason=%s "
            "— refusing to boot. Fix the Codex parse error, do not bypass.",
            workcell_name,
            exc,
        )
        raise


_CODEX_WATCHER_STARTED = False


def _start_codex_watcher_if_enabled(workcell_name: str) -> None:
    """Start the Codex auto-reload watcher exactly once per process.

    Honors ``SAMUS_CODEX_AUTO_RELOAD`` (default on). Fail-OPEN: a watcher
    startup failure logs at WARNING and does NOT abort boot — the Codex
    is already loaded (fail-CLOSED) by ``_ensure_codex_loaded`` above, so
    the workcell is safe to run without hot-reload.
    """
    global _CODEX_WATCHER_STARTED
    if _CODEX_WATCHER_STARTED:
        return
    try:
        from .codex.watchdog import start_codex_watcher
    except ImportError as exc:
        _LOG.warning(
            "samus.app_factory.codex_watcher_unavailable: workcell=%s err=%s",
            workcell_name,
            exc,
        )
        return
    try:
        handle = start_codex_watcher()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(
            "samus.app_factory.codex_watcher_start_failed: workcell=%s err=%s",
            workcell_name,
            exc,
        )
        return
    if handle is not None:
        _CODEX_WATCHER_STARTED = True


def _hmac_disabled_for_tests() -> bool:
    """Honour the conftest-set env var that disables HMAC for the test suite.

    Set ``SAMUS_DISABLE_HMAC_MIDDLEWARE=1`` in conftest.py to opt the entire
    pytest process out of HMAC verification. Production / Cloud Run / Compose
    never set this env var, so the default-on posture stands there.
    """
    raw = os.getenv("SAMUS_DISABLE_HMAC_MIDDLEWARE", "").strip().lower()
    return raw in ("1", "true", "yes", "on", "y")


def create_base_app(
    *,
    service_name: str | None = None,
    lifespan: Callable[[FastAPI], Any] | None = None,
    add_hmac_middleware: bool = True,
    hmac_exempt_paths: Iterable[str] | None = None,
    add_idempotency_middleware: bool = True,
    idempotency_exempt_paths: Iterable[str] | None = None,
) -> FastAPI:
    """Build a FastAPI app with the canonical Samus middleware + endpoints.

    ``lifespan`` is the FastAPI lifespan async context manager (the modern
    replacement for ``@app.on_event("startup"|"shutdown")``). Workcells that
    need startup/shutdown work pass one in; everything else leaves it None.

    ``add_hmac_middleware`` defaults to True — every workcell gets HMAC
    verification on inbound requests unless it explicitly opts out (e.g. a
    pure public-webhook workcell that uses a different signature scheme).
    When ``False``, a warning is logged so the operator can see in the boot
    logs which workcells run without the middleware.

    ``hmac_exempt_paths`` adds workcell-specific paths to the middleware's
    exempt set (on top of the always-exempt ``/health`` and ``/metrics``).
    Use for endpoints that already enforce their own auth scheme — Stripe
    signs ``/stripe_webhook`` with ``Stripe-Signature``, Vapi signs
    ``/vapi/webhook`` with ``X-Vapi-Signature``, AWS SNS signs
    ``/api/ses/feedback`` via X.509 ``SigningCert``, and ``/intake/onboarding``
    is a public form receiver. Pass these explicitly so the carve-out is
    visible at the call site, not silent.
    """
    settings = get_settings()
    name = service_name or settings.service_name or "samus"
    configure_logging(settings.log_level)

    _ensure_codex_loaded(name)
    _start_codex_watcher_if_enabled(name)

    # Runtime flag store: install the process singleton + register the catalog
    # so is_enabled(...) honours persisted set_flag flips in EVERY workcell.
    # Before 2026-07-03 only prospecting's lifespan initialised the store, so
    # an operator flip was silently ignored by every other service (each fell
    # back to its boot-time env value — discovered when arming self_supply on
    # the gateway had no effect). Guarded: never replaces a store a test
    # installed, never runs under pytest (unit tests must not read the real
    # persisted flips), and a fault never blocks a workcell boot.
    if not os.getenv("PYTEST_CURRENT_TEST"):
        try:
            from backend.common.flags import store as _flag_store
            from backend.common.flags.catalog import register_all

            if _flag_store._REGISTRY is None:  # noqa: SLF001 — boot-orchestrator check
                _flag_store.init_default_store()
            register_all()
        except Exception:  # noqa: BLE001 — flags must never block a workcell boot
            logging.getLogger("samus.app_factory").exception(
                "flag store init failed; runtime flag flips will not be honoured",
            )

    app = FastAPI(title=f"samus-{name}", lifespan=lifespan)
    # Starlette runs middleware OUTERMOST-FIRST in reverse add order, so the
    # last-added middleware is the outermost. Inner layers first:
    app.add_middleware(MetricsMiddleware, service_name=name)
    app.add_middleware(CorrelationMiddleware)
    # S5: browser-hardening headers on every response. Inert on the M2M JSON
    # mesh (HMAC clients ignore response headers); protective on the gateway's
    # operator-console SPA surface. Added here so it wraps the handler +
    # correlation but sits inside the body-size cap below.
    app.add_middleware(SecurityHeadersMiddleware)

    if add_idempotency_middleware and not _hmac_disabled_for_tests():
        extra_idem_exempt = frozenset(idempotency_exempt_paths or ())
        app.add_middleware(
            IdempotencyMiddleware,
            workcell=name,
            exempt_paths=extra_idem_exempt,
        )

    if add_hmac_middleware:
        if _hmac_disabled_for_tests():
            # Test-suite escape hatch. Honoured ONLY when the env var is set
            # (conftest.py is the sole intended setter). Production never
            # reads this branch.
            _LOG.debug(
                "samus.app_factory.hmac_test_disabled: workcell=%s — "
                "SAMUS_DISABLE_HMAC_MIDDLEWARE set, skipping VerifyHMACMiddleware",
                name,
            )
        else:
            # Fail-closed at construction. In non-development we refuse to
            # boot a workcell that advertises HMAC verification but has no
            # key material at all — silent no-op would defeat the boundary
            # contract. Gateway is the signer-side exception (its inbound
            # surface is the operator console, gated separately by bearer
            # auth).
            #
            # R-1 (2026-05-20): "no key material" now means BOTH the shared
            # key AND every per-service key are empty. A workcell verifies
            # the CALLER's key (per-service with shared fallback), so a stack
            # that has migrated fully to per-service keys can legitimately
            # leave SAMUS_SHARED_HMAC_KEY empty — refusing to boot in that
            # configuration would be a false positive. The check stays
            # fail-closed: if there is genuinely no key anywhere, no caller
            # could ever be verified, so boot is still refused.
            _has_any_key = bool(settings.shared_hmac_key or settings.per_service_hmac_keys)
            if (
                not _has_any_key
                and settings.env != "development"
                and name not in _HMAC_SIGNER_SERVICES
            ):
                raise RuntimeError(
                    "samus.app_factory.hmac_key_missing: workcell="
                    f"{name} env={settings.env} — refusing to boot with "
                    "VerifyHMACMiddleware enabled but no HMAC key material "
                    "(neither SAMUS_SHARED_HMAC_KEY nor any "
                    "SAMUS_HMAC_KEY_<SERVICE>) configured. Set a key or pass "
                    "add_hmac_middleware=False explicitly."
                )

            extra_exempt = tuple(hmac_exempt_paths or ())
            app.add_middleware(
                VerifyHMACMiddleware,
                extra_exempt_paths=extra_exempt,
            )
    else:
        _LOG.warning(
            "samus.app_factory.hmac_disabled: workcell=%s launched WITHOUT "
            "HMAC middleware — caller must enforce",
            name,
        )

    # S4: request-body ceiling. Added LAST so it is the OUTERMOST middleware —
    # the cap runs BEFORE VerifyHMACMiddleware buffers the body for signature
    # verification, so an oversized body is rejected with 413 before any
    # workcell allocates it. Env-tunable via SAMUS_MAX_REQUEST_BODY_BYTES
    # (default 1 MiB); a cap of 0 opts out. GET/HEAD/OPTIONS (incl. /health,
    # /metrics) short-circuit untouched.
    app.add_middleware(BodySizeLimitMiddleware)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "service": name, "ready": True}

    @app.get("/metrics")
    async def prometheus_metrics():
        return metrics_response()

    # Finding L4 (2026-05-20): generic 500-class exception handler.
    #
    # Before this, an UNHANDLED exception bubbling out of a route handler was
    # rendered by Starlette's default 500 page — and many workcells also do
    # ``raise HTTPException(detail=f"...: {exc}")`` deliberately, but the
    # *unhandled* path leaked the raw ``str(exc)`` (Pydantic field values,
    # store error semantics, absolute filesystem paths) straight to the
    # caller. That is useful recon, sometimes on unauthenticated paths.
    #
    # This handler catches anything that is NOT an HTTPException (FastAPI
    # already has dedicated handlers for HTTPException and
    # RequestValidationError, and those responses are intentionally generic /
    # structured — we do NOT override them). It returns a generic client
    # message plus the correlation/trace id so the operator can grep the
    # server logs for the full detail, which is logged here at ERROR with the
    # stack trace. The trace id is the only thing the client sees.
    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        trace_id = correlation.ensure_trace_id()
        _LOG.error(
            "samus.unhandled_exception: service=%s method=%s path=%s trace_id=%s exc_type=%s",
            name,
            request.method,
            request.url.path,
            trace_id,
            type(exc).__name__,
            exc_info=exc,
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "message": (
                    "An internal error occurred. Quote the trace id to the operator for diagnosis."
                ),
                "trace_id": trace_id,
            },
            # Echo the trace id on the same header CorrelationMiddleware uses
            # so a client / log correlation works even on the error path.
            headers={CorrelationMiddleware.HEADER: trace_id},
        )

    return app
