"""$149 SEO Audit blast campaign — one-shot, per-prospect personalized.

Reads ``call_list_<date>.csv``, filters to rows with valid emails, composes
a per-prospect SEO Audit pitch with the live Stripe checkout link, and
sends via :func:`backend.outreach.send_lint.send_promotional` so the
subject↔CTA misalignment that caused the Kelly Zimmerman 2026-06-30 send
cannot recur here — every send carries ``claimed_sku="seo_audit"`` and the
lint refuses ENFORCE-mode dispatch on misalignment.

**Dry-run by default.** Pass ``--live`` to actually send. Recommended
sequence: dry-run → eyeball the previews + recipient list → live with the
same cap.

Each live send:
  * Tagged with SendGrid ``custom_args`` (prospect_id, sku, campaign_id)
    so the heat / open-no-click watchers can attribute events back.
  * Registered with the open-no-click nudge watcher (cancels itself if
    the prospect clicks; fires one soft-nudge after dwell hours).
  * Appended to ``campaign_seo_audit_blast_<YYYY-MM-DD>.jsonl`` for
    audit + dedupe (re-running the campaign skips anyone already in the
    ledger so we never double-send).
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_LOG = logging.getLogger("samus.outreach.seo_audit_blast")

CAMPAIGN_ID = "seo_audit_blast"
SKU_ID = "seo_audit"
PRICE_LABEL = "$149"
SUBJECT_TEMPLATE = "{company} — quick SEO audit ({price})"
DEFAULT_FROM_NAME = "Alex Hartman"
DEFAULT_REPLY_TO = "ahartman@hustleforge.tech"
POSTAL_FOOTER = "HustleForge LLC · 2290 Cheim Boulevard, Marysville, CA 95901-3560"
UNSUB_LINE = (
    'Not interested? Just reply "unsubscribe" and you won\'t hear from me again.'
)

# Liberal but real email regex (RFC 5321 is overkill; this catches typos).
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


@dataclass
class Recipient:
    prospect_id: str
    company: str
    email: str
    industry: str
    city: str
    state: str
    website_url: str
    seo_score: int
    security_grade: str
    owner_first_name: str
    callsheet_issues: str  # "; "-joined string from the CSV
    callsheet_pitch: str   # personalized pitch line if present


# ---------------------------------------------------------------------------
# CSV → Recipient list
# ---------------------------------------------------------------------------

def _first_name(owner_name: str) -> str:
    if not owner_name:
        return ""
    parts = owner_name.strip().split()
    return parts[0] if parts else ""


def _normalize_email(raw: str) -> str:
    if not raw:
        return ""
    addr = raw.strip().strip("<>").lower()
    if not _EMAIL_RE.match(addr):
        return ""
    return addr


# Personal-email domains we'll skip when an owner_email reads like the
# operator's own inbox (the row is a self-row, not a prospect).
_SKIP_EMAILS = frozenset({"ahartman@hustleforge.tech"})

# Junk-email defenses. Hard-earned from the 2026-06-30 dry-run:
#   * placeholder domains the enrichment scraper sometimes captures
#   * vendor/error-pipeline addresses (Sentry, Wix, Webador) that the
#     scraper finds in page source but aren't real prospect contacts
#   * obvious filename-shaped strings (home-1@2x11.jpg) that pass the
#     loose email regex
# Quota every blast is paying for; better one false-skip than a wasted send.
_JUNK_EMAIL_DOMAIN_SUFFIXES: tuple[str, ...] = (
    "example.com", "domain.com", "test.com", "email.com", "yoursite.com",
    "sentry.wixpress.com", "sentry.io",
    "webador.com",
    ".cpanel.site",
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
)
_JUNK_LOCAL_PARTS: frozenset[str] = frozenset({
    "user", "test", "email", "name", "your_email", "youremail",
    "info@info", "noreply", "no-reply", "donotreply", "mailer-daemon",
})


def _is_junk_email(addr: str) -> bool:
    """True when an address looks scraped-garbage rather than a real
    deliverable inbox. Conservative — only obvious cases."""
    local, _, domain = addr.partition("@")
    if not domain:
        return True
    dl = domain.lower()
    for suffix in _JUNK_EMAIL_DOMAIN_SUFFIXES:
        if dl.endswith(suffix):
            return True
    if local.lower() in _JUNK_LOCAL_PARTS:
        return True
    # Hex-blob local-parts (32+ hex chars) — classic Sentry/Wix
    # error-reporting endpoint shape.
    if len(local) >= 32 and all(c in "0123456789abcdef" for c in local.lower()):
        return True
    return False


def _warm_enrolled_ids() -> set[str]:
    """Prospect IDs in an active warm buying_signal enrollment — they should
    not get cold blasts. Same single-source-of-truth used by the cold-list
    exclusion in prospecting/text_export."""
    try:
        from backend.outreach.buying_signal_route import active_warm_prospect_ids
        return active_warm_prospect_ids()
    except Exception:  # noqa: BLE001
        return set()


def _load_recipients(
    csv_path: Path, skip_already_sent: set[str],
    extra_skip_emails: set[str] | None = None,
) -> list[Recipient]:
    """Read the call list, return one Recipient per row with a deliverable
    email (deduped on email, excluding already-sent, self-rows, junk
    addresses, prospects in an active warm buying-signal enrollment, and
    any explicit operator-provided skip list)."""
    warm_ids = _warm_enrolled_ids()
    extra = {e.lower() for e in (extra_skip_emails or set()) if e}
    out: list[Recipient] = []
    seen_emails: set[str] = set()
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            email = _normalize_email(row.get("owner_email") or "")
            if not email:
                continue
            if email in _SKIP_EMAILS or email in seen_emails:
                continue
            if email in skip_already_sent or email in extra:
                continue
            if _is_junk_email(email):
                continue
            pid = (row.get("prospect_id") or "").strip()
            if pid and pid in warm_ids:
                # Already in an active warm sequence — cold blast would
                # crowd a hotter conversation.
                continue
            seen_emails.add(email)
            try:
                seo = int(float(row.get("seo_score") or 0))
            except (TypeError, ValueError):
                seo = 0
            out.append(Recipient(
                prospect_id=(row.get("prospect_id") or "").strip(),
                company=(row.get("company_name") or "your business").strip(),
                email=email,
                industry=(row.get("industry") or "local business").strip(),
                city=(row.get("city") or "").strip(),
                state=(row.get("state") or "").strip(),
                website_url=(row.get("website_url") or "").strip(),
                seo_score=seo,
                security_grade=(row.get("security_grade") or "").strip(),
                owner_first_name=_first_name(row.get("owner_name") or ""),
                callsheet_issues=(row.get("callsheet_issues") or "").strip(),
                callsheet_pitch=(row.get("callsheet_pitch") or "").strip(),
            ))
    return out


def _load_already_sent(ledger_path: Path) -> set[str]:
    """Return emails that have already received this campaign — re-runs
    must skip them so SendGrid quota and prospect inboxes don't get hit twice."""
    out: set[str] = set()
    if not ledger_path.exists():
        return out
    try:
        with ledger_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                addr = _normalize_email(str(rec.get("to") or ""))
                if addr:
                    out.add(addr)
    except OSError:
        pass
    return out


# ---------------------------------------------------------------------------
# Per-prospect message composer
# ---------------------------------------------------------------------------

def _first_two_issues(issues_joined: str) -> list[str]:
    """Pull up to two cleaned issue strings out of the "; "-joined cell."""
    if not issues_joined:
        return []
    pieces = [p.strip() for p in issues_joined.split(";") if p.strip()]
    return pieces[:2]


def _hello(name: str, company: str) -> str:
    if name:
        return f"Hi {name},"
    return f"Hi there at {company},"


def _compose(rec: Recipient, buy_url: str) -> tuple[str, str, str]:
    """Return (subject, html_body, text_body)."""
    subject = SUBJECT_TEMPLATE.format(company=rec.company[:48], price=PRICE_LABEL)
    salutation = _hello(rec.owner_first_name, rec.company)
    issues = _first_two_issues(rec.callsheet_issues)

    issues_html = ""
    issues_text = ""
    if issues:
        bullets = "".join(f"<li>{i}</li>" for i in issues)
        issues_html = (
            "<p>Two things from a first pass on your site:</p>"
            f"<ul style='margin:6px 0 14px 18px;padding:0'>{bullets}</ul>"
        )
        issues_text = "\nTwo things from a first pass on your site:\n" + "".join(
            f"  - {i}\n" for i in issues
        )

    # Opener varies by audit-context availability — Apollo-pulled prospects
    # have no callsheet_issues, so "ran your site through our checks" would
    # be an overclaim. Switch to a plain first-contact opener in that case.
    if issues:
        opener_text = (
            f"I was looking at how {rec.industry} sites in "
            f"{rec.city or 'your area'} show up in local search and ran "
            f"{rec.website_url or rec.company} through our checks."
        )
        opener_html = (
            f"<p>I was looking at how {rec.industry} sites in "
            f"{rec.city or 'your area'} show up in local search and ran "
            f"<strong>{rec.website_url or rec.company}</strong> through our checks.</p>"
        )
    else:
        opener_text = (
            f"I help {rec.industry} owners in "
            f"{rec.city or 'Northern California'} get more visible on Google "
            f"without juggling another tool. Came across "
            f"{rec.company} and thought the audit might be useful before you "
            f"spend another dollar on ads."
        )
        opener_html = (
            f"<p>I help {rec.industry} owners in "
            f"{rec.city or 'Northern California'} get more visible on Google "
            f"without juggling another tool. Came across "
            f"<strong>{rec.company}</strong> and thought the audit might be "
            f"useful before you spend another dollar on ads.</p>"
        )

    text_body = (
        f"{salutation}\n\n"
        f"{opener_text}"
        f"{issues_text}\n"
        f"Our SEO Audit names every issue in plain language, ranks them by "
        f"impact, and hands you a prioritized fix list you can act on. "
        f"{PRICE_LABEL}, one-time, no retainer:\n\n"
        f"{buy_url}\n\n"
        f"Best,\n— Alex\nHustleForge\n\n"
        f"—\n{POSTAL_FOOTER}\n{UNSUB_LINE}\n"
    )

    html_body = (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;'
        'font-size:15px;line-height:1.55;color:#1a1a1a;max-width:620px;margin:0 auto;'
        'padding:20px 16px;background:#ffffff">'
        f"<p>{salutation}</p>"
        f"{opener_html}"
        f"{issues_html}"
        f"<p>Our <strong>SEO Audit</strong> names every issue in plain language, "
        f"ranks them by impact, and hands you a prioritized fix list you can act "
        f"on. {PRICE_LABEL}, one-time, no retainer.</p>"
        '<table cellpadding="0" cellspacing="0" border="0" style="margin:18px auto"><tr>'
        '<td align="center" bgcolor="#2a6df4" style="border-radius:6px">'
        f'<a href="{buy_url}" style="display:inline-block;padding:14px 32px;'
        'font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;font-size:16px;'
        'font-weight:700;color:#ffffff;background:#2a6df4;border-radius:6px;'
        'text-decoration:none">'
        f'Get the SEO Audit &mdash; {PRICE_LABEL} &rarr;</a>'
        '</td></tr></table>'
        "<p>Best,<br>&mdash; Alex<br>HustleForge</p>"
        '<div style="margin:24px 0 0;padding-top:12px;border-top:1px solid #e2e2e2;'
        'font-size:12px;line-height:1.5;color:#8a8a8a;text-align:center">'
        f"{POSTAL_FOOTER}<br>{UNSUB_LINE}"
        "</div></div>"
    )
    return subject, html_body, text_body


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run(
    *,
    csv_path: Path,
    ledger_path: Path,
    max_send: int,
    live: bool,
    now_iso: str,
    extra_skip_emails: set[str] | None = None,
) -> dict:
    from backend.catalog.registry import sku as catalog_sku

    entry = catalog_sku(SKU_ID)
    buy_base = entry.payment_link_url or ""
    if not buy_base:
        raise RuntimeError(f"catalog SKU {SKU_ID!r} has no payment_link_url")

    already = _load_already_sent(ledger_path)
    recipients = _load_recipients(
        csv_path, skip_already_sent=already,
        extra_skip_emails=extra_skip_emails,
    )
    queue = recipients[:max_send]

    out: dict = {
        "campaign_id": CAMPAIGN_ID,
        "csv": str(csv_path),
        "ledger": str(ledger_path),
        "ts": now_iso,
        "live": live,
        "total_emailable_in_csv": len(recipients),
        "already_sent_skip_count": len(already),
        "queued": len(queue),
        "max_send": max_send,
        "results": [],
    }

    if not queue:
        return out

    # Lazy-import the send path so dry-run never needs SendGrid env.
    if live:
        from backend.outreach.send_lint import send_promotional, SendLintBlocked
        try:
            from backend.outreach.open_no_click_watch import register as watch_register
        except Exception:  # noqa: BLE001
            watch_register = None  # type: ignore
    else:
        send_promotional = None  # type: ignore
        watch_register = None    # type: ignore

    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    for rec in queue:
        # Per-prospect attribution on the buy URL.
        ref = rec.prospect_id or "anon"
        buy_url = f"{buy_base}?client_reference_id=op_{CAMPAIGN_ID}_{ref}"
        subject, html_body, text_body = _compose(rec, buy_url)

        if not live:
            out["results"].append({
                "to": rec.email,
                "company": rec.company,
                "prospect_id": rec.prospect_id,
                "subject": subject,
                "buy_url": buy_url,
                "would_send": True,
            })
            continue

        try:
            resp = send_promotional(  # type: ignore[misc]
                to=rec.email,
                subject=subject,
                body=text_body,
                html_body=html_body,
                reply_to=DEFAULT_REPLY_TO,
                from_name=DEFAULT_FROM_NAME,
                claimed_sku=SKU_ID,
                custom_args={
                    "prospect_id": rec.prospect_id,
                    "sku": SKU_ID,
                    "campaign_id": CAMPAIGN_ID,
                },
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("send failed to=%s: %s", rec.email, exc)
            out["results"].append({
                "to": rec.email, "company": rec.company,
                "prospect_id": rec.prospect_id,
                "sent": False, "reason": str(exc),
            })
            continue

        msg_id = str((resp or {}).get("message_id") or "")
        # Register the open-no-click watch so a 24h-no-click triggers a nudge.
        watch_res: dict = {"registered": False, "reason": "skipped"}
        if watch_register and rec.prospect_id:
            try:
                watch_res = watch_register(
                    prospect_id=rec.prospect_id,
                    email=rec.email,
                    sent_at_iso=now_iso,
                    subject=subject,
                    buy_url=buy_url,
                    message_id=msg_id,
                    company=rec.company,
                    campaign_id=CAMPAIGN_ID,
                )
            except Exception as exc:  # noqa: BLE001
                watch_res = {"registered": False, "reason": f"error:{exc}"}

        ledger_record = {
            "ts": now_iso,
            "to": rec.email,
            "company": rec.company,
            "prospect_id": rec.prospect_id,
            "subject": subject,
            "buy_url": buy_url,
            "sku": SKU_ID,
            "campaign_id": CAMPAIGN_ID,
            "response": resp,
            "watch": watch_res,
        }
        with ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(ledger_record) + "\n")

        out["results"].append({
            "to": rec.email, "company": rec.company,
            "prospect_id": rec.prospect_id, "subject": subject,
            "sent": True, "message_id": msg_id, "watch": watch_res,
        })
    return out


def _default_csv_path() -> Path:
    today = datetime.date.today().isoformat()
    root = os.getenv("SAMUS_ARTIFACT_ROOT", "/opt/samus/data/host_artifacts")
    return Path(root) / "daily_calls" / f"call_list_{today}.csv"


def _default_ledger_path() -> Path:
    today = datetime.date.today().isoformat()
    root = os.getenv("SAMUS_ARTIFACT_ROOT", "/opt/samus/data/host_artifacts")
    return Path(root) / "outreach" / f"campaign_seo_audit_blast_{today}.jsonl"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="$149 SEO Audit blast — dry-run by default."
    )
    parser.add_argument("--csv", default=None,
                        help="Path to call_list CSV (default: today's host artifact)")
    parser.add_argument("--ledger", default=None,
                        help="Path to campaign ledger jsonl (default: today's)")
    parser.add_argument("--max-send", type=int, default=80,
                        help="Cap on sends this run (default: 80; SendGrid trial = 100).")
    parser.add_argument("--live", action="store_true",
                        help="Actually send. Without this flag, dry-run only.")
    parser.add_argument("--preview", action="store_true",
                        help="In dry-run, also print the subject+body of the first 3 recipients.")
    parser.add_argument("--skip-emails", default="",
                        help="Comma-separated explicit-skip emails (operator override).")
    args = parser.parse_args(argv)
    extra_skip = {e.strip().lower() for e in (args.skip_emails or "").split(",") if e.strip()}

    csv_path = Path(args.csv) if args.csv else _default_csv_path()
    ledger_path = Path(args.ledger) if args.ledger else _default_ledger_path()
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if not csv_path.exists():
        sys.stderr.write(f"call list CSV not found: {csv_path}\n")
        return 1

    summary = run(
        csv_path=csv_path, ledger_path=ledger_path,
        max_send=args.max_send, live=args.live, now_iso=now_iso,
        extra_skip_emails=extra_skip,
    )

    if args.preview and not args.live:
        # Recompose the first 3 with full bodies so the operator can read them.
        from backend.catalog.registry import sku as catalog_sku
        buy_base = catalog_sku(SKU_ID).payment_link_url or ""
        already = _load_already_sent(ledger_path)
        for r in _load_recipients(csv_path, skip_already_sent=already,
                                  extra_skip_emails=extra_skip)[:3]:
            buy_url = f"{buy_base}?client_reference_id=op_{CAMPAIGN_ID}_{r.prospect_id or 'anon'}"
            subj, _html, text = _compose(r, buy_url)
            sys.stderr.write("\n" + "=" * 60 + "\n")
            sys.stderr.write(f"PREVIEW TO: {r.email}  ({r.company})\n")
            sys.stderr.write(f"SUBJECT: {subj}\n")
            sys.stderr.write("-" * 60 + "\n")
            sys.stderr.write(text + "\n")

    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
