"""Apollo → call_list CSV pull, sized for ad-hoc daily-reach extension.

The existing :mod:`backend.outreach.run_campaign` does Apollo + send in one
shot, but it's gated on the G8 warmth / stake-sentence stack that zeroes out
vanilla cold pulls (by design — see
``project_samus_outreach_campaign``). For the trial-quota dash we want a
plain materialisation: hit Apollo People Search, keep only the contacts
that came back **sendable** (verified or guessed status, no locked email,
no extra credit spend on unlocks), write to a CSV in the same shape as
``call_list_<date>.csv`` so :mod:`.seo_audit_blast` reads it natively.

The blast then layers its own guards (junk-email regex, warm enrollment
exclusion, dedupe ledger, send-lint enforce) on top — the operator never
sees Apollo's pre-filter trash.

CLI::

    python -m backend.outreach.apollo_pull \\
        --titles "owner,founder,president" \\
        --industries "hvac,plumbing,roofing" \\
        --locations "Sacramento, California, US;Roseville, California, US" \\
        --pages 1 --per-page 25 \\
        --label sac_hvac_plumbing_roofing
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import re
import sys
from pathlib import Path

from backend.outreach.apollo_source import (
    ApolloContact,
    ApolloError,
    search_people,
)


_CSV_COLUMNS = (
    "prospect_id",
    "account_id",
    "company_name",
    "phone",
    "website_url",
    "website_status",
    "city",
    "state",
    "zipcode",
    "industry",
    "call_priority",
    "lead_score",
    "policy_family",
    "llm_cost_usd",
    "seo_score",
    "security_grade",
    "owner_name",
    "owner_email",
    "owner_title",
    "owner_linkedin_url",
    "contact_emails",
    "review_rating",
    "review_count",
    "business_description",
    "business_hours",
    "business_categories",
    "social_facebook",
    "social_instagram",
    "social_linkedin",
    "negative_review_snippets",
    "callsheet_issues",
    "callsheet_offer",
    "callsheet_pitch",
    "callsheet_opener",
    "callsheet_voicemail",
    "callsheet_objections",
)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")


def _website_from_domain(domain: str) -> str:
    domain = (domain or "").strip().lower()
    if not domain:
        return ""
    if domain.startswith("http://") or domain.startswith("https://"):
        return domain
    return f"https://{domain}"


def _to_csv_row(p: ApolloContact) -> dict[str, str]:
    """Map an ApolloContact onto the daily-call-list CSV shape.

    Fields the call list normally fills from the audit pipeline (seo_score,
    security_grade, callsheet_*) are left blank — the blast composer falls
    back to the cold-opener variant when those are empty."""
    pid = "apollo_" + (p.person_id or _slug(p.email or p.name))
    return {
        "prospect_id": pid,
        "account_id": "apollo_" + _slug(p.company_domain or p.company),
        "company_name": p.company or "",
        "phone": p.phone or "",
        "website_url": _website_from_domain(p.company_domain),
        "website_status": "",
        "city": p.city or "",
        "state": p.state or "",
        "zipcode": "",
        "industry": p.industry or "",
        "call_priority": "warm",
        "lead_score": "55",
        "policy_family": "apollo_cold",
        "llm_cost_usd": "0.0",
        "seo_score": "",
        "security_grade": "",
        "owner_name": p.name or p.first_name or "",
        "owner_email": p.email or "",
        "owner_title": p.title or "",
        "owner_linkedin_url": p.linkedin_url or "",
        "contact_emails": p.email or "",
        "review_rating": "",
        "review_count": "",
        "business_description": "",
        "business_hours": "",
        "business_categories": "",
        "social_facebook": "",
        "social_instagram": "",
        "social_linkedin": p.linkedin_url or "",
        "negative_review_snippets": "",
        "callsheet_issues": "",
        "callsheet_offer": "",
        "callsheet_pitch": "",
        "callsheet_opener": "",
        "callsheet_voicemail": "",
        "callsheet_objections": "",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apollo People Search → daily_calls CSV materialisation.",
    )
    parser.add_argument(
        "--titles",
        required=True,
        help='Comma-separated job titles (e.g. "owner,founder,president").',
    )
    parser.add_argument(
        "--industries",
        required=True,
        help="Comma-separated industry keywords "
        '(e.g. "hvac,plumbing,roofing,dentist,real estate").',
    )
    parser.add_argument(
        "--locations",
        required=True,
        help="Semicolon-separated person locations "
        '(e.g. "Sacramento, California, US;Roseville, California, US"). '
        "Use semicolons since locations contain commas.",
    )
    parser.add_argument(
        "--pages", type=int, default=1, help="Pages to pull (1 page = up to per_page contacts)."
    )
    parser.add_argument(
        "--per-page",
        type=int,
        default=25,
        help="Per-page size, 1-100. Apollo bills 1 unit per page.",
    )
    parser.add_argument(
        "--label", default="apollo_pull", help="Slug appended to the output CSV filename."
    )
    parser.add_argument("--out", default=None, help="Override the output CSV path.")
    args = parser.parse_args(argv)

    titles = [t.strip() for t in args.titles.split(",") if t.strip()]
    industries = [i.strip() for i in args.industries.split(",") if i.strip()]
    locations = [l.strip() for l in args.locations.split(";") if l.strip()]
    if not titles or not industries or not locations:
        sys.stderr.write("titles, industries, locations all required\n")
        return 1

    today = datetime.date.today().isoformat()
    out_path = (
        Path(args.out)
        if args.out
        else (
            Path(os.getenv("SAMUS_ARTIFACT_ROOT", "/opt/samus/data/host_artifacts"))
            / "daily_calls"
            / f"call_list_{today}__{_slug(args.label)}.csv"
        )
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Initialize the Codex registry — the G11 preflight inside search_people
    # fails closed if the registry isn't loaded, and host-venv invocations
    # don't have an app-lifespan to do it.
    try:
        from backend.common.codex.registry import REGISTRY  # noqa: PLC0415

        if not REGISTRY.is_loaded():
            REGISTRY.load()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"codex registry load warning: {exc}\n")

    pulled: list[ApolloContact] = []
    for page in range(1, max(1, args.pages) + 1):
        try:
            batch = search_people(
                titles=titles,
                industries=industries,
                locations=locations,
                per_page=args.per_page,
                page=page,
            )
        except ApolloError as exc:
            sys.stderr.write(f"apollo error on page {page}: {exc}\n")
            break
        if not batch:
            break
        pulled.extend(batch)

    # Keep only sendable contacts (verified/guessed status, real email).
    sendable = [p for p in pulled if p.sendable]

    # Dedupe on email — Apollo can return the same person twice when the
    # filter is broad.
    seen: set[str] = set()
    deduped: list[ApolloContact] = []
    for p in sendable:
        key = (p.email or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(p)

    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        for p in deduped:
            writer.writerow(_to_csv_row(p))

    summary = {
        "csv": str(out_path),
        "filters": {
            "titles": titles,
            "industries": industries,
            "locations": locations,
            "pages": args.pages,
            "per_page": args.per_page,
        },
        "pulled_total": len(pulled),
        "sendable": len(sendable),
        "after_dedupe": len(deduped),
    }
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
