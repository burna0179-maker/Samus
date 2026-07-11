"""ISV (InitialStateVector) consumer — v0.6.0 Directed Capability Protocol.

Pulls Major-signed ISVs from Optimus dispatch and constrains Samus's
commercial action surface to the ISV's assigned_goals + resource_budget.
"""

from .consumer import IsvConsumer, ProtocolViolation, NoActiveIsvError

__all__ = ["IsvConsumer", "ProtocolViolation", "NoActiveIsvError"]
