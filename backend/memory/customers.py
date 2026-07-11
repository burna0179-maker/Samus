"""Customer lifecycle tracking on top of the memory workcell's Neo4j client.

Append-only state model: each Customer node has a current_state pointer; every
transition writes a :StateEvent node linked by :HAS_STATE_EVENT. The latest
event is the current state. Neo4j-down behavior mirrors the graph_client
graceful-degradation pattern — create/advance raise CustomerStoreUnavailableError,
reads return None / [].
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


def _encode_metadata(metadata: dict[str, Any] | None) -> str:
    """Neo4j rejects Map property values; persist metadata as a JSON string."""
    if not metadata:
        return ""
    try:
        return json.dumps(metadata, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return ""


def _decode_metadata(value: Any) -> dict[str, Any]:
    """Inverse of _encode_metadata. Tolerant of legacy / missing values."""
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


from backend.common.graph_client import GraphClient, get_client

_LOG = logging.getLogger("samus.memory.customers")


CustomerState = Literal[
    "prospect",
    "contacted",
    "paid",
    "in_delivery",
    "scope_confirmed",
    "delivered",
    "active_retainer",
    "retainer_cancelled",
    "renewed",
    "churned",
]

VALID_STATES: tuple[str, ...] = (
    "prospect",
    "contacted",
    "paid",
    "in_delivery",
    "scope_confirmed",
    "delivered",
    "active_retainer",
    "retainer_cancelled",
    "renewed",
    "churned",
)

INITIAL_STATE: str = "prospect"


class CustomerStoreUnavailableError(RuntimeError):
    """Raised on a write path when Neo4j is unreachable."""


class Customer(BaseModel):
    id: str
    email: str
    name: str = ""
    company: str = ""
    source: str = "manual"
    created_at: float
    current_state: str = INITIAL_STATE
    current_state_since: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class CustomerStateEvent(BaseModel):
    event_id: str
    customer_id: str
    from_state: str | None
    to_state: str
    date: float
    reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


# --- helpers ----------------------------------------------------------------

_EMAIL_SAFE = re.compile(r"[^a-z0-9]+")


def slugify_email(email: str) -> str:
    """Stable id derived from email, e.g. 'a@b.com' -> 'a_at_b_com'."""
    e = email.strip().lower()
    if "@" in e:
        local, _, domain = e.partition("@")
        slug = f"{local}_at_{domain}"
    else:
        slug = e
    slug = _EMAIL_SAFE.sub("_", slug).strip("_")
    return slug or uuid.uuid4().hex


def _validate_state(state: str) -> None:
    if state not in VALID_STATES:
        raise ValueError(f"invalid customer state: {state}")


# --- store ------------------------------------------------------------------

class CustomerStore:
    """Customer + state-event persistence on Neo4j."""

    def __init__(self, client: GraphClient | None = None) -> None:
        self._client = client or get_client()

    # ---- internals ---------------------------------------------------------

    def _ensure_available(self) -> None:
        if not self._client.available:
            raise CustomerStoreUnavailableError("neo4j unavailable")

    def _safe_run(self, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Run cypher, return rows. None means driver was unavailable (read-side)."""
        if not self._client.available:
            return None
        try:
            return self._client._run(cypher, params)  # noqa: SLF001 — intentional reuse
        except Exception as exc:
            _LOG.warning("customer_store_run_failed", extra={"err": str(exc)})
            return None

    @staticmethod
    def _row_to_customer(props: dict[str, Any]) -> Customer:
        return Customer(
            id=props["id"],
            email=props.get("email", ""),
            name=props.get("name", "") or "",
            company=props.get("company", "") or "",
            source=props.get("source", "manual") or "manual",
            created_at=float(props.get("created_at", 0.0)),
            current_state=props.get("current_state", INITIAL_STATE),
            current_state_since=float(props.get("current_state_since", 0.0)),
            metadata=_decode_metadata(props.get("metadata")),
        )

    @staticmethod
    def _row_to_event(props: dict[str, Any]) -> CustomerStateEvent:
        return CustomerStateEvent(
            event_id=props["event_id"],
            customer_id=props["customer_id"],
            from_state=props.get("from_state"),
            to_state=props["to_state"],
            date=float(props.get("date", 0.0)),
            reason=props.get("reason", "") or "",
            metadata=_decode_metadata(props.get("metadata")),
        )

    # ---- create / advance --------------------------------------------------

    def create_customer(
        self,
        email: str,
        name: str = "",
        company: str = "",
        source: str = "manual",
        metadata: dict[str, Any] | None = None,
    ) -> Customer:
        if not email or "@" not in email:
            raise ValueError("email required (must contain '@')")
        self._ensure_available()

        existing = self.get_by_email(email)
        if existing is not None:
            return existing

        now = time.time()
        cust = Customer(
            id=slugify_email(email),
            email=email.strip().lower(),
            name=name or "",
            company=company or "",
            source=source or "manual",
            created_at=now,
            current_state=INITIAL_STATE,
            current_state_since=now,
            metadata=dict(metadata or {}),
        )

        cypher = (
            "MERGE (c:Customer {id: $id}) "
            "ON CREATE SET c.email = $email, c.name = $name, c.company = $company, "
            "c.source = $source, c.created_at = $created_at, "
            "c.current_state = $current_state, c.current_state_since = $current_state_since, "
            "c.metadata = $metadata "
            "RETURN c"
        )
        params = {
            "id": cust.id,
            "email": cust.email,
            "name": cust.name,
            "company": cust.company,
            "source": cust.source,
            "created_at": cust.created_at,
            "current_state": cust.current_state,
            "current_state_since": cust.current_state_since,
            "metadata": _encode_metadata(cust.metadata),  # JSON string, not Map
        }
        try:
            self._client._run(cypher, params)  # noqa: SLF001
        except Exception as exc:
            raise CustomerStoreUnavailableError(str(exc)) from exc

        # Initial state event so history is complete even before any advance.
        self._write_event(
            customer_id=cust.id,
            from_state=None,
            to_state=INITIAL_STATE,
            date=now,
            reason="created",
            metadata={},
        )
        return cust

    def advance_state(
        self,
        customer_id: str,
        to_state: str,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> CustomerStateEvent:
        _validate_state(to_state)
        self._ensure_available()

        cust = self.get_customer(customer_id)
        if cust is None:
            raise ValueError(f"customer not found: {customer_id}")

        now = time.time()
        event = self._write_event(
            customer_id=customer_id,
            from_state=cust.current_state,
            to_state=to_state,
            date=now,
            reason=reason or "",
            metadata=dict(metadata or {}),
        )

        update_cypher = (
            "MATCH (c:Customer {id: $id}) "
            "SET c.current_state = $to_state, c.current_state_since = $date "
            "RETURN c"
        )
        try:
            self._client._run(  # noqa: SLF001
                update_cypher,
                {"id": customer_id, "to_state": to_state, "date": now},
            )
        except Exception as exc:
            raise CustomerStoreUnavailableError(str(exc)) from exc
        return event

    def _write_event(
        self,
        *,
        customer_id: str,
        from_state: str | None,
        to_state: str,
        date: float,
        reason: str,
        metadata: dict[str, Any],
    ) -> CustomerStateEvent:
        event = CustomerStateEvent(
            event_id=uuid.uuid4().hex,
            customer_id=customer_id,
            from_state=from_state,
            to_state=to_state,
            date=date,
            reason=reason,
            metadata=dict(metadata),
        )
        cypher = (
            "MATCH (c:Customer {id: $customer_id}) "
            "CREATE (e:StateEvent {event_id: $event_id, customer_id: $customer_id, "
            "from_state: $from_state, to_state: $to_state, date: $date, "
            "reason: $reason, metadata: $metadata}) "
            "MERGE (c)-[:HAS_STATE_EVENT]->(e) "
            "RETURN e"
        )
        params = {
            "event_id": event.event_id,
            "customer_id": event.customer_id,
            "from_state": event.from_state,
            "to_state": event.to_state,
            "date": event.date,
            "reason": event.reason,
            "metadata": _encode_metadata(event.metadata),  # JSON string, not Map
        }
        try:
            self._client._run(cypher, params)  # noqa: SLF001
        except Exception as exc:
            raise CustomerStoreUnavailableError(str(exc)) from exc
        return event

    # ---- reads -------------------------------------------------------------

    def get_customer(self, customer_id: str) -> Customer | None:
        rows = self._safe_run(
            "MATCH (c:Customer {id: $id}) RETURN c LIMIT 1",
            {"id": customer_id},
        )
        if not rows:
            return None
        node = rows[0].get("c") or {}
        if not node:
            return None
        return self._row_to_customer(dict(node))

    def get_by_email(self, email: str) -> Customer | None:
        norm = email.strip().lower()
        rows = self._safe_run(
            "MATCH (c:Customer {email: $email}) RETURN c LIMIT 1",
            {"email": norm},
        )
        if not rows:
            return None
        node = rows[0].get("c") or {}
        if not node:
            return None
        return self._row_to_customer(dict(node))

    def list_customers(
        self,
        state: str | None = None,
        limit: int = 100,
    ) -> list[Customer]:
        if state is not None:
            _validate_state(state)
        limit = max(1, min(int(limit or 100), 1000))
        if state is None:
            rows = self._safe_run(
                "MATCH (c:Customer) RETURN c ORDER BY c.created_at DESC LIMIT $limit",
                {"limit": limit},
            )
        else:
            rows = self._safe_run(
                "MATCH (c:Customer {current_state: $state}) "
                "RETURN c ORDER BY c.created_at DESC LIMIT $limit",
                {"state": state, "limit": limit},
            )
        if not rows:
            return []
        out: list[Customer] = []
        for row in rows:
            node = row.get("c") or {}
            if node:
                out.append(self._row_to_customer(dict(node)))
        return out

    def state_history(self, customer_id: str) -> list[CustomerStateEvent]:
        rows = self._safe_run(
            "MATCH (c:Customer {id: $id})-[:HAS_STATE_EVENT]->(e:StateEvent) "
            "RETURN e ORDER BY e.date ASC",
            {"id": customer_id},
        )
        if not rows:
            return []
        out: list[CustomerStateEvent] = []
        for row in rows:
            node = row.get("e") or {}
            if node:
                out.append(self._row_to_event(dict(node)))
        return out
