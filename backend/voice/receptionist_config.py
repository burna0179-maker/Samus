"""Per-client AI Digital Receptionist configuration — file-backed loader.

Pilot storage is a hand-edited YAML file per client at
``<SAMUS_ARTIFACT_ROOT>/customers/<slug>/receptionist/config.yaml``. This
module is the ONLY reader: swapping to a DynamoDB-backed store later means
reimplementing these three functions and nothing else — every caller depends
on the :class:`ReceptionistConfig` model, not on the file layout.

``resolve_customer_for_number`` is on the inbound-call hot path (called once
per Vapi end-of-call webhook), so the active-config list is cached for
``_CACHE_TTL_S`` — short enough that an operator editing a config sees it
take effect within seconds, long enough that a burst of calls is one sweep.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

import yaml

from backend.common import storage
from backend.common.config import get_settings
from backend.common.http_client import signed_post_json_sync

from .models import ReceptionistConfig


_LOG = logging.getLogger("samus.voice.receptionist_config")


# ---------------------------------------------------------------------------
# FIN-08 — stripe_customer_id validation (fail-closed metering gate)
# ---------------------------------------------------------------------------
#
# ``stripe_customer_id`` is read from an operator-editable YAML and forwarded
# into the finance ``/meter-event`` dispatch. A typo / stale / wrong id would
# bill ANOTHER customer's meter. So the loader validates the id ONCE per
# config-load through a dependency-injected validator; on an invalid / unknown
# / error result it disables metering for that config (the call is still
# logged, just never billed to the wrong customer).
#
# The validator is injected (see ``set_stripe_customer_validator``) so tests
# can mock it without hard-calling Stripe. The DEFAULT validator asks the
# finance workcell — which is the one container holding the Stripe secret;
# voice does not — to confirm the customer exists, mirroring the signed-HMAC
# cross-service call pattern the inbound caller already uses for metering.
#
# Contract: a validator returns ``(True, "")`` when the id is confirmed valid,
# and ``(False, reason)`` for invalid / unknown / unreachable / error — every
# non-confirmed outcome FAILS CLOSED (metering disabled), so a finance outage
# never silently re-enables billing against an unverified id.
#
# A validator returning ``(False, ...)`` is a confirmed-bad / unverifiable id;
# raising is treated identically (caught here -> disabled).

# (config, stripe_customer_id) -> (is_valid, reason_if_invalid)
StripeCustomerValidator = Callable[[ReceptionistConfig, str], "tuple[bool, str]"]


def _default_stripe_customer_validator(
    config: ReceptionistConfig,
    stripe_customer_id: str,
) -> tuple[bool, str]:
    """Confirm ``stripe_customer_id`` with the finance workcell, fail-closed.

    Finance holds the Stripe secret key (voice does not), so the existence
    check is delegated there over a signed-HMAC POST — the same channel the
    inbound caller uses to report metered usage. Finance is expected to expose
    ``POST /customer-exists`` returning ``{"exists": bool}`` for a given
    ``stripe_customer_id``.

    Fails closed: a missing finance url, a missing HMAC key, a network error,
    a non-2xx, an unparseable body, or ``exists != True`` all return
    ``(False, reason)`` -> metering disabled. Only an explicit
    ``{"exists": true}`` returns ``(True, "")``.

    NOTE (FIN-08 proposal): the finance-side ``POST /customer-exists`` route is
    NOT yet wired (see module docstring / commit message). Until it is, this
    default validator fails closed on every unset/unreachable finance dep —
    metering stays disabled rather than billing an unverified id. The gate and
    the disable path ship now; the finance route is the proposed integration.
    """
    cid = (stripe_customer_id or "").strip()
    if not cid:
        # Empty id is "no metering configured", not "invalid" — let the empty
        # check in _report_usage handle it (a benign skip, not a disable).
        return True, ""
    settings = get_settings()
    finance_url = settings.gateway_urls.get("finance")
    if not finance_url:
        return False, "finance_url_unset"
    if not getattr(settings, "shared_hmac_key", ""):
        return False, "shared_hmac_key_unset"
    try:
        resp = signed_post_json_sync(
            finance_url,
            "/customer-exists",
            {"stripe_customer_id": cid},
            retries=1,
        )
    except Exception as exc:  # noqa: BLE001 — network / circuit / 5xx
        _LOG.warning(
            "stripe customer validation dispatch failed for %s: %s", config.customer_slug, exc
        )
        return False, f"validation_dispatch_error: {exc}"
    if resp.status_code >= 400:
        return False, f"validation_http_{resp.status_code}"
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — malformed peer body
        return False, "validation_unparseable_response"
    if isinstance(body, dict) and body.get("exists") is True:
        return True, ""
    return False, "stripe_customer_not_found"


# Module-level injectable validator. Tests / future wiring swap this out.
_stripe_customer_validator: StripeCustomerValidator = _default_stripe_customer_validator


def set_stripe_customer_validator(
    validator: StripeCustomerValidator | None,
) -> None:
    """Inject the stripe_customer_id validator (dependency injection for tests).

    Passing ``None`` restores the default finance-validation validator.
    """
    global _stripe_customer_validator
    _stripe_customer_validator = validator or _default_stripe_customer_validator


def _validate_and_gate_metering(config: ReceptionistConfig) -> ReceptionistConfig:
    """Validate ``stripe_customer_id`` once and fail-closed on a bad id.

    Run at config-load time (one validation per load, cached with the config
    object). An empty id is left alone (metering simply skipped downstream).
    A non-empty id that the validator cannot confirm flips
    ``metering_disabled=True`` with a reason — the inbound caller then refuses
    to dispatch a meter event for this client.
    """
    cid = (config.stripe_customer_id or "").strip()
    if not cid:
        return config
    try:
        ok, reason = _stripe_customer_validator(config, cid)
    except Exception as exc:  # noqa: BLE001 — a raising validator fails closed
        _LOG.warning("stripe customer validator raised for %s: %s", config.customer_slug, exc)
        ok, reason = False, f"validator_error: {exc}"
    if not ok:
        _LOG.warning(
            "metering DISABLED for %s — stripe_customer_id %r failed validation: %s",
            config.customer_slug,
            cid,
            reason or "invalid",
        )
        return config.model_copy(
            update={
                "metering_disabled": True,
                "metering_disabled_reason": reason or "invalid_stripe_customer_id",
            }
        )
    return config


# TTL for the list_active_configs cache. resolve_customer_for_number scans
# every client config per inbound call; this bounds that to one filesystem
# sweep per window without going stale on an operator who just saved an edit.
_CACHE_TTL_S = 30.0
_cache: dict[str, object] = {"loaded_at": 0.0, "configs": []}


def _receptionist_dir(slug: str) -> Path:
    return storage.root() / "customers" / slug / "receptionist"


def config_path(slug: str) -> Path:
    """Absolute path to one client's receptionist config YAML."""
    return _receptionist_dir(slug) / "config.yaml"


def load_config(slug: str) -> ReceptionistConfig | None:
    """Load + validate one client's receptionist config.

    Returns ``None`` (never raises) when the file is absent, unreadable, not
    a mapping, or fails model validation — the inbound handler treats a
    ``None`` config as "unknown DID" and drops the call rather than crashing.
    """
    path = config_path(slug)
    if not path.exists():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        _LOG.warning("receptionist config unreadable for %s: %s", slug, exc)
        return None
    if not isinstance(raw, dict):
        _LOG.warning("receptionist config for %s is not a mapping", slug)
        return None
    # The slug is the directory name — authoritative. Stamp it so a hand-
    # edited file that omits customer_slug (or typos it) still validates
    # against the right client.
    raw["customer_slug"] = slug
    # The metering gate is loader-derived, never operator-authored — drop any
    # hand-set values so a YAML edit cannot pre-clear the fail-closed flag.
    raw.pop("metering_disabled", None)
    raw.pop("metering_disabled_reason", None)
    try:
        config = ReceptionistConfig.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 — any validation error -> skip
        _LOG.warning("receptionist config for %s failed validation: %s", slug, exc)
        return None
    # FIN-08: validate the operator-supplied stripe_customer_id ONCE here (the
    # result rides with the cached config object) and fail closed on a bad id.
    return _validate_and_gate_metering(config)


def list_active_configs(*, use_cache: bool = True) -> list[ReceptionistConfig]:
    """Every client config with ``status == "active"``. Cached for _CACHE_TTL_S."""
    now = time.monotonic()
    if use_cache and (now - float(_cache["loaded_at"])) < _CACHE_TTL_S:
        return list(_cache["configs"])  # type: ignore[arg-type]

    configs: list[ReceptionistConfig] = []
    customers_root = storage.root() / "customers"
    if customers_root.is_dir():
        for child in sorted(customers_root.iterdir()):
            if not child.is_dir():
                continue
            cfg = load_config(child.name)
            if cfg is not None and cfg.status == "active":
                configs.append(cfg)

    _cache["loaded_at"] = now
    _cache["configs"] = configs
    return list(configs)


def resolve_customer_for_number(e164: str) -> ReceptionistConfig | None:
    """Find the active client whose ``phone_numbers`` contains ``e164``.

    Returns ``None`` when no active client claims the number — a call to an
    unknown DID, which the caller logs and drops (never billed).
    """
    target = (e164 or "").strip()
    if not target:
        return None
    for cfg in list_active_configs():
        if target in cfg.phone_numbers:
            return cfg
    return None


def resolve_customer_for_vapi_number(vapi_phone_number_id: str) -> ReceptionistConfig | None:
    """Find the active client bound to a Vapi phone-number id.

    The inbound webhook reliably carries ``call.phoneNumberId`` (a Vapi UUID);
    the dialed E.164 is not always present. Resolving by the Vapi id is the
    primary path; :func:`resolve_customer_for_number` is the fallback.
    """
    target = (vapi_phone_number_id or "").strip()
    if not target:
        return None
    for cfg in list_active_configs():
        if cfg.vapi_phone_number_id and cfg.vapi_phone_number_id == target:
            return cfg
    return None


def clear_cache() -> None:
    """Drop the active-config cache. For tests + operator config reloads."""
    _cache["loaded_at"] = 0.0
    _cache["configs"] = []
