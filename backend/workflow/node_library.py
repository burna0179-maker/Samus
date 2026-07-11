"""Maps the scope_planner vocabulary -> concrete n8n node specs.

The scope planner emits a normalized vocabulary (triggers like ``form_submission``
/ ``payment_received``, actions like ``post_to_slack`` / ``create_crm_record``,
tools like ``stripe`` / ``hubspot``). This module turns each label into a real
n8n node type with sensible default parameters and a credential hint. Some
actions are **tool-aware** (e.g. ``create_crm_record`` becomes a HubSpot vs.
Salesforce vs. Pipedrive node depending on ``plan.tools``). Anything unknown
falls back to a generic ``httpRequest`` node so a plan *always* compiles.

A "spec" is a plain dict: ``{type, type_version, parameters, credential_hint}``.
The compiler wraps it in an :class:`backend.workflow.models.N8nNode` with a name +
position. Parameters intentionally carry empty placeholders (channel ids, sheet
ids, phone numbers) — those are documented in the runbook and filled in n8n; we
never fabricate credential ids.
"""

from __future__ import annotations

from typing import Any

Spec = dict[str, Any]


def _spec(node_type: str, version: float, parameters: dict[str, Any], cred: str = "") -> Spec:
    return {
        "type": node_type,
        "type_version": version,
        "parameters": parameters,
        "credential_hint": cred,
    }


# --- triggers --------------------------------------------------------------


def _webhook(path: str) -> Spec:
    return _spec(
        "n8n-nodes-base.webhook",
        2.0,
        {"httpMethod": "POST", "path": path, "responseMode": "onReceived"},
    )


_TRIGGER_SPECS = {
    "form_submission": lambda: _webhook("form-submission"),
    "new_lead": lambda: _webhook("new-lead"),
    "booking_created": lambda: _webhook("booking-created"),
    "schedule_recurring": lambda: _spec(
        "n8n-nodes-base.scheduleTrigger",
        1.2,
        {"rule": {"interval": [{"field": "days", "daysInterval": 1, "triggerAtHour": 6}]}},
    ),
    "email_received": lambda: _spec(
        "n8n-nodes-base.gmailTrigger",
        1.2,
        {"pollTimes": {"item": [{"mode": "everyMinute"}]}, "simple": True, "filters": {}},
        cred="Gmail OAuth2",
    ),
    "payment_received": lambda: _spec(
        "n8n-nodes-base.stripeTrigger",
        1.0,
        {"events": ["checkout.session.completed"]},
        cred="Stripe API",
    ),
}
_DEFAULT_TRIGGER = lambda: _webhook("workflow-trigger")  # noqa: E731


# --- tool-aware action helpers ---------------------------------------------


def _crm_spec(tools: list[str]) -> Spec:
    if "hubspot" in tools:
        return _spec(
            "n8n-nodes-base.hubspot", 2.1, {"resource": "contact", "operation": "create"}, "HubSpot"
        )
    if "salesforce" in tools:
        return _spec(
            "n8n-nodes-base.salesforce",
            1.0,
            {"resource": "lead", "operation": "create"},
            "Salesforce",
        )
    if "pipedrive" in tools:
        return _spec(
            "n8n-nodes-base.pipedrive",
            1.0,
            {"resource": "deal", "operation": "create"},
            "Pipedrive",
        )
    return _spec(
        "n8n-nodes-base.httpRequest",
        4.2,
        {"method": "POST", "url": "", "sendBody": True},
        "HTTP Header Auth (CRM)",
    )


def _sheet_spec(tools: list[str]) -> Spec:
    if "airtable" in tools:
        return _spec("n8n-nodes-base.airtable", 2.1, {"operation": "create"}, "Airtable API")
    if "notion" in tools:
        return _spec(
            "n8n-nodes-base.notion",
            2.2,
            {"resource": "databasePage", "operation": "create"},
            "Notion API",
        )
    return _spec(
        "n8n-nodes-base.googleSheets",
        4.5,
        {"resource": "sheet", "operation": "append", "documentId": "", "sheetName": ""},
        "Google Sheets OAuth2",
    )


def _email_spec(tools: list[str]) -> Spec:
    if "gmail" in tools:
        return _spec(
            "n8n-nodes-base.gmail",
            2.1,
            {
                "resource": "message",
                "operation": "send",
                "sendTo": "",
                "subject": "",
                "message": "",
            },
            "Gmail OAuth2",
        )
    return _spec(
        "n8n-nodes-base.emailSend",
        2.1,
        {"fromEmail": "", "toEmail": "", "subject": "", "text": ""},
        "SMTP",
    )


def _slack_spec(_tools: list[str]) -> Spec:
    return _spec(
        "n8n-nodes-base.slack",
        2.2,
        {
            "resource": "message",
            "operation": "post",
            "select": "channel",
            "channelId": "",
            "text": "",
        },
        "Slack API",
    )


_ACTION_SPECS = {
    "post_to_slack": _slack_spec,
    "send_email": _email_spec,
    "create_crm_record": _crm_spec,
    "append_to_sheet": _sheet_spec,
    "send_sms": lambda _t: _spec(
        "n8n-nodes-base.twilio",
        1.0,
        {"resource": "sms", "operation": "send", "from": "", "to": "", "message": ""},
        "Twilio API",
    ),
    "generate_invoice": lambda _t: _spec(
        "n8n-nodes-base.httpRequest",
        4.2,
        {"method": "POST", "url": "https://api.stripe.com/v1/invoices", "sendBody": True},
        "Stripe API (HTTP Header Auth)",
    ),
    "route_to_owner": lambda _t: _spec(
        "n8n-nodes-base.set",
        3.4,
        {"assignments": {"assignments": [{"name": "owner", "value": "", "type": "string"}]}},
    ),
    "schedule_followup": lambda _t: _spec(
        "n8n-nodes-base.wait", 1.1, {"amount": 1, "unit": "days"}
    ),
}


# --- notifications ---------------------------------------------------------

_NOTIFICATION_SPECS = {
    "notify_operator": _slack_spec,
    "discord_webhook": lambda _t: _spec(
        "n8n-nodes-base.discord",
        2.0,
        {
            "resource": "message",
            "operation": "sendLegacy",
            "authentication": "webhook",
            "content": "",
        },
        "Discord Webhook",
    ),
}


def _http_fallback(label: str) -> Spec:
    return _spec(
        "n8n-nodes-base.httpRequest",
        4.2,
        {"method": "POST", "url": "", "sendBody": True},
        f"HTTP ({label})",
    )


# --- public resolver -------------------------------------------------------


def trigger_spec(label: str) -> Spec:
    return _TRIGGER_SPECS.get(label, _DEFAULT_TRIGGER)()


def action_spec(label: str, tools: list[str]) -> Spec:
    builder = _ACTION_SPECS.get(label)
    return builder(tools) if builder else _http_fallback(label)


def notification_spec(label: str, tools: list[str]) -> Spec:
    builder = _NOTIFICATION_SPECS.get(label)
    return builder(tools) if builder else _http_fallback(label)


def error_alert_spec(tools: list[str]) -> Spec:
    """The failure-alert node for the error branch — Discord if the plan used it,
    else Slack (every HustleForge example ships failure alerts)."""
    if "discord" in tools or "discord_webhook" in tools:
        return notification_spec("discord_webhook", tools)
    return _slack_spec(tools)


__all__ = ["trigger_spec", "action_spec", "notification_spec", "error_alert_spec", "Spec"]
