"""Thin httpx wrapper around the Stripe REST API.

Read endpoints: ``/v1/balance``, ``/v1/charges``, ``/v1/payouts``,
``/v1/subscriptions``, ``/v1/customers``, ``/v1/payment_links``. Write
endpoints: ``/v1/coupons``, ``/v1/promotion_codes`` (upsell credits) and
``/v1/billing/meter_events`` (AI Digital Receptionist metered usage). The
write endpoints need a full secret key (``sk_...``); the read endpoints also
work with a restricted-read key.

Why no ``stripe`` SDK: the SDK adds ~30MB to the container and pulls in
threading + retry machinery we already handle at the workcell layer. The
REST surface for this workcell is tiny — keeping it httpx-only keeps the
finance image lean and stays consistent with seo/content + memory/llm.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .models import (
    StripeBalance,
    StripeCharge,
    StripeCustomer,
    StripePaymentLink,
    StripePayout,
    StripeSubscription,
)


_LOG = logging.getLogger("samus.finance.stripe_client")

_BASE_URL = "https://api.stripe.com/v1"
_HTTP_TIMEOUT = 15.0


class StripeError(Exception):
    """Raised when Stripe returns a non-2xx response or the network fails."""


class StripeClient:
    """Minimal read-only Stripe client. One instance per service call.

    The api_key is taken at construction time (not from env) so tests can
    inject a fake without touching get_settings().
    """

    def __init__(self, api_key: str, *, base_url: str = _BASE_URL,
                 timeout: float = _HTTP_TIMEOUT) -> None:
        if not api_key:
            raise ValueError("StripeClient requires a non-empty api_key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    # --- low-level ---------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET ``path`` (must start with /). Raise StripeError on any failure."""
        if not path.startswith("/"):
            raise ValueError(f"path must start with /, got {path!r}")
        url = f"{self._base_url}{path}"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.get(url, headers=headers, params=params or {})
        except httpx.HTTPError as exc:
            raise StripeError(f"stripe_transport_error: {exc}") from exc

        if response.status_code >= 400:
            # Stripe error bodies are { error: { type, code, message, ... } }.
            try:
                err = response.json().get("error", {})
            except ValueError:
                err = {}
            message = err.get("message") or response.text[:200]
            raise StripeError(
                f"stripe_http_{response.status_code}: {message}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise StripeError(f"stripe_invalid_json: {exc}") from exc

    def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        """POST form-encoded ``data`` to ``path`` (must start with /).

        Stripe expects ``application/x-www-form-urlencoded`` for all writes,
        with nested objects flattened as ``parent[child]=value`` keys. httpx's
        ``data=`` param does the encoding; nested dicts must be flattened by
        the caller before passing (see ``create_coupon`` for the pattern).
        """
        if not path.startswith("/"):
            raise ValueError(f"path must start with /, got {path!r}")
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(url, headers=headers, data=data)
        except httpx.HTTPError as exc:
            raise StripeError(f"stripe_transport_error: {exc}") from exc

        if response.status_code >= 400:
            try:
                err = response.json().get("error", {})
            except ValueError:
                err = {}
            message = err.get("message") or response.text[:200]
            raise StripeError(
                f"stripe_http_{response.status_code}: {message}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise StripeError(f"stripe_invalid_json: {exc}") from exc

    # --- coupons + promotion codes (upsell credit application) ------------

    def create_coupon(
        self,
        *,
        amount_off_cents: int,
        currency: str = "usd",
        duration: str = "once",
        max_redemptions: int = 1,
        name: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST /v1/coupons -> raw Stripe coupon dict (id, amount_off, ...).

        ``duration="once"`` is the right default for upsell credits — the
        discount applies to a single invoice (the first one for a recurring
        product, the only one for a one-time product). The discount caps at
        invoice amount, so a credit larger than the product price simply
        zeros out that invoice (excess is forfeit unless duration is
        switched to ``repeating`` with ``duration_in_months``).
        """
        if amount_off_cents <= 0:
            raise ValueError(f"amount_off_cents must be positive, got {amount_off_cents}")
        if duration not in ("once", "repeating", "forever"):
            raise ValueError(f"duration must be once|repeating|forever, got {duration!r}")
        data: dict[str, Any] = {
            "amount_off": str(amount_off_cents),
            "currency": currency,
            "duration": duration,
            "max_redemptions": str(max_redemptions),
        }
        if name:
            data["name"] = name
        for key, value in (metadata or {}).items():
            data[f"metadata[{key}]"] = value
        return self._post("/coupons", data)

    def create_promotion_code(
        self,
        *,
        coupon_id: str,
        code: str,
        max_redemptions: int = 1,
        customer: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST /v1/promotion_codes -> raw Stripe promotion_code dict.

        The ``code`` is the human-readable string the customer sees
        (e.g. ``AUDIT-CREDIT-7K2QXM``). Stripe enforces uppercase + uniqueness
        per account; the caller is responsible for generating something
        collision-resistant.

        ``customer`` (optional) locks the code to one Stripe customer id; if
        the upsell row pre-dates the customer record being created in Stripe
        (typical for SEO audit → implementation), leave it unset and rely on
        ``max_redemptions=1`` to prevent reuse.
        """
        if not coupon_id:
            raise ValueError("coupon_id is required")
        if not code:
            raise ValueError("code is required")
        data: dict[str, Any] = {
            "coupon": coupon_id,
            "code": code,
            "max_redemptions": str(max_redemptions),
        }
        if customer:
            data["customer"] = customer
        for key, value in (metadata or {}).items():
            data[f"metadata[{key}]"] = value
        return self._post("/promotion_codes", data)

    # --- metered usage (AI Digital Receptionist) --------------------------

    def create_meter_event(
        self,
        *,
        event_name: str,
        stripe_customer_id: str,
        value: int | float,
        identifier: str | None = None,
        timestamp: int | None = None,
    ) -> dict[str, Any]:
        """POST /v1/billing/meter_events -> raw Stripe meter-event dict.

        Reports ``value`` usage units against the Meter named ``event_name``
        for ``stripe_customer_id``. Stripe aggregates these onto the metered
        subscription item's invoice at period close — no further call needed.

        ``identifier`` (recommended: the Vapi call id) makes the event
        idempotent: Stripe deduplicates events that share an identifier, so a
        webhook redelivery cannot double-bill the same call.
        """
        if not event_name:
            raise ValueError("create_meter_event requires a non-empty event_name")
        if not stripe_customer_id:
            raise ValueError(
                "create_meter_event requires a non-empty stripe_customer_id",
            )
        if value is None or float(value) <= 0:
            raise ValueError(
                f"create_meter_event value must be positive, got {value!r}",
            )
        data: dict[str, Any] = {
            "event_name": event_name,
            "payload[stripe_customer_id]": stripe_customer_id,
            "payload[value]": str(value),
        }
        if identifier:
            data["identifier"] = identifier
        if timestamp is not None:
            data["timestamp"] = str(int(timestamp))
        return self._post("/billing/meter_events", data)

    # --- high-level (typed) ------------------------------------------------

    def fetch_balance(self) -> StripeBalance:
        """GET /v1/balance -> typed StripeBalance."""
        return StripeBalance.model_validate(self._get("/balance"))

    def fetch_charges(self, limit: int = 10, *,
                      customer: str | None = None) -> list[StripeCharge]:
        """GET /v1/charges?limit=N&customer=X -> typed StripeCharge list.

        ``customer`` (optional) scopes the query to one customer id; without
        it Stripe returns the account-wide recent-charges feed (existing
        snapshot behaviour).
        """
        params: dict[str, Any] = {"limit": max(1, min(100, int(limit)))}
        if customer:
            params["customer"] = customer
        data = self._get("/charges", params)
        rows = data.get("data", []) or []
        out: list[StripeCharge] = []
        for row in rows:
            try:
                out.append(StripeCharge.model_validate(row))
            except Exception as exc:  # noqa: BLE001 — log + skip malformed row
                _LOG.warning("skipping malformed charge row: %s", exc)
        return out

    def fetch_payouts(self, limit: int = 10) -> list[StripePayout]:
        """GET /v1/payouts?limit=N -> list of typed StripePayout."""
        data = self._get("/payouts", {"limit": max(1, min(100, int(limit)))})
        rows = data.get("data", []) or []
        out: list[StripePayout] = []
        for row in rows:
            try:
                out.append(StripePayout.model_validate(row))
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("skipping malformed payout row: %s", exc)
        return out

    def fetch_payment_links(self, *, active: bool = True,
                            limit: int = 100) -> list[StripePaymentLink]:
        """GET /v1/payment_links?active=X -> list of typed StripePaymentLink.

        The ``active`` query filter is sticky — Stripe defaults to returning
        all links including archived. Filter at the API level so callers don't
        get a polluted result set.
        """
        data = self._get("/payment_links", {
            "active": "true" if active else "false",
            "limit": max(1, min(100, int(limit))),
        })
        rows = data.get("data", []) or []
        out: list[StripePaymentLink] = []
        for row in rows:
            try:
                out.append(StripePaymentLink.model_validate_envelope(row))
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("skipping malformed payment_link row: %s", exc)
        return out

    def fetch_subscriptions(self, *, status: str = "active",
                            limit: int = 100,
                            customer: str | None = None) -> list[StripeSubscription]:
        """GET /v1/subscriptions?status=X with items.price expanded.

        Stripe requires ``expand[]=data.items.data.price`` to inline the full
        price object; without it ``item.price`` is just the price id and MRR
        is uncomputable from the response.

        ``customer`` (optional) scopes the query to one customer id.
        """
        params: dict[str, Any] = {
            "status": status,
            "limit": max(1, min(100, int(limit))),
            "expand[]": "data.items.data.price",
        }
        if customer:
            params["customer"] = customer
        data = self._get("/subscriptions", params)
        rows = data.get("data", []) or []
        out: list[StripeSubscription] = []
        for row in rows:
            try:
                out.append(StripeSubscription.model_validate_envelope(row))
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("skipping malformed subscription row: %s", exc)
        return out

    # --- per-customer reads (billing-summary surface) ---------------------

    def retrieve_customer(self, customer_id: str) -> StripeCustomer | None:
        """GET /v1/customers/{id} -> typed StripeCustomer, or None on 404.

        Used by the existence check the voice workcell calls before metering a
        receptionist's ``stripe_customer_id`` (FIN-08). Semantics are
        fail-closed for the caller's benefit:

        * a live customer with this id -> the typed :class:`StripeCustomer`
        * a 404 (no such customer, or a deleted/malformed id Stripe rejects as
          a missing resource) -> ``None`` (a definitive "does not exist")
        * any other Stripe failure (auth, rate limit, transport, 5xx) ->
          re-raised as :class:`StripeError` so the route maps it to
          "not confirmed" rather than silently treating it as existing.

        Never returns a customer object on error — the only non-None return is
        a 2xx body Stripe affirmatively returned for this id.
        """
        cid = (customer_id or "").strip()
        if not cid:
            # An empty/blank id cannot identify a live customer; treat as a
            # definitive "does not exist" without a network round-trip.
            return None
        try:
            raw = self._get(f"/customers/{cid}")
        except StripeError as exc:
            # A 404 is a definitive "no such customer" — distinguish it from a
            # genuine failure (auth/rate-limit/transport) which must propagate
            # so the caller fails closed to "not confirmed".
            if "stripe_http_404" in str(exc):
                return None
            raise
        # Stripe returns deleted customers as {id, deleted: true, ...}; a
        # deleted record is not a live customer.
        if raw.get("deleted") is True:
            return None
        return StripeCustomer.model_validate(raw)

    def fetch_customers_by_email(self, email: str, *,
                                 limit: int = 10) -> list[StripeCustomer]:
        """GET /v1/customers?email=X — Stripe's exact-match email search.

        Returns zero, one, or multiple customer rows: Stripe permits a single
        email to map to several Customer objects (legacy duplicates,
        test+live mixups, etc.) so callers must consider the *set* and
        pick by recency/livemode.
        """
        if not email or not email.strip():
            return []
        data = self._get("/customers", {
            "email": email.strip(),
            "limit": max(1, min(100, int(limit))),
        })
        rows = data.get("data", []) or []
        out: list[StripeCustomer] = []
        for row in rows:
            try:
                out.append(StripeCustomer.model_validate(row))
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("skipping malformed customer row: %s", exc)
        return out


# ---------------------------------------------------------------------------
# Pure-function helpers
# ---------------------------------------------------------------------------

def monthly_recurring_revenue_usd(subscriptions: list[StripeSubscription]) -> float:
    """Sum per-item amount normalized to monthly. USD-only.

    Interval factors mirror Stripe billing math:
        day   -> x30
        week  -> x4.33
        month -> x1
        year  -> /12
    Returns dollars (not cents).
    """
    factors: dict[str, float] = {
        "day": 30.0, "week": 4.33, "month": 1.0, "year": 1.0 / 12.0,
    }
    cents = 0.0
    for sub in subscriptions:
        for item in sub.items:
            price = item.price
            if price.currency.lower() != "usd":
                continue
            interval = (price.recurring.interval or "month").lower()
            count = max(1, int(price.recurring.interval_count or 1))
            factor = factors.get(interval, 1.0) / count
            cents += price.unit_amount * item.quantity * factor
    return round(cents / 100.0, 2)
