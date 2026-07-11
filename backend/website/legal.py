"""Jurisdiction-aware legal pages — Privacy Policy + Terms & Conditions.

A client's site needs legal sub-pages, and the requirements vary by where the
client's company is registered (US federal baseline + state privacy law, EU/UK
GDPR, Canada PIPEDA, etc.). This module resolves the jurisdiction from the
client's registered location, maps it to applicable website-requirement laws,
and generates tailored Privacy Policy + Terms pages plus a compliance report
for the operator.

RESPONSIBILITY BOUNDARY (read this):
  * Generated documents are STANDARD, jurisdiction-aware TEMPLATES, NOT legal
    advice and NOT a substitute for a licensed attorney. The compliance report
    says so, and the real liability shield is contractual (a HustleForge MSA
    that places legal-compliance responsibility on the client + requires their
    review/sign-off before publish) — see ``compliance_report``.
  * The knowledge base below is curated + maintained, NOT live LLM "legal
    research" (which would risk hallucinated law). New/unknown jurisdictions
    resolve to a conservative superset default and are flagged for operator
    research (the deep-research skill expands the KB, operator-reviewed).

Pure-Python + deterministic. No network, no LLM. Returns escaped HTML content
blocks (the caller wraps them in page chrome) + a structured report.
"""

from __future__ import annotations

import html as _html
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# jurisdiction knowledge base (curated)
# --------------------------------------------------------------------------

# US federal baseline that applies to essentially every US business site.
_US_FEDERAL = {
    "laws": [
        "ADA (Title III) / WCAG 2.1 AA accessibility",
        "CAN-SPAM Act (email)",
        "COPPA (if collecting data from children under 13)",
        "FTC Act (truth in advertising)",
    ],
    "obligations": [
        "Accessibility: meet WCAG 2.1 AA (alt text, contrast, keyboard nav).",
        "Email: include physical postal address + one-click unsubscribe in marketing email (CAN-SPAM).",
        "No data collection from under-13s without verifiable parental consent (COPPA).",
        "Truthful advertising + clear disclosure of material connections (FTC).",
    ],
    "cookie_consent": False,  # US: notice required; opt-in banner not federally mandated
}

# State privacy laws (consumer privacy acts) — opt-out + disclosure regimes.
_US_STATES: dict[str, dict[str, Any]] = {
    "CA": {
        "law": "CCPA/CPRA",
        "rights": [
            "know",
            "delete",
            "correct",
            "opt-out of sale/sharing",
            "limit use of sensitive PI",
            "non-discrimination",
        ],
        "extra": [
            "'Do Not Sell or Share My Personal Information' link if you sell/share PI.",
            "Notice at collection.",
        ],
    },
    "VA": {
        "law": "VCDPA",
        "rights": [
            "access",
            "delete",
            "correct",
            "data portability",
            "opt-out of targeted ads/sale/profiling",
        ],
        "extra": [],
    },
    "CO": {
        "law": "Colorado Privacy Act (CPA)",
        "rights": [
            "access",
            "delete",
            "correct",
            "portability",
            "opt-out (incl. universal opt-out signals)",
        ],
        "extra": ["Honor browser universal opt-out (e.g. Global Privacy Control)."],
    },
    "CT": {
        "law": "Connecticut Data Privacy Act (CTDPA)",
        "rights": ["access", "delete", "correct", "portability", "opt-out"],
        "extra": [],
    },
    "UT": {
        "law": "Utah Consumer Privacy Act (UCPA)",
        "rights": ["access", "delete", "opt-out of sale/targeted ads"],
        "extra": [],
    },
    "TX": {
        "law": "Texas Data Privacy and Security Act (TDPSA)",
        "rights": ["access", "delete", "correct", "portability", "opt-out"],
        "extra": [],
    },
    "OR": {
        "law": "Oregon Consumer Privacy Act (OCPA)",
        "rights": [
            "access",
            "delete",
            "correct",
            "data portability",
            "opt-out of sale/targeted ads/profiling",
        ],
        "extra": ["Honor universal opt-out signals (e.g. Global Privacy Control)."],
    },
    "MT": {
        "law": "Montana Consumer Data Privacy Act (MCDPA)",
        "rights": ["access", "delete", "correct", "portability", "opt-out"],
        "extra": ["Honor universal opt-out signals."],
    },
}

# Non-US jurisdictions.
_COUNTRIES: dict[str, dict[str, Any]] = {
    "EU": {
        "laws": ["GDPR", "ePrivacy Directive (cookies)"],
        "cookie_consent": True,
        "rights": [
            "access",
            "rectification",
            "erasure",
            "restrict processing",
            "portability",
            "object",
            "withdraw consent",
        ],
        "obligations": [
            "Lawful basis for processing.",
            "Cookie consent banner (prior opt-in).",
            "DPO/EU representative if applicable.",
            "72-hour breach notification.",
        ],
    },
    "UK": {
        "laws": ["UK GDPR", "Data Protection Act 2018", "PECR (cookies)"],
        "cookie_consent": True,
        "rights": [
            "access",
            "rectification",
            "erasure",
            "restrict",
            "portability",
            "object",
            "withdraw consent",
        ],
        "obligations": [
            "Lawful basis.",
            "Cookie consent banner.",
            "ICO registration if applicable.",
        ],
    },
    "CA-COUNTRY": {
        "laws": ["PIPEDA"],
        "cookie_consent": False,
        "rights": ["access", "correction", "withdraw consent"],
        "obligations": ["Meaningful consent for collection/use.", "Designate a privacy officer."],
    },
}

_EU_MEMBERS = {
    "AT",
    "BE",
    "BG",
    "HR",
    "CY",
    "CZ",
    "DK",
    "EE",
    "FI",
    "FR",
    "DE",
    "GR",
    "HU",
    "IE",
    "IT",
    "LV",
    "LT",
    "LU",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SK",
    "SI",
    "ES",
    "SE",
}


@dataclass
class Jurisdiction:
    country: str = "US"
    state: str = ""
    label: str = "United States"
    laws: list[str] = field(default_factory=list)
    rights: list[str] = field(default_factory=list)
    obligations: list[str] = field(default_factory=list)
    cookie_consent: bool = False
    governing_law: str = "the United States"
    flagged_unknown: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "country": self.country,
            "state": self.state,
            "label": self.label,
            "laws": list(self.laws),
            "rights": list(self.rights),
            "obligations": list(self.obligations),
            "cookie_consent": self.cookie_consent,
            "governing_law": self.governing_law,
            "flagged_unknown": self.flagged_unknown,
        }


def resolve_jurisdiction(country: str = "US", state: str = "") -> Jurisdiction:
    """Resolve a client's registered location to applicable website-law requirements."""
    country = (country or "US").strip().upper()
    state = (state or "").strip().upper()

    if country in ("US", "USA", "UNITED STATES"):
        j = Jurisdiction(
            country="US",
            state=state,
            label="United States",
            laws=list(_US_FEDERAL["laws"]),
            obligations=list(_US_FEDERAL["obligations"]),
            cookie_consent=_US_FEDERAL["cookie_consent"],
        )
        st = _US_STATES.get(state)
        if st:
            j.laws.append(st["law"])
            j.rights = list(st["rights"])
            j.obligations += st.get("extra", [])
            j.governing_law = f"the State of {state}, United States"
        else:
            # conservative default: treat as if a consumer-privacy regime may apply
            j.rights = ["access", "delete", "correct", "opt-out of sale/sharing"]
            j.governing_law = (
                f"the State of {state}, United States" if state else "the United States"
            )
            if state:
                j.flagged_unknown = True  # state not in KB -> operator should confirm
        return j

    if country in _EU_MEMBERS or country == "EU":
        c = _COUNTRIES["EU"]
        return Jurisdiction(
            country=country,
            label="European Union",
            laws=list(c["laws"]),
            rights=list(c["rights"]),
            obligations=list(c["obligations"]),
            cookie_consent=True,
            governing_law=f"{country} and the European Union",
        )
    if country in ("GB", "UK", "UNITED KINGDOM"):
        c = _COUNTRIES["UK"]
        return Jurisdiction(
            country="UK",
            label="United Kingdom",
            laws=list(c["laws"]),
            rights=list(c["rights"]),
            obligations=list(c["obligations"]),
            cookie_consent=True,
            governing_law="England and Wales",
        )
    if country in ("CA-COUNTRY", "CANADA", "CAN"):
        c = _COUNTRIES["CA-COUNTRY"]
        return Jurisdiction(
            country="CA",
            label="Canada",
            laws=list(c["laws"]),
            rights=list(c["rights"]),
            obligations=list(c["obligations"]),
            cookie_consent=False,
            governing_law="Canada",
        )

    # unknown country -> conservative GDPR-grade superset, flagged for research
    return Jurisdiction(
        country=country,
        label=country,
        laws=["GDPR-grade baseline (jurisdiction not in KB)"],
        rights=["access", "delete", "correct", "portability", "opt-out"],
        obligations=["Treat as a strict opt-in privacy regime pending operator research."],
        cookie_consent=True,
        governing_law=country,
        flagged_unknown=True,
    )


# --------------------------------------------------------------------------
# generators (escaped HTML content blocks)
# --------------------------------------------------------------------------


def _e(s: str) -> str:
    return _html.escape(" ".join((s or "").split()), quote=True)


def _section(heading: str, *paras: str) -> str:
    body = "".join(f'<p class="mt-3 text-slate-600 leading-relaxed">{p}</p>' for p in paras if p)
    return f'<h2 class="mt-10 text-2xl font-semibold tracking-tight text-slate-900">{_e(heading)}</h2>{body}'


def _li(items: list[str]) -> str:
    return (
        '<ul class="mt-3 list-disc pl-6 space-y-1 text-slate-600">'
        + "".join(f"<li>{_e(i)}</li>" for i in items)
        + "</ul>"
    )


def privacy_policy_html(brief: Any, j: Jurisdiction, *, effective: str = "") -> str:
    name = _e(brief.business_name)
    email = _e(getattr(brief, "contact_email", "") or "the contact form")
    eff = _e(effective or "the date of publication")
    rights = j.rights or ["access", "deletion", "correction"]
    cookie = (
        "This site uses cookies and requires your prior consent via a cookie banner before "
        "non-essential cookies are set."
        if j.cookie_consent
        else "This site uses cookies; by using the site you consent to essential cookies."
    )
    return (
        _section(
            "Privacy Policy",
            f"This Privacy Policy explains how {name} collects, uses, and protects your personal "
            f"information when you use this website. Effective {eff}.",
        )
        + _section(
            "Information We Collect",
            "Information you provide directly (such as your name, email, phone number, and message "
            "when you contact us or request a quote), and information collected automatically "
            "(such as IP address, device and browser type, and usage data via cookies and analytics).",
        )
        + _section(
            "How We Use Your Information",
            "To respond to your inquiries, provide and improve our services, send service and "
            "(with your consent) marketing communications, and comply with legal obligations.",
        )
        + _section("Cookies and Tracking", cookie)
        + _section(
            "How We Share Information",
            "We do not sell your personal information. We share it only with service providers who "
            "help us operate the site and our business, and where required by law.",
        )
        + _section("Your Rights", "Depending on your location, you may have the right to:")
        + _li(rights)
        + _section(
            "Data Retention and Security",
            "We retain personal information only as long as necessary for the purposes described "
            "and use reasonable administrative and technical safeguards to protect it.",
        )
        + _section(
            "Children's Privacy",
            "This site is not directed to children under 13, and we do not knowingly collect their "
            "personal information (COPPA).",
        )
        + _section(
            "Contact Us",
            f"To exercise your rights or ask about this policy, contact {name} at {email}.",
        )
        + (
            f'<p class="mt-8 text-sm text-slate-500">Applicable framework: {_e(", ".join(j.laws))}.</p>'
            if j.laws
            else ""
        )
    )


def terms_html(brief: Any, j: Jurisdiction, *, effective: str = "") -> str:
    name = _e(brief.business_name)
    eff = _e(effective or "the date of publication")
    gov = _e(j.governing_law)
    return (
        _section(
            "Terms & Conditions",
            f"These Terms govern your use of the {name} website and services. By using this site you "
            f"agree to these Terms. Effective {eff}.",
        )
        + _section(
            "Services",
            f"{name} provides the services described on this site. Quotes and availability are subject "
            "to confirmation and may change.",
        )
        + _section(
            "Use of the Site",
            "You agree to use the site lawfully and not to misuse, disrupt, or attempt unauthorized "
            "access to it.",
        )
        + _section(
            "Intellectual Property",
            f"All content on this site is the property of {name} or its licensors and may not be "
            "reproduced without permission.",
        )
        + _section(
            "Disclaimers",
            'The site and services are provided "as is" without warranties of any kind to the fullest '
            "extent permitted by law.",
        )
        + _section(
            "Limitation of Liability",
            f"To the maximum extent permitted by law, {name} is not liable for indirect, incidental, or "
            "consequential damages arising from your use of the site or services.",
        )
        + _section(
            "Governing Law",
            f"These Terms are governed by the laws of {gov}, without regard to conflict-of-law rules.",
        )
        + _section(
            "Changes",
            "We may update these Terms; continued use of the site after changes constitutes acceptance.",
        )
        + _section("Contact", f"Questions about these Terms can be directed to {name}.")
    )


def disclaimer_html(brief: Any, j: Jurisdiction, *, effective: str = "") -> str:
    name = _e(brief.business_name)
    email = _e(getattr(brief, "contact_email", "") or "us through this site")
    eff = _e(effective or "the date of publication")
    return (
        _section(
            "Disclaimer",
            f"The information on this website is provided by {name} for general informational "
            f"purposes only. By using this site you accept this Disclaimer in full. Effective {eff}.",
        )
        + _section(
            "No Guarantee of Results",
            f"{name} takes pride in its work and strives for a high standard on every job. However, "
            "cleaning outcomes depend on the condition, materials, and circumstances of each space, so "
            "no specific result is guaranteed. Nothing on this site should be read as a promise, "
            "warranty, or contractual commitment.",
        )
        + _section(
            "Quotes and Pricing",
            "Any prices, quotes, or estimates shown or discussed are approximate and for planning only. "
            "They are not a binding offer and are confirmed in writing before work begins. Actual pricing "
            "may vary with the size, condition, and scope of the job.",
        )
        + _section(
            "Images and Examples",
            "Photographs, illustrations, and examples on this site may be illustrative and may not "
            "depict actual jobs, staff, customers, or results. They are provided to convey the style and "
            "standard of service, not a guarantee of any particular outcome.",
        )
        + _section(
            "No Professional Advice",
            "Content on this site does not constitute professional, legal, or health advice. You should "
            "not rely on it as a substitute for advice from a qualified professional.",
        )
        + _section(
            "External Links",
            f"This site may link to third-party websites or tools (such as booking or application "
            f"forms). {name} does not control and is not responsible for the content, policies, or "
            "practices of those third parties.",
        )
        + _section(
            "Accuracy",
            f"{name} works to keep the information here current and correct but does not warrant that "
            'the site is complete, accurate, or error-free. The site is provided "as is."',
        )
        + _section(
            "Limitation of Liability",
            f"To the maximum extent permitted by law, {name} is not liable for any loss or damage "
            "arising from reliance on information on this site. This Disclaimer is in addition to, and "
            "does not limit, the Terms & Conditions.",
        )
        + _section(
            "Contact", f"Questions about this Disclaimer can be directed to {name} at {email}."
        )
    )


def compliance_report(brief: Any, j: Jurisdiction) -> dict[str, Any]:
    """Operator-facing compliance summary + the responsibility disclaimer."""
    obligations = list(j.obligations)
    if getattr(brief, "contact_email", ""):
        obligations.append("Marketing email must carry a postal address + unsubscribe (CAN-SPAM).")
    flags: list[str] = []
    if j.cookie_consent:
        flags.append("Cookie consent banner REQUIRED before non-essential cookies.")
    if j.flagged_unknown:
        flags.append(
            "Jurisdiction not fully in the KB — operator should research + confirm before publish."
        )
    return {
        "jurisdiction": j.to_dict(),
        "applicable_laws": j.laws,
        "obligations": obligations,
        "flags": flags,
        "disclaimer": (
            "These pages are standard jurisdiction-aware TEMPLATES, not legal advice. "
            "Have the client review and, where stakes warrant, obtain attorney sign-off before publishing. "
            "The contractual shield (HustleForge MSA placing compliance responsibility on the client + "
            "requiring their approval) is what limits HustleForge's liability."
        ),
    }


__all__ = [
    "Jurisdiction",
    "resolve_jurisdiction",
    "privacy_policy_html",
    "terms_html",
    "compliance_report",
]
