"""Template registry for the proposal workcell.

Module-level :data:`TEMPLATE_REGISTRY` is populated at import time with the
small canonical set the $500 fixed-scope offer supports. ``find_template``
returns the first registry entry whose ``supported_<type>s`` list contains
``want``.
"""
from __future__ import annotations

from .models import TemplateDefinition, TemplateMaturity


TEMPLATE_REGISTRY: dict[str, TemplateDefinition] = {}


def _register(t: TemplateDefinition) -> None:
    TEMPLATE_REGISTRY[t.template_id] = t


_register(TemplateDefinition(
    template_id="slack_notify",
    type="notification",
    description="Post a Slack channel notification",
    tags=["slack", "chat"],
    supported_tools=["slack"],
    supported_notifications=["slack_message", "team_alert", "channel_post"],
    reliability_score=0.92,
    maturity=TemplateMaturity.PRODUCTION,
))

_register(TemplateDefinition(
    template_id="email_send",
    type="action",
    description="Send a transactional email (SES/SMTP)",
    tags=["email"],
    supported_tools=["ses", "smtp", "gmail"],
    supported_actions=["send_email", "email_notification", "transactional_email"],
    reliability_score=0.95,
    maturity=TemplateMaturity.PRODUCTION,
))

_register(TemplateDefinition(
    template_id="gsheet_write",
    type="action",
    description="Append a row to a Google Sheet",
    tags=["sheets", "logging"],
    supported_tools=["google_sheets"],
    supported_actions=["append_row", "log_to_sheet", "record_event"],
    reliability_score=0.88,
    maturity=TemplateMaturity.VERIFIED,
))

_register(TemplateDefinition(
    template_id="form_trigger",
    type="trigger",
    description="Fire when a hosted form is submitted",
    tags=["intake"],
    supported_tools=["typeform", "google_forms"],
    supported_triggers=["form_submitted", "intake_received", "lead_form"],
    reliability_score=0.90,
    maturity=TemplateMaturity.PRODUCTION,
))

_register(TemplateDefinition(
    template_id="webhook_in",
    type="trigger",
    description="Inbound webhook trigger (HTTP POST)",
    tags=["webhook", "integration"],
    supported_tools=["http"],
    supported_triggers=["webhook", "http_event", "callback"],
    reliability_score=0.93,
    maturity=TemplateMaturity.PRODUCTION,
))

_register(TemplateDefinition(
    template_id="crm_write",
    type="action",
    description="Create or update a CRM contact",
    tags=["crm"],
    supported_tools=["hubspot", "pipedrive", "salesforce"],
    supported_actions=["create_contact", "update_contact", "sync_to_crm"],
    reliability_score=0.85,
    maturity=TemplateMaturity.VERIFIED,
))


def find_template(type: str, want: str) -> TemplateDefinition | None:
    """Return the first registry entry that supports ``want`` for the given type."""
    if type == "trigger":
        attr = "supported_triggers"
    elif type == "action":
        attr = "supported_actions"
    elif type == "notification":
        attr = "supported_notifications"
    else:
        return None
    for tpl in TEMPLATE_REGISTRY.values():
        if tpl.type != type:
            continue
        if want in getattr(tpl, attr, []):
            return tpl
    return None


__all__ = ["TEMPLATE_REGISTRY", "find_template"]
