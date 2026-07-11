"""Known-client directory — email -> client identity lookup.

Scans ``clients/*/campaign.yaml`` for known-client email addresses (the
``approval_contact`` on each active campaign instance) and returns identity
information for correspondence routing.

Used by the intake pipeline (``backend.intake.email_classifier`` and
``backend.intake.gmail_poller``) so replies from signed/existing clients are
categorized as ``client_correspondence`` and routed to a dedicated
customer-service path instead of being lumped with cold-prospect replies.

Also exposes :func:`operator_addresses` for the outbound-forward detection
path: when the operator forwards their sent reply to Samus's polled inbox,
the classifier recognizes ``from_addr in operator_addresses()`` +
``forwarded body's original To: matches a known client`` = outbound
correspondence, and routes to the same client-thread with direction=outbound.

Design notes:

* The directory is a **thin projection** over ``clients/*/campaign.yaml`` —
  the YAML is the source of truth (see the operator directive Save->parse->
  show->approve->run for why one file per client is preferred over a table).
* Reads are cached with mtime-based invalidation so operators can drop a new
  ``clients/<slug>/campaign.yaml`` on disk without a container restart.
* Fail-soft end to end: an unreadable / malformed YAML is skipped with a
  warning; a missing ``clients/`` dir yields an empty directory — the
  classifier's ``client_correspondence`` category simply never fires and the
  email falls through to the existing ``business`` path.
* No LLM, no network. Pure disk read + string normalization.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

import yaml

_LOG = logging.getLogger("samus.crm.client_directory")

_CLIENTS_ROOT = Path(__file__).resolve().parents[2] / "clients"


@dataclass(frozen=True)
class KnownClient:
    """One resolved known-client contact keyed on their email address."""

    email: str
    client_id: str
    campaign_id: str
    template_id: str
    role: str  # "approval_contact" | "authorized_signatory" | ...
    display_name: str = ""
    vertical: str = ""
    docuseal_slug: str = ""
    yaml_path: str = ""


# --- cache -----------------------------------------------------------------

_lock = Lock()
_cache: dict[str, KnownClient] = {}
_cache_stamps: dict[str, float] = {}


def _campaign_yaml_paths() -> list[Path]:
    if not _CLIENTS_ROOT.exists():
        return []
    return sorted(_CLIENTS_ROOT.glob("*/campaign.yaml"))


def _norm_email(raw: Any) -> str:
    """Lowercase + strip; return '' for anything non-string / empty."""
    if not isinstance(raw, str):
        return ""
    return raw.strip().lower()


def _add_contact(
    directory: dict[str, KnownClient],
    email: str,
    *,
    client_id: str,
    campaign_id: str,
    template_id: str,
    role: str,
    display_name: str,
    vertical: str,
    docuseal_slug: str,
    yaml_path: str,
) -> None:
    e = _norm_email(email)
    if not e or "@" not in e:
        return
    # First entry for an email wins — deterministic (paths are sorted).
    if e in directory:
        return
    directory[e] = KnownClient(
        email=e,
        client_id=client_id,
        campaign_id=campaign_id,
        template_id=template_id,
        role=role,
        display_name=display_name,
        vertical=vertical,
        docuseal_slug=docuseal_slug,
        yaml_path=yaml_path,
    )


def _parse_one(path: Path, directory: dict[str, KnownClient]) -> None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — bad YAML skipped, not fatal
        _LOG.warning("client_directory: skipping unreadable %s: %s", path, exc)
        return
    if not isinstance(data, dict):
        return
    instance = data.get("campaign_instance") or {}
    if not isinstance(instance, dict):
        return
    client_id = str(instance.get("client_id") or "").strip()
    campaign_id = str(instance.get("campaign_id") or "").strip()
    template_id = str(instance.get("template_id") or "").strip()
    vertical = str(instance.get("vertical") or "").strip()
    docuseal_slug = str(instance.get("docuseal_slug") or "").strip()
    signatory = str(instance.get("authorized_signatory") or "").strip()

    inputs = instance.get("inputs") if isinstance(instance.get("inputs"), dict) else {}
    approval_contact = _norm_email(inputs.get("approval_contact"))
    signatory_from_inputs = str(inputs.get("authorized_signatory") or "").strip()
    display_name = signatory or signatory_from_inputs or client_id

    if approval_contact:
        _add_contact(
            directory,
            approval_contact,
            client_id=client_id,
            campaign_id=campaign_id,
            template_id=template_id,
            role="approval_contact",
            display_name=display_name,
            vertical=vertical,
            docuseal_slug=docuseal_slug,
            yaml_path=str(path),
        )

    # Any additional emails an operator explicitly lists under an optional
    # `additional_contacts: [{email: ..., role: ...}, ...]` block. Purely
    # forward-compatible — absent block = no-op.
    extras = inputs.get("additional_contacts")
    if isinstance(extras, list):
        for entry in extras:
            if not isinstance(entry, dict):
                continue
            _add_contact(
                directory,
                entry.get("email"),
                client_id=client_id,
                campaign_id=campaign_id,
                template_id=template_id,
                role=str(entry.get("role") or "additional_contact"),
                display_name=str(entry.get("name") or display_name),
                vertical=vertical,
                docuseal_slug=docuseal_slug,
                yaml_path=str(path),
            )


def _rebuild() -> dict[str, KnownClient]:
    directory: dict[str, KnownClient] = {}
    for p in _campaign_yaml_paths():
        _parse_one(p, directory)
    return directory


def _current_stamps() -> dict[str, float]:
    stamps: dict[str, float] = {}
    for p in _campaign_yaml_paths():
        try:
            stamps[str(p)] = p.stat().st_mtime
        except OSError:
            continue
    return stamps


def _refresh_if_stale() -> None:
    """Rebuild the cache if any campaign.yaml has changed on disk."""
    global _cache, _cache_stamps
    stamps = _current_stamps()
    if stamps == _cache_stamps and _cache:
        return
    _cache = _rebuild()
    _cache_stamps = stamps


def lookup_client(email: str) -> KnownClient | None:
    """Return the known-client record for an email, or None."""
    key = _norm_email(email)
    if not key:
        return None
    with _lock:
        _refresh_if_stale()
        return _cache.get(key)


def is_known_client(email: str) -> bool:
    return lookup_client(email) is not None


def all_known_clients() -> list[KnownClient]:
    """Return a snapshot of every known-client contact (for admin/debug)."""
    with _lock:
        _refresh_if_stale()
        return list(_cache.values())


def find_client_in_text(text: str) -> KnownClient | None:
    """Scan free text for markers of a known client. First match wins.

    Recognizes:
      * the client's ``display_name`` (e.g. "Kerry Brown")
      * the client's email (e.g. "<client-email>@example.com")
      * the email's domain (e.g. "<client-domain>.example")
      * the client_id humanized (e.g. "conquerors christian school",
        derived from "sample_school")
      * the campaign_id humanized (e.g. "conquerors christian school phased 2026")

    Case-insensitive. Longer markers match first (so
    "conquerors christian school" wins over "conquerors" if both existed
    as separate clients — unlikely in practice, but deterministic).

    Used by the classifier's outbound / content-based branch: when an
    operator-forwarded email talks ABOUT a client (rather than being TO
    a client's inbox), we still route it to that client's correspondence
    thread.

    Returns ``None`` for empty/no-match input; never raises.
    """
    if not text:
        return None
    lowered = text.lower()
    with _lock:
        _refresh_if_stale()
        clients = list(_cache.values())
    if not clients:
        return None

    # Build markers per client, sort by length descending so more specific
    # names win before shorter ones. Skip markers under 4 chars — too many
    # spurious hits (e.g. a random word ending "ing" for a hypothetical
    # client whose display_name starts with those letters).
    candidates: list[tuple[str, KnownClient]] = []
    for kc in clients:
        markers: set[str] = set()
        if kc.email:
            markers.add(kc.email.lower())
            if "@" in kc.email:
                domain = kc.email.split("@", 1)[1].lower()
                if len(domain) >= 6:
                    markers.add(domain)
        if kc.display_name and len(kc.display_name) >= 4:
            markers.add(kc.display_name.lower())
        # client_id "sample_school" -> "conquerors christian school"
        cid_human = kc.client_id.replace("_", " ").strip().lower()
        if cid_human and len(cid_human) >= 6:
            markers.add(cid_human)
        # campaign_id humanized — often distinctive
        camp_human = kc.campaign_id.replace("_", " ").strip().lower()
        if camp_human and len(camp_human) >= 8:
            markers.add(camp_human)

        for m in markers:
            if len(m) >= 4:
                candidates.append((m, kc))

    if not candidates:
        return None
    # Longest markers first for deterministic wins.
    candidates.sort(key=lambda t: len(t[0]), reverse=True)
    for marker, kc in candidates:
        if marker in lowered:
            return kc
    return None


def operator_addresses() -> set[str]:
    """Return the set of email addresses that identify the operator.

    An email whose ``from_addr`` is in this set + whose forwarded body's
    original ``To:`` matches a known client = outbound correspondence FROM
    the operator TO a client. Used by the classifier's forward-detection
    branch so operator BCC/forward workflow logs outbound automatically.

    Sourced from env (settings-agnostic to keep this module free of the
    heavy ``backend.common.config`` dependency and its Pydantic v2 model
    load — the classifier already runs on every inbound and must stay
    lean):

    * ``SENDGRID_FROM_EMAIL``      — the address campaign email sends AS
    * ``SAMUS_OPERATOR_EMAILS``    — optional comma-separated extras
      (e.g. `alex@hustleforge.tech,ops@hustleforge.tech`)

    All addresses are lowercased + stripped. Empty env values are skipped.
    """
    out: set[str] = set()
    from_email = (os.environ.get("SENDGRID_FROM_EMAIL") or "").strip().lower()
    if from_email:
        out.add(from_email)
    extras = (os.environ.get("SAMUS_OPERATOR_EMAILS") or "").strip().lower()
    if extras:
        for a in extras.split(","):
            a = a.strip()
            if a:
                out.add(a)
    return out


def is_operator_address(email: str) -> bool:
    """True if the email is one of the operator's outbound-from addresses."""
    return _norm_email(email) in operator_addresses()


__all__ = [
    "KnownClient",
    "lookup_client",
    "is_known_client",
    "all_known_clients",
    "operator_addresses",
    "is_operator_address",
]
