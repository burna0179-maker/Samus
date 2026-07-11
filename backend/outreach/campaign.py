"""Email-campaign builder — turns Apollo contacts into ready-to-send messages.

This is the deciding layer the outreach workcell never had: it takes a pool of
:class:`backend.outreach.apollo_source.ApolloContact`, applies suppression +
compliance + a per-run cap, composes each body, and emits validated
:class:`backend.outreach.models.OutreachMessageRequest` objects. The CLI
(run_campaign.py) then hands each to ``outreach.service.send_message``.

Pure + offline: no network, no SES, no AWS. The only optional outbound call is
LLM personalisation, which is off by default and routed through the budget gate
(``backend.common.llm_client.anthropic_messages``) with a deterministic
template fallback — so a budget denial or LLM outage never blocks a send.

CAN-SPAM: every composed body ends with a footer carrying the sender's physical
postal address and an unsubscribe line. A campaign with neither configured
refuses to build (fail-closed) rather than send non-compliant cold email.
"""
from __future__ import annotations

import json
import logging

from pydantic import BaseModel, ConfigDict, Field

from backend.common.codex import (
    CodexUnavailable,
    ProposedAction,
    check_action,
)
from backend.common.stake_sentence_guard import (
    StakeSentenceRejected,
    validate_stake_sentence,
)

from .apollo_source import ApolloContact
from .models import OutreachMessageRequest

_LOG = logging.getLogger("samus.outreach.campaign")


class OutreachStakeMissing(RuntimeError):
    """Raised when an outreach compose / build attempt has no stake_sentence.

    Fail-closed: the brief's invariant is that outreach refuses to fire
    without an operator-authored stake. ``compose_body`` raises BEFORE any
    LLM call so a missing stake also can't burn tokens.
    """


class CampaignConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str = "apollo_cold_v1"
    # First-class campaign attribution (HOTL Tranche 1). Threaded onto every
    # built OutreachMessageRequest so the send path can stamp business events
    # + conversion records with the campaign that fired them. Empty =
    # un-attributed (legacy configs keep working unchanged).
    campaign_id: str = ""
    subject: str = "Quick question about {company}"
    # The body always opens with the operator-authored stake_sentence on its
    # own line, blank line, then the templated content. The post-stake portion
    # is what the LLM (when opted in) is allowed to rewrite — the stake line
    # itself is rendered verbatim and must not be modified.
    # {first_name} {name} {title} {company} {industry} are fillable.
    body_template: str = (
        "{stake_sentence}\n\n"
        "Hi {first_name},\n\n"
        "Worth a quick look? Happy to send over a free teardown.\n\n"
        "Best,\n{from_name}"
    )
    from_name: str = "Samus"
    sender_postal_address: str = ""     # required (CAN-SPAM physical address)
    unsubscribe_url: str = ""           # required (CAN-SPAM opt-out)
    max_send: int = 25                  # per-run cap (SES reputation / warmup)
    require_verified_email: bool = True
    use_llm: bool = False               # opt-in personalisation (budget-gated)


# --- Re-engagement template (soft-no resurfacing) -----------------------
# Used by the re-engagement sweep (backend/crm/reengagement_sweep.py) when a
# prospect whose last_outcome was "not_interested" clears the cooldown. The
# opener references the prior conversation explicitly (so we are honest with
# the recipient) and leads with a discount / promotional angle rather than
# the cold "quick question" pitch. CAN-SPAM footer is still appended by
# compose_body — the operator's postal address + unsubscribe URL flow in
# through CampaignConfig exactly as for the cold path.
REENGAGEMENT_TEMPLATE_ID = "samus_reengagement_promotional_v1"
REENGAGEMENT_SUBJECT = "Circling back, {company} — a different offer"
REENGAGEMENT_BODY_TEMPLATE = (
    "{stake_sentence}\n\n"
    "Hi {first_name},\n\n"
    "We spoke a while back and the timing wasn't right — completely "
    "understood. Since then we've put together a promotional package "
    "designed for {company}-sized operations: lower entry price, same "
    "deliverable, no long commitment.\n\n"
    "Want me to send the one-page summary? Two-minute read, no call needed.\n\n"
    "Best,\n{from_name}"
)


def make_reengagement_config(
    *,
    from_name: str,
    sender_postal_address: str,
    unsubscribe_url: str,
) -> CampaignConfig:
    """Build the CampaignConfig used by the soft-no re-engagement sweep.

    Same compliance contract as the cold path (postal address + unsubscribe
    URL are required by ``build_messages`` / ``compose_body``); the only
    difference is the template_id + subject + body — so the recipient
    isn't getting a duplicate of the cold opener they already declined.
    """
    return CampaignConfig(
        template_id=REENGAGEMENT_TEMPLATE_ID,
        subject=REENGAGEMENT_SUBJECT,
        body_template=REENGAGEMENT_BODY_TEMPLATE,
        from_name=from_name,
        sender_postal_address=sender_postal_address,
        unsubscribe_url=unsubscribe_url,
    )


class CampaignResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    considered: int = 0
    suppressed: int = 0
    not_sendable: int = 0
    duplicate: int = 0
    built: int = 0
    capped: int = 0
    messages: list[OutreachMessageRequest] = Field(default_factory=list)


def _compliance_footer(cfg: CampaignConfig, recipient_email: str = "") -> str:
    # Per-recipient one-click link: the /unsubscribe page (outreach workcell,
    # public via the ingress) suppresses ?e=<address> directly. Without the
    # param the page still renders reply-to-opt-out instructions, but the
    # one-click path is what CAN-SPAM expects a reasonable person to find.
    url = cfg.unsubscribe_url
    if recipient_email:
        from urllib.parse import quote

        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}e={quote(recipient_email.strip().lower())}"
    return (
        "\n\n---\n"
        f"{cfg.sender_postal_address}\n"
        f"Unsubscribe: {url}"
    )


def _safe_format(
    template: str,
    contact: ApolloContact,
    cfg: CampaignConfig,
    *,
    stake_sentence: str | None = None,
) -> str:
    """Fill {placeholders} without KeyError on an unknown token.

    When ``stake_sentence`` is provided the ``{stake_sentence}`` token
    in the template is filled with it verbatim; if the template carries
    the token but no stake is passed, we raise — silently rendering an
    empty stake line would defeat the whole gate.
    """
    values = {
        "first_name": contact.first_name or "there",
        "name": contact.name or "there",
        "title": contact.title or "",
        "company": contact.company or "your business",
        "industry": contact.industry or "",
        "from_name": cfg.from_name,
    }
    if stake_sentence is not None:
        values["stake_sentence"] = stake_sentence

    class _Default(dict):
        def __missing__(self, key: str) -> str:  # noqa: D401
            if key == "stake_sentence":
                raise OutreachStakeMissing(
                    "_safe_format called with {stake_sentence} token but no value "
                    "(refusing to silently drop the gate)",
                )
            return ""
    return template.format_map(_Default(values))


def compose_body(
    contact: ApolloContact,
    cfg: CampaignConfig,
    *,
    stake_sentence: str | None = None,
) -> str:
    """Build one email body. Refuses without a stake_sentence (fail-closed).

    The stake_sentence is the operator-authored opener that turns this from a
    template blast into a chosen-prospect email. It is validated and rendered
    BEFORE any LLM call — a missing or guard-rejected sentence aborts the
    compose with no tokens spent.

    Templated by default; LLM-personalised when ``cfg.use_llm`` AND a
    budget-cleared Anthropic call succeeds. The compliance footer is always
    appended last, regardless of path. The LLM is instructed to keep the
    stake_sentence opener verbatim, but as a defence the stake line is
    re-prepended after any LLM rewrite as well.
    """
    if stake_sentence is None or not str(stake_sentence).strip():
        raise OutreachStakeMissing(
            "compose_body refused: stake_sentence is required and was empty/None",
        )
    stake = str(stake_sentence).strip()
    try:
        validate_stake_sentence(stake)
    except StakeSentenceRejected as exc:
        raise OutreachStakeMissing(
            f"compose_body refused: stake_sentence failed guard: {exc}",
        ) from exc

    body = _safe_format(cfg.body_template, contact, cfg, stake_sentence=stake)
    assert stake in body, (
        "compose_body invariant violated: stake_sentence missing from rendered body"
    )

    # Codex Validation Layer (chapter 12 / ADR-011): meta-gate over the
    # composed payload. Catches banned phrases in subject/body that the
    # stake-only guard wouldn't see, and surfaces G6/G7/G8 intent warnings.
    # If the Codex isn't loaded, fail closed by aborting compose — the
    # boot path (_ensure_codex_loaded) should have made this impossible.
    try:
        _verdict = check_action(ProposedAction(
            service="outreach",
            capability="send_message",
            action_kind="outreach_send",
            payload={
                "stake_sentence": stake,
                "subject": _safe_format(cfg.subject, contact, cfg),
                "body": body,
                "to_email": contact.email,
                "legitimacy_signal": getattr(contact, "legitimacy_signal", None),
            },
            proposed_by="outreach.compose_body",
            correlation_id=None,
        ))
    except CodexUnavailable as exc:
        raise OutreachStakeMissing(
            f"compose_body refused: Codex unavailable, refusing to send: {exc}",
        ) from exc
    if not _verdict.allowed:
        raise OutreachStakeMissing(
            f"compose_body refused by Codex rule {_verdict.violated_rule_id}: "
            f"{_verdict.reason} (ADR draft: {_verdict.drafted_adr_path})",
        )
    for _warning in _verdict.warnings:
        _LOG.info("compose_body codex advisory: %s", _warning)

    if cfg.use_llm:
        try:
            from backend.common.llm_client import anthropic_messages

            # The recipient greeting the template used ("Hi there," / "Hi
            # <first_name>,"). The stake line names the SENDER ("Alex flagged
            # ..."); without this guidance the model has been observed to greet
            # the recipient AS the sender ("Hi Alex"), which reads as a broken
            # mail-merge. Name the greeting explicitly and forbid the sender name.
            greeting_name = contact.first_name or "there"
            prompt = (
                "Rewrite this cold outreach email to be warm, specific, and "
                "under 120 words. Keep the FIRST line (it starts 'Alex flagged') "
                "and the greeting line ('Hi ...,') EXACTLY as written — do not "
                "change any name in them. 'Alex' is the SENDER of this email; "
                "NEVER address the recipient as Alex. Keep the sign-off line "
                "exactly. Do NOT add a subject line or any footer.\n\n"
                f"You are writing TO the owner of {contact.company} "
                f"({contact.industry}); address the recipient as "
                f"'{greeting_name}'.\n\nDraft:\n{body}"
            )
            drafted, _usage = anthropic_messages(
                workcell="outreach",
                api_key="unused",
                prompt=prompt,
                max_tokens=400,
            )
            if drafted and drafted.strip():
                rewritten = drafted.strip()
                # Defence in depth: even if the model edited or dropped the
                # stake line, force it back to the top verbatim. Outreach
                # MUST open with the operator's sentence.
                if stake not in rewritten:
                    rewritten = f"{stake}\n\n{rewritten}"
                # Greeting guard: keep the LLM rewrite ONLY if it greets the
                # recipient correctly. The model has greeted with the SENDER's
                # name ("Hi Alex") pulled from the stake line — a credibility
                # killer. If the expected greeting is absent, discard the rewrite
                # and keep the deterministic template body (correct greeting).
                if f"hi {greeting_name}".lower() in rewritten.lower():
                    body = rewritten
                else:
                    _LOG.info(
                        "compose_body llm greeting guard tripped (expected "
                        "'Hi %s'); using template body for %s",
                        greeting_name, contact.email,
                    )
        except Exception as exc:  # noqa: BLE001 — budget/LLM faults degrade to template
            _LOG.info(
                "compose_body llm unavailable (%s); using template",
                type(exc).__name__,
            )

    return body + _compliance_footer(cfg, recipient_email=contact.email or "")


def _stable_prospect_id(contact: ApolloContact) -> str:
    base = contact.person_id or (contact.email or "").lower()
    return f"apollo_{base}" if base else "apollo_unknown"


def _load_stake_sentence_from_opportunity(opportunity_id: str) -> str | None:
    """Pull the persisted stake_sentence off an Opportunity. None if absent.

    Used by the run_campaign wire-up when a contact maps to an Opportunity
    id. A missing Opportunity / empty stake returns None so the caller can
    skip the contact rather than crash.
    """
    if not (opportunity_id or "").strip():
        return None
    try:
        from backend.crm import service as crm_service
        opp = crm_service.get_opportunity(opportunity_id)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(
            "stake load failed for opportunity %s: %s",
            opportunity_id, exc,
        )
        return None
    if opp is None:
        return None
    stake = (opp.stake_sentence or "").strip()
    return stake or None


def build_messages(
    contacts: list[ApolloContact],
    cfg: CampaignConfig,
    *,
    suppression: set[str] | None = None,
    already_sent: set[str] | None = None,
    stake_sentences: dict[str, str] | None = None,
) -> CampaignResult:
    """Filter, compose, and cap. Returns a CampaignResult with the validated
    OutreachMessageRequest list plus per-stage counts for the run ledger.

    Refuses to build (ValueError) if the compliance fields are unset — a cold
    campaign with no postal address or unsubscribe link must not go out.

    ``stake_sentences`` is a per-email mapping (lower-cased) of the operator-
    authored stake_sentence for each prospect. A contact without an entry (or
    with an empty entry) is COUNTED AS ``not_sendable`` and skipped — fail
    closed. Outreach refuses to fire on a contact for whom no stake was
    authored.
    """
    if not (cfg.sender_postal_address.strip() and cfg.unsubscribe_url.strip()):
        raise ValueError(
            "CampaignConfig requires sender_postal_address AND unsubscribe_url "
            "(CAN-SPAM). Set SAMUS_OUTREACH_POSTAL_ADDRESS and "
            "SAMUS_OUTREACH_UNSUBSCRIBE_URL."
        )

    suppression = {e.lower() for e in (suppression or set())}
    already_sent = {e.lower() for e in (already_sent or set())}
    stake_lookup = {
        k.lower(): v
        for k, v in (stake_sentences or {}).items()
        if isinstance(k, str) and isinstance(v, str) and v.strip()
    }
    result = CampaignResult(considered=len(contacts))
    seen_this_run: set[str] = set()

    for contact in contacts:
        email = (contact.email or "").strip().lower()

        if cfg.require_verified_email and not contact.sendable:
            result.not_sendable += 1
            continue
        if not email:
            result.not_sendable += 1
            continue
        if email in suppression or email in already_sent:
            result.suppressed += 1
            continue
        if email in seen_this_run:
            result.duplicate += 1
            continue

        if len(result.messages) >= cfg.max_send:
            result.capped += 1
            continue

        stake = stake_lookup.get(email, "").strip()
        if not stake:
            _LOG.warning(
                "build_messages skipping %s — no stake_sentence on file", email,
            )
            result.not_sendable += 1
            continue

        try:
            body = compose_body(contact, cfg, stake_sentence=stake)
        except OutreachStakeMissing as exc:
            _LOG.warning(
                "build_messages refused %s (stake gate): %s", email, exc,
            )
            result.not_sendable += 1
            continue

        seen_this_run.add(email)
        result.messages.append(
            OutreachMessageRequest(
                prospect_id=_stable_prospect_id(contact),
                channel="email",
                template_id=cfg.template_id,
                to=contact.email,
                subject=_safe_format(cfg.subject, contact, cfg),
                body=body,
                company=contact.company,
                phone=contact.phone,
                campaign_id=cfg.campaign_id,
            )
        )

    result.built = len(result.messages)
    _LOG.info(
        "build_messages considered=%d built=%d suppressed=%d not_sendable=%d "
        "duplicate=%d capped=%d",
        result.considered, result.built, result.suppressed,
        result.not_sendable, result.duplicate, result.capped,
    )
    return result


def load_suppression(path: str) -> set[str]:
    """Read a newline-delimited emailed/suppression file into a lowercased set.

    Missing file -> empty set (first run). Each line may be a bare email or a
    JSON object with an ``email`` key (the campaign ledger format).
    """
    out: set[str] = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("{"):
                    try:
                        email = str(json.loads(line).get("email") or "")
                    except json.JSONDecodeError:
                        continue
                else:
                    email = line
                if email:
                    out.add(email.lower())
    except FileNotFoundError:
        pass
    except OSError as exc:
        _LOG.warning("load_suppression read failed for %s: %s", path, exc)
    return out
