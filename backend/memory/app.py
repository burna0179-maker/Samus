"""HTTP surface for the memory workcell (doc §8)."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from backend.common.app_factory import create_base_app
from backend.common.capabilities import check_capability
from backend.common.graph_client import GraphClient, get_client

from .customers import CustomerStore, CustomerStoreUnavailableError
from .knowledge_ingest import (
    IngestRequest,
    KnowledgeIngestPod,
    TrustLevel,
    _jail_source_path,
)
from .models import KnowledgeIngestPayload
from .store import GLOBAL_MEMORY_STORE

_LOG = logging.getLogger("samus.memory.app")

app = create_base_app(service_name="memory")

# --- knowledge ingest pod (default Neo4j wiring) -------------------------

_INGEST_POD = KnowledgeIngestPod()
_ingest_router = _INGEST_POD.get_router()
if _ingest_router is not None:
    app.include_router(_ingest_router)


# --- request/response shapes ---------------------------------------------

class _WriteBody(BaseModel):
    namespace: str = Field(min_length=1)
    key: str = Field(min_length=1)
    value: Any
    ttl_seconds: int | None = Field(default=None, ge=1)


class _ReadBody(BaseModel):
    namespace: str = Field(min_length=1)
    key: str = Field(min_length=1)


class _QueryBody(BaseModel):
    namespace: str = Field(min_length=1)
    prefix: str = ""
    limit: int = Field(default=50, ge=1, le=500)
    cursor: str | None = None


class _DeleteBody(BaseModel):
    namespace: str = Field(min_length=1)
    key: str = Field(min_length=1)


class _GraphInitBody(BaseModel):
    """No fields — POST body kept for symmetry with the other graph endpoints."""

    pass


class _GraphNodeBody(BaseModel):
    label: str = Field(min_length=1)
    properties: dict[str, Any] = Field(default_factory=dict)


class _GraphRelationshipBody(BaseModel):
    source_label: str = Field(min_length=1)
    source_key: Any
    rel_type: str = Field(min_length=1)
    target_label: str = Field(min_length=1)
    target_key: Any


class _GraphQueryBody(BaseModel):
    name: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class _GraphPromoteBody(BaseModel):
    label: str = Field(min_length=1)
    key_value: Any
    key_property: str | None = None


class _CustomerCreateBody(BaseModel):
    email: str = Field(min_length=1)
    name: str = ""
    company: str = ""
    source: str = "manual"
    metadata: dict[str, Any] = Field(default_factory=dict)


class _CustomerAdvanceBody(BaseModel):
    to_state: str = Field(min_length=1)
    reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class _WorkBody(BaseModel):
    task_id: str = Field(default="local", min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


# --- k/v endpoints --------------------------------------------------------

@app.post("/write")
async def write_endpoint(body: _WriteBody) -> dict[str, Any]:
    check_capability("memory", "write")
    GLOBAL_MEMORY_STORE.write(body.namespace, body.key, body.value, body.ttl_seconds)
    return {"status": "ok", "namespace": body.namespace, "key": body.key}


@app.post("/read")
async def read_endpoint(body: _ReadBody) -> dict[str, Any]:
    check_capability("memory", "read")
    value, found = GLOBAL_MEMORY_STORE.read(body.namespace, body.key)
    return {"value": value, "found": found}


@app.post("/query")
async def query_endpoint(body: _QueryBody) -> dict[str, Any]:
    check_capability("memory", "query")
    items, next_cursor = GLOBAL_MEMORY_STORE.query(
        body.namespace,
        body.prefix,
        limit=body.limit,
        cursor=body.cursor,
    )
    return {"items": items, "next_cursor": next_cursor}


@app.post("/delete")
async def delete_endpoint(body: _DeleteBody) -> dict[str, Any]:
    check_capability("memory", "delete")
    deleted = GLOBAL_MEMORY_STORE.delete(body.namespace, body.key)
    return {"deleted": deleted}


@app.get("/stats/{namespace}")
async def stats_endpoint(namespace: str) -> dict[str, Any]:
    check_capability("memory", "stats")
    if not namespace:
        raise HTTPException(status_code=422, detail="namespace required")
    return GLOBAL_MEMORY_STORE.stats(namespace)


# --- graph endpoints (Neo4j) --------------------------------------------

def _resolve_client() -> GraphClient:
    """Wrapped for monkeypatching in tests."""
    return get_client()


@app.post("/graph/init")
async def graph_init(body: _GraphInitBody | None = None) -> dict[str, Any]:
    check_capability("memory", "graph")
    client = _resolve_client()
    if not client.available:
        return {"status": "unavailable"}
    client.init_schema()
    return {"status": "ok"}


@app.post("/graph/write/node")
async def graph_write_node(body: _GraphNodeBody) -> dict[str, Any]:
    check_capability("memory", "graph")
    client = _resolve_client()
    try:
        # Validate even when the driver is offline so callers get a 422 on
        # bad shape regardless of whether Neo4j is reachable.
        from backend.common import graph_schema
        graph_schema.validate_node(body.label, body.properties)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not client.available:
        return {"status": "unavailable"}
    try:
        client.write_node(body.label, body.properties)
    except ValueError as exc:
        _LOG.warning("graph_write_node failed: %s", exc)
        raise HTTPException(status_code=422, detail="graph_write_error") from exc
    return {"status": "ok"}


@app.post("/graph/write/relationship")
async def graph_write_relationship(body: _GraphRelationshipBody) -> dict[str, Any]:
    check_capability("memory", "graph")
    client = _resolve_client()
    try:
        from backend.common import graph_schema
        graph_schema.validate_relationship(body.source_label, body.rel_type, body.target_label)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not client.available:
        return {"status": "unavailable"}
    try:
        client.write_relationship(
            body.source_label,
            body.source_key,
            body.rel_type,
            body.target_label,
            body.target_key,
        )
    except ValueError as exc:
        _LOG.warning("graph_write_relationship failed: %s", exc)
        raise HTTPException(status_code=422, detail="graph_write_error") from exc
    return {"status": "ok"}


@app.post("/graph/promote")
async def graph_promote(body: _GraphPromoteBody) -> dict[str, Any]:
    """Promote a knowledge-graph node to the ``hivemind`` tier (W-4).

    Flips the node's ``tier`` property to ``hivemind`` — the set the §7
    experience feed exports. ``key_property`` defaults to the label's schema
    primary key. Returns ``promoted=false`` when no node matched.
    """
    check_capability("memory", "graph")
    client = _resolve_client()
    try:
        promoted = client.promote_node(
            body.label, body.key_value, key_property=body.key_property,
        )
    except ValueError as exc:
        _LOG.warning("graph_promote failed: %s", exc)
        raise HTTPException(status_code=422, detail="graph_promote_error") from exc
    if not client.available:
        return {"status": "unavailable"}
    return {"status": "ok", "promoted": promoted}


# --- customer-lifecycle endpoints ---------------------------------------

def _resolve_customer_store() -> CustomerStore:
    """Wrapped for monkeypatching in tests."""
    return CustomerStore(client=_resolve_client())


@app.post("/customers")
async def customers_create(body: _CustomerCreateBody) -> dict[str, Any]:
    check_capability("memory", "customers")
    store = _resolve_customer_store()
    try:
        cust = store.create_customer(
            email=body.email,
            name=body.name,
            company=body.company,
            source=body.source,
            metadata=body.metadata,
        )
    except CustomerStoreUnavailableError:
        return {"status": "unavailable"}
    except ValueError as exc:
        _LOG.warning("customers_create failed: %s", exc)
        raise HTTPException(status_code=422, detail="customer_validation_error") from exc
    return {"status": "ok", "customer": cust.model_dump()}


@app.post("/customers/{customer_id}/advance")
async def customers_advance(customer_id: str, body: _CustomerAdvanceBody) -> dict[str, Any]:
    check_capability("memory", "customers")
    store = _resolve_customer_store()
    try:
        event = store.advance_state(
            customer_id=customer_id,
            to_state=body.to_state,
            reason=body.reason,
            metadata=body.metadata,
        )
    except CustomerStoreUnavailableError:
        return {"status": "unavailable"}
    except ValueError as exc:
        # Distinguish "not found" from validation; both surface opaque codes.
        _LOG.warning("customers_advance failed for %r: %s", customer_id, exc)
        if "not found" in str(exc):
            raise HTTPException(status_code=404, detail="customer_not_found") from exc
        raise HTTPException(status_code=422, detail="customer_validation_error") from exc
    return {"status": "ok", "event": event.model_dump()}


@app.get("/customers/{customer_id}")
async def customers_get(customer_id: str) -> dict[str, Any]:
    check_capability("memory", "customers")
    store = _resolve_customer_store()
    cust = store.get_customer(customer_id)
    if cust is None:
        raise HTTPException(status_code=404, detail=f"customer not found: {customer_id}")
    return {"customer": cust.model_dump()}


@app.get("/customers")
async def customers_list(state: str | None = None, limit: int = 100) -> dict[str, Any]:
    check_capability("memory", "customers")
    store = _resolve_customer_store()
    try:
        items = store.list_customers(state=state, limit=limit)
    except ValueError as exc:
        _LOG.warning("customers_list failed: %s", exc)
        raise HTTPException(status_code=422, detail="customer_query_error") from exc
    return {"items": [c.model_dump() for c in items], "count": len(items)}


@app.get("/customers/{customer_id}/history")
async def customers_history(customer_id: str) -> dict[str, Any]:
    check_capability("memory", "customers")
    store = _resolve_customer_store()
    events = store.state_history(customer_id)
    return {"customer_id": customer_id, "events": [e.model_dump() for e in events]}


# --- /work dispatcher (envelope-style routing) --------------------------

@app.post("/work")
async def work_endpoint(body: _WorkBody) -> dict[str, Any]:
    action = (body.metadata or {}).get("action")

    # --- ingest_knowledge dispatch -----------------------------------------
    if action == "ingest_knowledge":
        check_capability("memory", "ingest_knowledge")
        from dataclasses import asdict

        payload = KnowledgeIngestPayload.model_validate(body.payload)
        # Jail any file-path ingest under the fixed ingest root (CWE-22).
        # _jail_source_path raises ValueError on an escape; the except below
        # turns that into a 422. handle() re-jails defensively regardless.
        try:
            source = (
                _jail_source_path(payload.source_path)
                if payload.source_path
                else None
            )
        except ValueError as exc:
            _LOG.warning("ingest source_path jail rejected: %s", exc)
            raise HTTPException(status_code=422, detail="invalid_source_path") from exc
        req = IngestRequest(
            source_path=source,
            documents=payload.documents,
            trust_level=TrustLevel(payload.trust_level),
            signature=payload.signature,
            chunk_size=payload.chunk_size,
            chunk_overlap=payload.chunk_overlap,
            batch_size=payload.batch_size,
            dry_run=payload.dry_run,
            embed=payload.embed,
            collection_label=payload.collection_label,
        )
        try:
            receipt = _INGEST_POD.handle(req)
        except (PermissionError, ValueError) as exc:
            _LOG.warning("knowledge ingest failed: %s", exc)
            raise HTTPException(status_code=422, detail="ingest_error") from exc
        return asdict(receipt)

    # --- customers dispatch ------------------------------------------------
    if action != "customers":
        raise HTTPException(status_code=400, detail=f"unknown_action: {action}")
    check_capability("memory", "customers")
    sub = (body.payload or {}).get("sub_action")
    store = _resolve_customer_store()
    try:
        if sub == "create":
            create = _CustomerCreateBody.model_validate(body.payload)
            cust = store.create_customer(
                email=create.email,
                name=create.name,
                company=create.company,
                source=create.source,
                metadata=create.metadata,
            )
            return {"status": "ok", "customer": cust.model_dump()}
        if sub == "advance":
            cid = body.payload.get("customer_id")
            if not cid:
                raise HTTPException(status_code=422, detail="customer_id required")
            adv = _CustomerAdvanceBody.model_validate(body.payload)
            event = store.advance_state(
                customer_id=cid,
                to_state=adv.to_state,
                reason=adv.reason,
                metadata=adv.metadata,
            )
            return {"status": "ok", "event": event.model_dump()}
        if sub == "get":
            cid = body.payload.get("customer_id")
            cust = store.get_customer(cid) if cid else None
            return {"customer": cust.model_dump() if cust else None}
        if sub == "list":
            items = store.list_customers(
                state=body.payload.get("state"),
                limit=int(body.payload.get("limit", 100)),
            )
            return {"items": [c.model_dump() for c in items], "count": len(items)}
        if sub == "history":
            cid = body.payload.get("customer_id")
            if not cid:
                raise HTTPException(status_code=422, detail="customer_id required")
            events = store.state_history(cid)
            return {"customer_id": cid, "events": [e.model_dump() for e in events]}
        raise HTTPException(status_code=400, detail=f"unknown_sub_action: {sub}")
    except CustomerStoreUnavailableError:
        return {"status": "unavailable"}
    except ValueError as exc:
        _LOG.warning("work customers dispatch failed: %s", exc)
        raise HTTPException(status_code=422, detail="customer_validation_error") from exc


@app.post("/graph/query")
async def graph_query(body: _GraphQueryBody) -> dict[str, Any]:
    check_capability("memory", "graph")
    client = _resolve_client()
    try:
        from backend.common import graph_schema
        graph_schema.allowed_query(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not client.available:
        return {"status": "unavailable", "rows": []}
    try:
        rows = client.query(body.name, **body.params)
    except ValueError as exc:
        _LOG.warning("graph_query execution failed: %s", exc)
        raise HTTPException(status_code=422, detail="graph_query_error") from exc
    return {"rows": rows}
