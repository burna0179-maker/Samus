"""Samus contract-signing integration (self-hosted DocuSeal).

Flow: a campaign step generates a service-agreement signing link for a prospect
-> Samus sends the link -> the customer signs -> DocuSeal delivers a
``submission.completed`` webhook -> Samus advances the opportunity.

The DocuSeal instance is self-hosted in the Samus stack (``samus-docuseal``);
API calls and the completion webhook stay on the internal docker network, while
the customer-facing signing links are published through the Caddy ingress.
"""

from .models import (
    ContractParty,
    ContractResult,
    DocuSealField,
    DocuSealFieldArea,
    DocuSealWebhookEvent,
    ProposalAgreementRequest,
    ServiceAgreementRequest,
)

__all__ = [
    "ContractParty",
    "ContractResult",
    "DocuSealField",
    "DocuSealFieldArea",
    "DocuSealWebhookEvent",
    "ProposalAgreementRequest",
    "ServiceAgreementRequest",
]
