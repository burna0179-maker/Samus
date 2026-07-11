"""Samus governance subsystem (depth layer v0.3 + Directed Capability v0.6).

Load-bearing for outreach/CRM/subscription paths.
"""

from .efh_evaluator import EthicalFailureHandler
from .elegance_scorer import ElegancePlan, score_elegance
from .pdc_composite import PDCFinding, run_pdc
from .protocol_layer import (
    CommercialActionRefusal,
    ProtocolLayer,
    get_protocol_layer,
    reset_protocol_layer,
)

__all__ = [
    "CommercialActionRefusal",
    "ElegancePlan",
    "EthicalFailureHandler",
    "PDCFinding",
    "ProtocolLayer",
    "get_protocol_layer",
    "reset_protocol_layer",
    "run_pdc",
    "score_elegance",
]
