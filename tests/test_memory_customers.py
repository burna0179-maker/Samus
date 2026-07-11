"""Customer lifecycle tests — uses a stub GraphClient, no real Neo4j."""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest


# --- fake graph client ------------------------------------------------------


class _FakeGraphClient:
    """Drop-in stand-in for GraphClient that records cypher + emulates a tiny graph."""

    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # Tiny in-memory graph: customers keyed by id, events keyed by event_id.
        self.customers: dict[str, dict[str, Any]] = {}
        self.events: dict[str, dict[str, Any]] = {}
        # customer_id -> [event_id, ...] (insertion order)
        self.customer_events: dict[str, list[str]] = {}

    # The CustomerStore calls client._run(cypher, params). We dispatch by
    # matching loose substrings of the cypher.
    def _run(self, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((cypher, dict(params)))
        if "MERGE (c:Customer {id: $id})" in cypher and "ON CREATE SET" in cypher:
            cid = params["id"]
            if cid not in self.customers:
                self.customers[cid] = {
                    "id": cid,
                    "email": params["email"],
                    "name": params["name"],
                    "company": params["company"],
                    "source": params["source"],
                    "created_at": params["created_at"],
                    "current_state": params["current_state"],
                    "current_state_since": params["current_state_since"],
                    "metadata": dict(params["metadata"]),
                }
            return [{"c": dict(self.customers[cid])}]
        if "CREATE (e:StateEvent" in cypher:
            eid = params["event_id"]
            event = {
                "event_id": eid,
                "customer_id": params["customer_id"],
                "from_state": params["from_state"],
                "to_state": params["to_state"],
                "date": params["date"],
                "reason": params["reason"],
                "metadata": dict(params["metadata"]),
            }
            self.events[eid] = event
            self.customer_events.setdefault(params["customer_id"], []).append(eid)
            return [{"e": dict(event)}]
        if "SET c.current_state = $to_state" in cypher:
            cid = params["id"]
            if cid in self.customers:
                self.customers[cid]["current_state"] = params["to_state"]
                self.customers[cid]["current_state_since"] = params["date"]
                return [{"c": dict(self.customers[cid])}]
            return []
        if "MATCH (c:Customer {id: $id}) RETURN c" in cypher:
            cust = self.customers.get(params["id"])
            return [{"c": dict(cust)}] if cust else []
        if "MATCH (c:Customer {email: $email}) RETURN c" in cypher:
            for cust in self.customers.values():
                if cust["email"] == params["email"]:
                    return [{"c": dict(cust)}]
            return []
        if (
            "MATCH (c:Customer)" in cypher
            and "RETURN c" in cypher
            and "current_state" not in cypher
        ):
            ordered = sorted(self.customers.values(), key=lambda c: -c["created_at"])
            return [{"c": dict(c)} for c in ordered[: params.get("limit", 100)]]
        if "MATCH (c:Customer {current_state: $state})" in cypher:
            wanted = [c for c in self.customers.values() if c["current_state"] == params["state"]]
            wanted.sort(key=lambda c: -c["created_at"])
            return [{"c": dict(c)} for c in wanted[: params.get("limit", 100)]]
        if "HAS_STATE_EVENT" in cypher and "RETURN e" in cypher:
            ids = self.customer_events.get(params["id"], [])
            evs = [self.events[eid] for eid in ids]
            evs.sort(key=lambda e: e["date"])
            return [{"e": dict(e)} for e in evs]
        return []


class _UnavailableClient:
    available = False

    def _run(self, cypher: str, params: dict[str, Any]):  # pragma: no cover
        raise AssertionError("should not be invoked when available is False")


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def fake_client():
    return _FakeGraphClient(available=True)


@pytest.fixture
def store(fake_client):
    from backend.memory.customers import CustomerStore

    return CustomerStore(client=fake_client)


@pytest.fixture
def unavailable_store():
    from backend.memory.customers import CustomerStore

    return CustomerStore(client=_UnavailableClient())


# --- unit tests on CustomerStore -------------------------------------------


def test_create_customer_returns_typed_customer(store):
    cust = store.create_customer(email="alex@hustleforge.tech", name="Alex", company="HF")
    assert cust.email == "alex@hustleforge.tech"
    assert cust.name == "Alex"
    assert cust.company == "HF"
    assert cust.current_state == "prospect"
    assert cust.created_at > 0
    assert cust.current_state_since == cust.created_at
    assert cust.id  # non-empty
    assert "@" not in cust.id  # email is slugified


def test_create_customer_idempotent_by_email(store):
    a = store.create_customer(email="repeat@example.com", name="First")
    b = store.create_customer(email="repeat@example.com", name="Second")
    assert a.id == b.id
    # Same record returned, not a new one.
    assert b.name == a.name  # idempotent: first creation wins


def test_advance_state_appends_event_and_updates_current(store):
    cust = store.create_customer(email="ad@example.com")
    e1 = store.advance_state(cust.id, "contacted", reason="returned voicemail")
    e2 = store.advance_state(cust.id, "paid", reason="stripe link clicked")

    assert e1.from_state == "prospect"
    assert e1.to_state == "contacted"
    assert e2.from_state == "contacted"
    assert e2.to_state == "paid"

    fetched = store.get_customer(cust.id)
    assert fetched is not None
    assert fetched.current_state == "paid"
    assert fetched.current_state_since >= cust.created_at

    history = store.state_history(cust.id)
    # Initial 'created' event + 2 advances.
    assert [h.to_state for h in history] == ["prospect", "contacted", "paid"]


def test_advance_state_rejects_unknown_state(store):
    cust = store.create_customer(email="bad@example.com")
    with pytest.raises(ValueError, match="invalid customer state"):
        store.advance_state(cust.id, "bogus_state")


def test_advance_state_unknown_customer_raises(store):
    with pytest.raises(ValueError, match="not found"):
        store.advance_state("no_such_id", "paid")


def test_get_by_email_returns_customer(store):
    cust = store.create_customer(email="round@example.com", name="Round Trip")
    fetched = store.get_by_email("round@example.com")
    assert fetched is not None
    assert fetched.id == cust.id
    assert fetched.name == "Round Trip"


def test_get_by_email_case_insensitive(store):
    store.create_customer(email="MiXeD@Example.com")
    fetched = store.get_by_email("mixed@example.com")
    assert fetched is not None


def test_list_customers_filters_by_state(store):
    c1 = store.create_customer(email="p1@example.com")
    c2 = store.create_customer(email="p2@example.com")
    c3 = store.create_customer(email="p3@example.com")
    store.advance_state(c2.id, "paid")
    store.advance_state(c3.id, "paid")

    paid = store.list_customers(state="paid")
    paid_ids = {c.id for c in paid}
    assert c2.id in paid_ids and c3.id in paid_ids
    assert c1.id not in paid_ids

    all_three = store.list_customers()
    assert len(all_three) == 3


def test_state_history_chronological(store):
    cust = store.create_customer(email="hist@example.com")
    store.advance_state(cust.id, "contacted")
    # Tiny sleep so the next event has a strictly greater timestamp.
    time.sleep(0.001)
    store.advance_state(cust.id, "paid")
    time.sleep(0.001)
    store.advance_state(cust.id, "in_delivery")

    history = store.state_history(cust.id)
    assert [h.to_state for h in history] == ["prospect", "contacted", "paid", "in_delivery"]
    dates = [h.date for h in history]
    assert dates == sorted(dates)


def test_neo4j_unavailable_returns_none(unavailable_store):
    from backend.memory.customers import CustomerStoreUnavailableError

    # Reads degrade to None / [] gracefully.
    assert unavailable_store.get_customer("anything") is None
    assert unavailable_store.get_by_email("x@y.com") is None
    assert unavailable_store.list_customers() == []
    assert unavailable_store.state_history("anything") == []

    # Writes raise a clean typed error.
    with pytest.raises(CustomerStoreUnavailableError):
        unavailable_store.create_customer(email="x@y.com")


# --- app-level round-trip --------------------------------------------------


def _wire_app(monkeypatch, fake) -> Any:
    import backend.memory.app as app_mod
    from backend.memory.customers import CustomerStore

    monkeypatch.setattr(app_mod, "_resolve_customer_store", lambda: CustomerStore(client=fake))
    from fastapi.testclient import TestClient

    return TestClient(app_mod.app)


def test_app_endpoints_round_trip(monkeypatch):
    fake = _FakeGraphClient(available=True)
    client = _wire_app(monkeypatch, fake)

    # Create
    r = client.post(
        "/customers",
        json={"email": "rt@example.com", "name": "Round", "source": "manual"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    cust_id = body["customer"]["id"]
    assert body["customer"]["current_state"] == "prospect"

    # Advance
    r = client.post(
        f"/customers/{cust_id}/advance",
        json={"to_state": "paid", "reason": "stripe"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["event"]["to_state"] == "paid"

    # Get one
    r = client.get(f"/customers/{cust_id}")
    assert r.status_code == 200
    assert r.json()["customer"]["current_state"] == "paid"

    # List with filter
    r = client.get("/customers", params={"state": "paid"})
    assert r.status_code == 200
    assert r.json()["count"] == 1

    # History
    r = client.get(f"/customers/{cust_id}/history")
    assert r.status_code == 200
    states = [e["to_state"] for e in r.json()["events"]]
    assert states == ["prospect", "paid"]


def test_app_get_returns_404_when_missing(monkeypatch):
    fake = _FakeGraphClient(available=True)
    client = _wire_app(monkeypatch, fake)
    r = client.get("/customers/no_such")
    assert r.status_code == 404


def test_app_unavailable_returns_status_unavailable(monkeypatch):
    fake = _UnavailableClient()
    client = _wire_app(monkeypatch, fake)
    r = client.post("/customers", json={"email": "down@example.com"})
    assert r.status_code == 200
    assert r.json() == {"status": "unavailable"}


def test_app_work_envelope_dispatch(monkeypatch):
    fake = _FakeGraphClient(available=True)
    client = _wire_app(monkeypatch, fake)

    # create via /work
    r = client.post(
        "/work",
        json={
            "task_id": uuid.uuid4().hex,
            "payload": {"sub_action": "create", "email": "work@example.com"},
            "metadata": {"action": "customers"},
        },
    )
    assert r.status_code == 200, r.text
    cust_id = r.json()["customer"]["id"]

    # advance via /work
    r = client.post(
        "/work",
        json={
            "task_id": "t",
            "payload": {"sub_action": "advance", "customer_id": cust_id, "to_state": "paid"},
            "metadata": {"action": "customers"},
        },
    )
    assert r.status_code == 200
    assert r.json()["event"]["to_state"] == "paid"

    # list via /work
    r = client.post(
        "/work",
        json={
            "task_id": "t",
            "payload": {"sub_action": "list", "state": "paid"},
            "metadata": {"action": "customers"},
        },
    )
    assert r.status_code == 200
    assert r.json()["count"] == 1


def test_app_capability_registered():
    from backend.common.capabilities import SERVICE_CAPABILITIES

    assert "customers" in SERVICE_CAPABILITIES["memory"]
