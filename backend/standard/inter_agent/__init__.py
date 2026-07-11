"""Inter-agent communication primitives for Samus.

Current surface: an SSE subscriber to the cross-agent Quorum Hub
(``_shared/quorum_hub/server.py``, default ``http://127.0.0.1:8090``).
Samus already publishes governance decisions to the hub via
:mod:`_shared.quorum_hub.client`; this module is the receive side.

From inside a Docker container the hub is reached at
``http://host.docker.internal:8090/events`` — the URL is configurable via
the ``QUORUM_HUB_SSE_URL`` env var. The compose service for the gateway
declares ``host.docker.internal:host-gateway`` so Linux Docker hosts get
the same alias Docker Desktop provides on Windows by default.

Boot-safety contract: a missing/unreachable hub MUST NOT block Samus
boot. The subscriber loop catches every transport error, logs, and
retries with capped exponential backoff. Set
``QUORUM_HUB_SUBSCRIBE_DISABLED=1`` to skip starting the subscriber at
all (test envs, single-container dev runs).
"""

from __future__ import annotations

from .event_handler import (
    HubEventHandler,
    dispatch,
    register_handler,
    unregister_handler,
)
from .hub_subscriber import (
    DEFAULT_HUB_SSE_URL,
    ENV_HUB_DISABLED,
    ENV_HUB_SSE_URL,
    HubSubscriber,
    get_subscriber,
    is_subscribe_disabled,
)
from .quorum_vote_route import (
    ENV_VOTING_ENABLED,
    is_voting_enabled,
)
from .quorum_vote_route import register as register_quorum_vote_route
from .quorum_voter import decide_ballot

__all__ = [
    "DEFAULT_HUB_SSE_URL",
    "ENV_HUB_DISABLED",
    "ENV_HUB_SSE_URL",
    "ENV_VOTING_ENABLED",
    "HubEventHandler",
    "HubSubscriber",
    "decide_ballot",
    "dispatch",
    "get_subscriber",
    "is_subscribe_disabled",
    "is_voting_enabled",
    "register_handler",
    "register_quorum_vote_route",
    "unregister_handler",
]
