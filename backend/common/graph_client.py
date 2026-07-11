"""Neo4j driver wrapper with circuit-breaker integration.

Per doc Section 10 (Neo4j Knowledge Graph Layer). Lazy-initializes the driver
from settings; if the database is unreachable and ``neo4j_required`` is False,
all operations degrade to graceful no-ops so the rest of the agent keeps
running. If ``neo4j_required`` is True, connection failures raise.

Writes are routed through :mod:`backend.common.graph_schema` for validation
and queries are restricted to the named allowlist there — arbitrary Cypher is
not exposed.
"""
from __future__ import annotations

import logging
import re
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from .retry import CircuitState
from .settings import get_settings
from . import graph_schema

_LOG = logging.getLogger("samus.common.graph_client")

# Cypher does not parameterise node labels or property keys, so the few places
# that interpolate them (write_node, promote_node, nodes_in_tier) restrict the
# value to a plain identifier — defence-in-depth against Cypher injection even
# though current callers pass code-controlled, schema-derived names.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_safe_identifier(name: str) -> bool:
    """True iff ``name`` is a bare identifier safe to interpolate into Cypher."""
    return bool(name) and bool(_IDENTIFIER_RE.match(name))


class GraphClient:
    """Lazy Neo4j driver wrapper.

    The driver is constructed on first use. On :class:`neo4j.exceptions.ServiceUnavailable`
    or :class:`neo4j.exceptions.AuthError` during initial connect, the client
    marks itself unavailable and (unless ``neo4j_required`` is True) all
    subsequent operations are graceful no-ops.
    """

    def __init__(
        self,
        uri: str | None = None,
        user: str | None = None,
        password: str | None = None,
        *,
        database: str | None = None,
        required: bool | None = None,
        circuit: CircuitState | None = None,
    ) -> None:
        settings = get_settings()
        self._uri = uri if uri is not None else settings.neo4j_uri
        self._user = user if user is not None else settings.neo4j_user
        self._password = password if password is not None else settings.neo4j_password
        self._database = database if database is not None else settings.neo4j_database
        self._required = bool(settings.neo4j_required) if required is None else bool(required)
        # W-4: ``off`` | ``label``. Snapshot at construction, same as the other
        # settings-derived fields above; ``reset_client()`` rebuilds the
        # singleton so a reloaded SAMUS_KG_TIER_MODE is picked up.
        self._tier_mode = settings.kg_tier_mode
        self._circuit = circuit or CircuitState(threshold=5, recovery_seconds=30.0)
        self._driver: Any | None = None
        self._available: bool | None = None  # None = not yet probed
        self._lock = threading.Lock()

    @property
    def database(self) -> str:
        """The Neo4j database this client targets (e.g. ``samus``, ``hivemind``)."""
        return self._database

    @property
    def tier_mode(self) -> str:
        """Knowledge-graph tier mode (``off`` | ``label``). See W-4 / graph_schema."""
        return self._tier_mode

    @property
    def tier_enabled(self) -> bool:
        """True when tiering is active (``SAMUS_KG_TIER_MODE=label``)."""
        return self._tier_mode == "label"

    # --- driver lifecycle ---------------------------------------------------

    def _ensure_driver(self) -> Any | None:
        """Create the driver if not already created. Returns the driver or None."""
        if self._driver is not None:
            return self._driver
        if self._available is False and not self._required:
            return None
        with self._lock:
            if self._driver is not None:
                return self._driver
            try:
                from neo4j import GraphDatabase  # type: ignore
                from neo4j.exceptions import ServiceUnavailable, AuthError  # type: ignore
            except Exception as exc:  # pragma: no cover - driver missing
                _LOG.warning("neo4j_driver_import_failed", extra={"err": str(exc)})
                self._available = False
                if self._required:
                    raise
                return None

            auth = (self._user, self._password) if self._password else None
            try:
                driver = GraphDatabase.driver(self._uri, auth=auth)
                driver.verify_connectivity()
            except (ServiceUnavailable, AuthError, OSError) as exc:
                _LOG.warning(
                    "neo4j_unavailable",
                    extra={"uri": self._uri, "err": str(exc)},
                )
                self._available = False
                self._circuit.record_failure()
                if self._required:
                    raise
                return None
            except Exception as exc:
                _LOG.warning(
                    "neo4j_connect_unexpected_error",
                    extra={"uri": self._uri, "err": str(exc)},
                )
                self._available = False
                self._circuit.record_failure()
                if self._required:
                    raise
                return None

            self._driver = driver
            self._available = True
            self._circuit.record_success()
            return self._driver

    @property
    def available(self) -> bool:
        """True if the driver is currently usable (probes on first access)."""
        if self._available is None:
            self._ensure_driver()
        return bool(self._available) and self._circuit.can_call()

    @property
    def is_open(self) -> bool:
        """Circuit-breaker convenience: the *circuit* is open (calls refused)."""
        return not self._circuit.can_call()

    @property
    def is_closed(self) -> bool:
        """Circuit-breaker convenience: the *circuit* is closed (calls allowed)."""
        return self._circuit.can_call()

    def close(self) -> None:
        with self._lock:
            if self._driver is not None:
                try:
                    self._driver.close()
                except Exception as exc:  # pragma: no cover - best effort
                    _LOG.warning("neo4j_close_error", extra={"err": str(exc)})
                self._driver = None
            self._available = None

    # --- session context ----------------------------------------------------

    @contextmanager
    def session(self, *, database: str | None = None) -> Iterator[Any | None]:
        """Yield a raw neo4j session pinned to ``database`` (default = ``self._database``)."""
        driver = self._ensure_driver()
        if driver is None or not self._circuit.can_call():
            yield None
            return
        db = database or self._database
        try:
            with driver.session(database=db) as sess:
                yield sess
        except Exception as exc:
            _LOG.warning("neo4j_session_failed", extra={"err": str(exc), "database": db})
            self._circuit.record_failure()
            yield None

    # --- helpers ------------------------------------------------------------

    def _run(self, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Execute a Cypher statement; return rows as plain dicts."""
        driver = self._ensure_driver()
        if driver is None:
            if self._required:
                raise RuntimeError("neo4j unavailable but neo4j_required=True")
            return []
        if not self._circuit.can_call():
            if self._required:
                raise RuntimeError("neo4j circuit open")
            return []
        from neo4j.exceptions import ServiceUnavailable, AuthError  # type: ignore
        try:
            with driver.session(database=self._database) as sess:
                result = sess.run(cypher, **params)
                rows = [record.data() for record in result]
            self._circuit.record_success()
            return rows
        except (ServiceUnavailable, AuthError, OSError) as exc:
            _LOG.warning("neo4j_run_failed", extra={"err": str(exc), "cypher": cypher[:80]})
            self._circuit.record_failure()
            self._available = False
            if self._required:
                raise
            return []
        except Exception as exc:
            self._circuit.record_failure()
            _LOG.warning("neo4j_run_unexpected", extra={"err": str(exc), "cypher": cypher[:80]})
            if self._required:
                raise
            return []

    # --- public API ---------------------------------------------------------

    def init_schema(self) -> bool:
        """Create indexes for every (label, property) in graph_schema.INDEXES.

        Returns True if the schema commands were issued, False if the driver
        is unavailable.
        """
        if not self.available:
            return False
        for label, prop in graph_schema.INDEXES:
            cypher = f"CREATE INDEX IF NOT EXISTS FOR (n:{label}) ON (n.{prop})"
            self._run(cypher, {})
        return True

    def write_node(self, label: str, properties: dict[str, Any]) -> bool:
        """MERGE a node by its primary key, then SET the remaining properties.

        Returns True on success, False on no-op (unavailable).
        """
        graph_schema.validate_node(label, properties)
        if not self.available:
            return False
        pk = graph_schema.primary_key(label)
        pk_value = properties[pk]
        cypher = (
            f"MERGE (n:{label} {{{pk}: $pk_value}}) "
            f"SET n += $properties "
            f"RETURN n"
        )
        self._run(cypher, {"pk_value": pk_value, "properties": dict(properties)})
        return self.available

    def write_relationship(
        self,
        source_label: str,
        source_key: Any,
        rel_type: str,
        target_label: str,
        target_key: Any,
    ) -> bool:
        """MERGE a relationship between two nodes identified by their primary keys."""
        graph_schema.validate_relationship(source_label, rel_type, target_label)
        if not self.available:
            return False
        source_pk = graph_schema.primary_key(source_label)
        target_pk = graph_schema.primary_key(target_label)
        cypher = (
            f"MATCH (s:{source_label} {{{source_pk}: $source_key}}), "
            f"(t:{target_label} {{{target_pk}: $target_key}}) "
            f"MERGE (s)-[r:{rel_type}]->(t) "
            f"RETURN r"
        )
        self._run(cypher, {"source_key": source_key, "target_key": target_key})
        return self.available

    def query(self, name: str, **params: Any) -> list[dict[str, Any]]:
        """Execute an allowlisted named query and return rows as dicts."""
        cypher = graph_schema.allowed_query(name)
        if not self.available:
            return []
        return self._run(cypher, params)

    # --- knowledge-graph tiering (W-4 — CLOUD_RUN_DEPLOY.md §5.2 / D-6) -----

    def promote_node(
        self,
        label: str,
        key_value: Any,
        *,
        key_property: str | None = None,
    ) -> bool:
        """Flip a node's ``tier`` property to ``hivemind`` (W-4 promotion).

        The node is matched by ``key_property`` (default: the label's schema
        primary key from :mod:`graph_schema`) equal to ``key_value``. Returns
        True if a node was matched and updated, False on a no-op (driver
        unavailable, or no such node). Promotion is an explicit operator/rule
        action and is honoured regardless of ``SAMUS_KG_TIER_MODE`` — the mode
        only governs the *default* ``tier`` stamped at write time.

        Raises ``ValueError`` if ``label`` / ``key_property`` are not bare
        identifiers (they are interpolated into Cypher, which cannot
        parameterise them).
        """
        if key_property is None:
            key_property = graph_schema.primary_key(label)
        if not _is_safe_identifier(label):
            raise ValueError(f"unsafe node label: {label!r}")
        if not _is_safe_identifier(key_property):
            raise ValueError(f"unsafe key property: {key_property!r}")
        if not self.available:
            return False
        cypher = (
            f"MATCH (n:{label} {{{key_property}: $key_value}}) "
            f"SET n.{graph_schema.TIER_PROPERTY} = $tier "
            f"RETURN n"
        )
        rows = self._run(
            cypher,
            {"key_value": key_value, "tier": graph_schema.TIER_HIVEMIND},
        )
        return bool(rows)

    def nodes_in_tier(
        self,
        tier: str,
        *,
        label: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return nodes whose ``tier`` property equals ``tier``.

        Optionally filtered to a single ``label``. This is the read side of
        the tier convention — the §7 experience feed (W-6) calls it with
        ``tier='hivemind'`` to collect the promoted set. Returns [] when the
        driver is unavailable. Raises ``ValueError`` on an unknown tier or an
        unsafe label.
        """
        if tier not in graph_schema.VALID_TIERS:
            raise ValueError(f"unknown tier: {tier!r}")
        if label is not None and not _is_safe_identifier(label):
            raise ValueError(f"unsafe node label: {label!r}")
        if not self.available:
            return []
        limit = max(1, min(int(limit), 1000))
        match = f"(n:{label})" if label else "(n)"
        cypher = (
            f"MATCH {match} WHERE n.{graph_schema.TIER_PROPERTY} = $tier "
            f"RETURN n LIMIT $limit"
        )
        return self._run(cypher, {"tier": tier, "limit": limit})


# --- module-level singleton -------------------------------------------------

_CLIENT: GraphClient | None = None
_CLIENT_LOCK = threading.Lock()


def get_client() -> GraphClient:
    """Lazy module-level singleton."""
    global _CLIENT
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:
                _CLIENT = GraphClient()
    return _CLIENT


def reset_client() -> None:
    """Test helper: drop and close the module-level singleton."""
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            try:
                _CLIENT.close()
            except Exception:
                pass
        _CLIENT = None
