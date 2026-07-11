"""Compose-only outreach entry for the cash engine — no send.

The cash engine needs the *initial outreach packet* (subject + body) for a
prospect, composed through outreach's real, fail-closed path — stake
validation, the Codex ``outreach_send`` gate (G1 stake / G8 legitimacy / G2
banned phrases), and the CAN-SPAM compliance footer — but it must NOT send.
The actual SES send (``outreach.service.send_message``) has no dry-run mode;
the cash engine decides separately, behind a default-off flag, whether to
hand the composed packet to the sender.

This lives in the outreach workcell because building the ``ApolloContact`` +
``CampaignConfig`` and running ``compose_body`` is outreach-domain logic.
``compose_initial_packet`` raises :class:`OutreachStakeMissing` when the
compose path is blocked (missing/guard-rejected stake, a Codex rule, or no
legitimacy signal) — the caller maps that to a Codex halt.
"""
from __future__ import annotations

from typing import Any

from backend.common.settings import get_settings

from .apollo_source import ApolloContact
from .campaign import (
    CampaignConfig,
    OutreachStakeMissing,
    _safe_format,
    compose_body,
    make_reengagement_config,
)

__all__ = [
    "compose_initial_packet",
    "compose_reengagement_packet",
    "OutreachStakeMissing",
]


def _first_name(full_name: str) -> str:
    name = (full_name or "").strip()
    return name.split()[0] if name else ""


def _select_cold_send_email(prospect: Any) -> str:
    """Pick a cold-sendable ``to`` address for ``prospect`` — or ``""``.

    B-5 quality gate. The vetted address is ``owner_email`` (Apollo enrichment
    prefers a personal-looking mailbox), but it can degrade to a footer scrape
    (``bugreport@moatable.com``) when no personal address was found. Cold-mailing
    a role / system mailbox burns the SendGrid sender domain, so:

      1. use ``owner_email`` when it is cold-sendable;
      2. else fall back to the first cold-sendable address in ``contact_emails``
         (the scraped list, ``"; "``-joined);
      3. else return ``""`` — the contact is not cold-sendable and must not be
         sent (the caller blocks the compose, fail-closed).

    The scraped address stays on the record untouched; this only governs which
    address — if any — is used for the cold send.
    """
    from backend.prospecting.contact_validation import is_cold_sendable_email

    owner_email = str(getattr(prospect, "owner_email", "") or "").strip()
    if is_cold_sendable_email(owner_email):
        return owner_email

    raw_contacts = str(getattr(prospect, "contact_emails", "") or "")
    for candidate in raw_contacts.split(";"):
        addr = candidate.strip()
        if is_cold_sendable_email(addr):
            return addr
    return ""


def compose_initial_packet(
    *,
    prospect: Any,
    stake_sentence: str,
    legitimacy_signal: str,
    cfg: CampaignConfig | None = None,
) -> dict[str, str]:
    """Compose (subject, body, to) for the initial outreach. Pure — no send.

    ``prospect`` is a CRM Prospect / ProspectRecord. Raises
    ``OutreachStakeMissing`` if the outreach compose gate blocks.
    """
    settings = get_settings()
    cfg = cfg or CampaignConfig(
        from_name=(getattr(settings, "sendgrid_from_name", "") or "Samus"),
        sender_postal_address=(getattr(settings, "sender_postal_address", "") or ""),
        unsubscribe_url=(getattr(settings, "unsubscribe_url", "") or ""),
    )

    # B-5: choose a cold-sendable ``to`` — never a role/system footer scrape.
    # Empty means no address on file is safe to cold-send; refuse to compose
    # (fail-closed) so the caller marks the contact not-cold-sendable instead
    # of burning the sender domain.
    to_email = _select_cold_send_email(prospect)
    if not to_email:
        raise OutreachStakeMissing(
            "no cold-sendable email for prospect "
            f"{str(getattr(prospect, 'prospect_id', '') or '') or '<unknown>'} "
            "(owner_email + contact_emails are empty, malformed, or role/system "
            "mailboxes) — not cold-sendable"
        )

    owner_name = str(getattr(prospect, "owner_name", "") or "")
    contact = ApolloContact(
        first_name=_first_name(owner_name),
        name=owner_name or str(getattr(prospect, "company_name", "") or ""),
        company=str(getattr(prospect, "company_name", "") or ""),
        industry=str(getattr(prospect, "industry", "") or ""),
        email=to_email,
        city=str(getattr(prospect, "city", "") or ""),
        state=str(getattr(prospect, "state", "") or ""),
        phone=str(getattr(prospect, "phone", "") or ""),
        legitimacy_signal=legitimacy_signal or "",
    )

    # compose_body runs the stake guard + Codex outreach_send gate + footer.
    body = compose_body(contact, cfg, stake_sentence=stake_sentence)
    subject = _safe_format(cfg.subject, contact, cfg)
    return {"subject": subject, "body": body, "to": contact.email}


def compose_reengagement_packet(
    *,
    prospect: Any,
    stake_sentence: str,
    legitimacy_signal: str,
) -> dict[str, str]:
    """Compose the soft-no re-engagement packet — promotional template, not cold.

    Identical contract to :func:`compose_initial_packet` (raises
    ``OutreachStakeMissing`` on a blocked compose), but builds the
    CampaignConfig via :func:`backend.outreach.campaign.make_reengagement_config`
    so the body leads with a discount/new-product angle and a distinct
    template_id flows down into ``OutreachMessageRequest`` for the send-side
    audit trail. Used by the cash_engine outreach stage when
    ``CashEngineState.trigger_source == "reengagement"``.
    """
    settings = get_settings()
    cfg = make_reengagement_config(
        from_name=(getattr(settings, "sendgrid_from_name", "") or "Samus"),
        sender_postal_address=(getattr(settings, "sender_postal_address", "") or ""),
        unsubscribe_url=(getattr(settings, "unsubscribe_url", "") or ""),
    )
    return compose_initial_packet(
        prospect=prospect,
        stake_sentence=stake_sentence,
        legitimacy_signal=legitimacy_signal,
        cfg=cfg,
    )
