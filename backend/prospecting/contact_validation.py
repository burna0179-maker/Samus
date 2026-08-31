"""Contact-information validation + misdirection reasoning (prospecting).

Samus collects prospect contact emails from two very different places: the
enrichment crawler scrapes them off a business's own pages, and the operator
captures them on a live call ("just email info@..."). Both are spoofable — a
crawler picks up a garbled string, and a gatekeeper reads out a brush-off
address to get the caller off the phone.

A real example, 2026-05-21: a receptionist offered ``info@juniper-.com`` for
*Juniper Modern Dentistry*. The domain label ``juniper-`` ends in a hyphen,
which no hostname label may (RFC 1035 §2.3.1), so that address can never
receive mail — a textbook misdirection that a permissive extraction regex
would nonetheless wave through.

This module is the deterministic, zero-token check for exactly that. It does
two things — *identify* and *reason*:

  * :func:`is_valid_email_syntax` / :func:`email_syntax_error` — apply the
    hostname rules so a structurally-impossible address (``info@juniper-.com``,
    ``info@-x.com``, ``info@x..com``) is caught for what it is.
  * :func:`assess_email` — goes further: given what Samus already knows about
    the business (its website domain, its name, any on-file address), it
    grades an address ``valid`` / ``malformed`` / ``suspect`` and explains the
    reasoning in plain language — including spotting a domain that *resembles*
    a real one (the tell of a garbled or deliberately-misdirected address).

Pure + zero-I/O — safe to run on every prospect and every call note.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "EmailAssessment",
    "email_syntax_error",
    "is_valid_email_syntax",
    "is_cold_sendable_email",
    "assess_email",
    "main",
]


# Local part: RFC 5321 dot-atom plus the common special chars. We validate the
# local part loosely — misdirection lives in the *domain*, which we check hard.
_LOCAL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+$")
# A single DNS label (RFC 1035 §2.3.1): 1-63 chars, alphanumeric, hyphens
# allowed *interior* only — a label may not start or end with one. The anchored
# form below enforces exactly that: first char alnum, last char alnum.
_LABEL_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$")
# Top-level domain — letters only, 2+ (no all-numeric / single-char TLDs).
_TLD_RE = re.compile(r"^[A-Za-z]{2,}$")

# Consumer mailbox providers — legitimate for plenty of small businesses, but
# an address here cannot be tied to the business's own domain, so it is worth
# noting rather than trusting blind.
_CONSUMER_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "yahoo.com",
        "hotmail.com",
        "outlook.com",
        "aol.com",
        "icloud.com",
        "live.com",
        "msn.com",
        "ymail.com",
        "proton.me",
        "protonmail.com",
        "gmx.com",
        "mail.com",
    }
)

# A domain stem must be at least this long before a prefix match counts as a
# "resemblance" — guards against trivial 1-3 char coincidences.
_MIN_STEM = 4


@dataclass(frozen=True)
class EmailAssessment:
    """The verdict on one contact email and the reasoning behind it.

    ``verdict`` is one of:
      * ``valid``     — syntactically deliverable and either tied to the
                        business or with no context to doubt it.
      * ``malformed`` — structurally impossible; mail can never be delivered.
      * ``suspect``   — syntactically fine, but the domain matches nothing
                        about the business — treat as unverified / possible
                        misdirection until confirmed.
    """

    email: str
    verdict: str
    valid_syntax: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def is_trustworthy(self) -> bool:
        """True only for a clean ``valid`` verdict — convenience for callers."""
        return self.verdict == "valid"


# ---------------------------------------------------------------------------
# Syntactic validation — the hostname rules that catch a malformed address
# ---------------------------------------------------------------------------


def email_syntax_error(email: str) -> str | None:
    """Return a plain-language reason ``email`` is malformed, or None if valid.

    The domain is checked against RFC 1035 §2.3.1 hostname rules — the check
    that catches a misdirection address like ``info@juniper-.com`` (the label
    ``juniper-`` ends with a hyphen, which no hostname label may).
    """
    raw = (email or "").strip()
    if not raw:
        return "empty address"
    if raw.count("@") != 1:
        return "an address must contain exactly one '@'"

    local, _, domain = raw.partition("@")
    if not local:
        return "the local part (before '@') is empty"
    if len(local) > 64:
        return "the local part exceeds 64 characters"
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return "the local part has a leading, trailing, or doubled dot"
    if not _LOCAL_RE.match(local):
        return "the local part contains an illegal character"

    if not domain:
        return "the domain (after '@') is empty"
    if len(domain) > 253:
        return "the domain exceeds 253 characters"
    labels = domain.split(".")
    if len(labels) < 2:
        return f"the domain '{domain}' has no dot-separated top-level domain"
    for label in labels:
        if not label:
            return f"the domain '{domain}' has an empty label (doubled dot)"
        if len(label) > 63:
            return f"the domain label '{label}' exceeds 63 characters"
        if not _LABEL_RE.match(label):
            if label.startswith("-") or label.endswith("-"):
                return (
                    f"the domain label '{label}' starts or ends with a hyphen "
                    "— no hostname label may, so this address can never "
                    "receive mail"
                )
            return f"the domain label '{label}' contains an illegal character"
    if not _TLD_RE.match(labels[-1]):
        return f"the top-level domain '{labels[-1]}' is not valid (it must be two or more letters)"
    return None


def is_valid_email_syntax(email: str) -> bool:
    """True iff ``email`` is a structurally-deliverable address."""
    return email_syntax_error(email) is None


# ---------------------------------------------------------------------------
# Cold-send eligibility — never burn the sender domain on a role mailbox
# ---------------------------------------------------------------------------

# Role / system / department local-parts that must never be COLD-emailed.
# A footer scrape (``bugreport@vendorplatform-example.com`` lifted onto the Jordan Vega call
# card, 2026-06-22) or a department alias (``info@``, ``sales@``) is not a
# prospect's personal inbox — cold-mailing it draws spam complaints and burns
# the SendGrid sender-domain reputation. The address may still be stored on the
# record; this predicate only governs whether it is eligible as a COLD-send
# ``to``. Mirrors enrichment's ``_GENERIC_LOCAL_PARTS`` + ``_BLOCKED_LOCAL_PREFIXES``
# but is the single canonical gate the send-selection path consults.
_NON_COLD_SENDABLE_LOCAL_PARTS: frozenset[str] = frozenset(
    {
        "bugreport",
        "admin",
        "support",
        "noreply",
        "no-reply",
        "info",
        "postmaster",
        "abuse",
        "sales",
        "contact",
        "hello",
        "webmaster",
        "privacy",
        "legal",
        "security",
        "marketing",
        "help",
        "team",
        "office",
        "mailer-daemon",
    }
)


def is_cold_sendable_email(email: str) -> bool:
    """True iff ``email`` is safe to use as a COLD-outreach ``to`` address.

    Two gates, both fail-closed:

      1. It must be structurally deliverable (:func:`is_valid_email_syntax`) —
         empty / malformed addresses are never sendable.
      2. Its local part must not be a role / system / department mailbox
         (:data:`_NON_COLD_SENDABLE_LOCAL_PARTS`) — cold-mailing those burns
         the sender-domain reputation.

    This does NOT delete or rewrite the address on the record; it only tells the
    selection path whether the address may be cold-sent. Pure + zero-I/O.
    """
    raw = (email or "").strip()
    if not is_valid_email_syntax(raw):
        return False
    local = raw.partition("@")[0].lower()
    return local not in _NON_COLD_SENDABLE_LOCAL_PARTS


# ---------------------------------------------------------------------------
# Domain reasoning — does the address actually belong to this business?
# ---------------------------------------------------------------------------


def _host_from_url(url: str) -> str:
    """Reduce a URL to its bare registrable host (scheme/www/path stripped)."""
    u = (url or "").strip().lower()
    if not u:
        return ""
    u = re.sub(r"^[a-z][a-z0-9+.-]*://", "", u)  # strip scheme
    u = u.split("/", 1)[0].split("?", 1)[0]  # host only
    if u.startswith("www."):
        u = u[4:]
    return u.strip(".")


def _known_domains(website_url: str, extra_domains: tuple[str, ...]) -> set[str]:
    """The set of domains Samus already associates with the business."""
    out: set[str] = set()
    host = _host_from_url(website_url)
    if host:
        out.add(host)
    for d in extra_domains:
        d = (d or "").strip().lower().lstrip(".")
        if d.startswith("www."):
            d = d[4:]
        if d:
            out.add(d)
    return out


def _domain_stem(domain: str) -> str:
    """The alphanumeric stem of a domain's first label — ``juniper-`` -> ``juniper``."""
    first = (domain or "").lower().split(".", 1)[0]
    return re.sub(r"[^a-z0-9]", "", first)


def _name_stem(business_name: str) -> str:
    """The alphanumeric stem of a business name — for resemblance matching."""
    return re.sub(r"[^a-z0-9]", "", (business_name or "").lower())


def _resembles(domain: str, known: set[str], business_name: str) -> str:
    """If ``domain`` looks like a garbled form of something the business owns,
    return what it resembles (a domain or the quoted name); else ``""``.

    A prefix match either way — the offered stem is a prefix of a known stem,
    or vice versa — is the signature of a truncated / misheard / misdirected
    domain (``juniper-`` vs the real ``juniper-modern``).
    """
    stem = _domain_stem(domain)
    if len(stem) < _MIN_STEM:
        return ""
    for kd in sorted(known):
        ks = _domain_stem(kd)
        if ks and (ks.startswith(stem) or stem.startswith(ks)):
            return kd
    ns = _name_stem(business_name)
    if ns and (ns.startswith(stem) or stem.startswith(ns)):
        return f'the business name "{business_name.strip()}"'
    return ""


def assess_email(
    email: str,
    *,
    business_name: str = "",
    website_url: str = "",
    extra_domains: tuple[str, ...] = (),
) -> EmailAssessment:
    """Grade one contact email and explain the reasoning.

    ``business_name`` / ``website_url`` / ``extra_domains`` are whatever Samus
    already holds on the prospect (e.g. ``ProspectRecord.company_name`` /
    ``.website_url``, and the domain of any on-file ``owner_email``). With that
    context :func:`assess_email` can tell a real business address from a
    syntactically-fine but unrelated one — the soft form of misdirection. With
    no context it falls back to a syntax-only verdict.
    """
    raw = (email or "").strip()
    domain = raw.partition("@")[2].lower() if raw.count("@") == 1 else ""
    known = _known_domains(website_url, extra_domains)
    reasons: list[str] = []

    # 1. Malformed — structurally impossible. The definitive misdirection tell.
    syntax_err = email_syntax_error(raw)
    if syntax_err is not None:
        reasons.append(f"Malformed — {syntax_err}.")
        resembles = _resembles(domain, known, business_name)
        if resembles:
            reasons.append(
                f"Its domain resembles {resembles} — most likely a garbled or "
                "misdirected form of a real address, not a usable one."
            )
        return EmailAssessment(raw, "malformed", False, reasons)

    # 2. Syntactically valid — does the domain belong to this business?
    if domain in known:
        reasons.append(f"Domain matches the business's known domain ({domain}).")
        return EmailAssessment(raw, "valid", True, reasons)
    if domain in _CONSUMER_DOMAINS:
        reasons.append(
            f"Consumer mailbox ({domain}) — syntactically fine, but it cannot "
            "be tied to the business's own domain; confirm it is really theirs."
        )
        return EmailAssessment(raw, "valid", True, reasons)
    if not known and not business_name.strip():
        reasons.append(
            "Syntax is valid; no business context was supplied to cross-check the domain."
        )
        return EmailAssessment(raw, "valid", True, reasons)

    # Valid syntax, business context known, domain matches nothing of theirs.
    resembles = _resembles(domain, known, business_name)
    if resembles:
        reasons.append(
            f"Domain '{domain}' is not the business's known domain but "
            f"resembles {resembles} — a likely typo or misdirection; confirm "
            "before using it."
        )
    else:
        ref = ", ".join(sorted(known)) if known else "the business"
        reasons.append(
            f"Domain '{domain}' is unrelated to {ref} — unverified; treat as "
            "possible misdirection until confirmed."
        )
    return EmailAssessment(raw, "suspect", True, reasons)


# ---------------------------------------------------------------------------
# CLI — used by scripts/Log-Call.ps1 to check a contact on the spot
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Assess one contact email and print the verdict as a JSON line.

    Invoked inline by ``scripts/Log-Call.ps1`` when a gatekeeper offers a
    contact on a call, so the operator sees a malformed / misdirected address
    before acting on it. Exit code is 0 when the verdict is ``valid``, 1
    otherwise — so the caller can branch on ``$LASTEXITCODE`` if it wants.
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="contact_validation",
        description="Assess a contact email for validity + misdirection.",
    )
    parser.add_argument("--email", required=True)
    parser.add_argument("--company", default="", help="business name, for context")
    parser.add_argument("--website", default="", help="business website URL")
    parser.add_argument(
        "--known-domain",
        action="append",
        default=[],
        help="a domain already on file for the business (repeatable)",
    )
    args = parser.parse_args(argv)

    assessment = assess_email(
        args.email,
        business_name=args.company,
        website_url=args.website,
        extra_domains=tuple(args.known_domain),
    )
    print(
        json.dumps(
            {
                "email": assessment.email,
                "verdict": assessment.verdict,
                "valid_syntax": assessment.valid_syntax,
                "reasons": assessment.reasons,
            }
        )
    )
    return 0 if assessment.verdict == "valid" else 1


if __name__ == "__main__":
    raise SystemExit(main())
