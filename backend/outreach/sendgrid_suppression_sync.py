"""Pull SendGrid's authoritative suppression lists into the local emailed-
suppression file so the outreach batch never re-mails a bounced / blocked /
complained / invalid / unsubscribed address.

WHY (reputation protection). The SES account was permanently denied largely
because a sender who keeps hitting bad addresses and ignoring complaints craters
their reputation. SendGrid maintains its OWN suppression lists (bounces, blocks,
spam_reports, invalid_emails, unsubscribes) and will not deliver to them -- but
our campaign builder doesn't KNOW about them, so without this sync it keeps
composing to dead addresses, wasting sends and looking spammy. This closes the
feedback loop WITHOUT needing a public HTTPS ingress (the SendGrid Event Webhook
would need one; our ingress is flaky free-ngrok). It is a read-only pull against
SendGrid + an idempotent append into the file the batch already loads.

Reuses, does not reinvent:
  * ``backend.outreach.campaign.load_suppression`` -- the exact reader the
    batch uses (handles bare-email AND JSON-line entries), so entries written
    here are honored by ``campaign.build_messages`` with no send-path change.
  * ``httpx`` + ``settings.sendgrid_api_key`` / ``sendgrid_base_url`` -- same
    HTTP + config path as ``common.email_backends.sendgrid``.

Read-only against SendGrid: only ``GET /v3/suppression/*``. Never sends,
deletes a suppression, or mutates SendGrid state.

CLI::

    python -m backend.outreach.sendgrid_suppression_sync [--path P] [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from backend.common.config import get_settings
from backend.common.dates import iso_now

from . import campaign

_LOG = logging.getLogger("samus.outreach.sendgrid_suppression_sync")

# The five SendGrid suppression groups, mapped to the reason tag we record.
# Every endpoint returns a JSON array of objects carrying an ``email`` field.
_SUPPRESSION_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("suppression/bounces", "bounce"),
    ("suppression/blocks", "block"),
    ("suppression/spam_reports", "spam"),
    ("suppression/invalid_emails", "invalid"),
    ("suppression/unsubscribes", "unsubscribe"),
)

# SendGrid clamps a page to 500; loop offsets until a short page. Bound the loop
# so a pathological account can't spin forever.
_PAGE_LIMIT = 500
_MAX_PAGES = 40


def _default_path() -> str:
    """The emailed-suppression file the outreach batch loads.

    Mirrors morning_batch._ledger_paths(): <artifact_root>/outreach/emailed_emails.txt.
    """
    root = os.getenv("SAMUS_ARTIFACT_ROOT", "/opt/samus/data")
    return os.path.join(root, "outreach", "emailed_emails.txt")


def fetch_suppressed_emails(
    api_key: str,
    *,
    base_url: str = "https://api.sendgrid.com",
    http_client: httpx.Client | None = None,
    timeout: float = 20.0,
) -> dict[str, str]:
    """Return ``{email_lower: reason}`` across all SendGrid suppression groups.

    Read-only (GET). Fail-soft PER ENDPOINT: one group erroring (or the whole
    API being unreachable) never raises -- it logs and returns what it gathered.
    A partial result is safe: it only ever ADDS addresses to skip.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    out: dict[str, str] = {}
    own_client = http_client is None
    client = http_client or httpx.Client(timeout=timeout)
    try:
        for path, reason in _SUPPRESSION_ENDPOINTS:
            try:
                _collect_endpoint(client, base_url, headers, path, reason, out)
            except Exception as exc:  # noqa: BLE001 — one bad group must not abort
                _LOG.warning("suppression fetch failed for %s: %s", path, exc)
    finally:
        if own_client:
            client.close()
    return out


def _collect_endpoint(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    path: str,
    reason: str,
    out: dict[str, str],
) -> None:
    offset = 0
    for _ in range(_MAX_PAGES):
        url = f"{base_url.rstrip('/')}/v3/{path}?limit={_PAGE_LIMIT}&offset={offset}"
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list) or not rows:
            return
        for row in rows:
            email = ""
            if isinstance(row, dict):
                email = str(row.get("email") or "").strip().lower()
            if email and email not in out:
                out[email] = reason
        if len(rows) < _PAGE_LIMIT:
            return
        offset += _PAGE_LIMIT
    _LOG.warning("suppression %s hit page cap (%d) — list may be truncated",
                 path, _MAX_PAGES)


@dataclass
class SyncResult:
    fetched: int = 0
    added: int = 0
    already_present: int = 0
    path: str = ""
    emails: set[str] = field(default_factory=set)
    by_reason: dict[str, int] = field(default_factory=dict)


def sync(
    *,
    path: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    dry_run: bool = False,
    http_client: httpx.Client | None = None,
) -> SyncResult:
    """Fetch SendGrid suppressions and merge new ones into the batch's file.

    Returns a :class:`SyncResult` whose ``emails`` set the caller can also union
    into its in-memory suppression for THIS run (so a dry-run preview is accurate
    even though nothing is written). Idempotent: only addresses not already in
    the file are appended. Never raises on a SendGrid/API error — returns an
    empty result so a sync outage can't block the batch (SendGrid's own account-
    level suppression still protects the actual sends).
    """
    settings = get_settings()
    key = api_key if api_key is not None else settings.sendgrid_api_key
    resolved_base = (
        base_url if base_url is not None
        else (getattr(settings, "sendgrid_base_url", "") or "https://api.sendgrid.com")
    )
    target = path or _default_path()

    if not key:
        _LOG.warning("no SendGrid API key — suppression sync skipped")
        return SyncResult(path=target)

    supp = fetch_suppressed_emails(
        key, base_url=resolved_base, http_client=http_client,
    )
    emails = set(supp.keys())
    by_reason: dict[str, int] = {}
    for r in supp.values():
        by_reason[r] = by_reason.get(r, 0) + 1

    existing = campaign.load_suppression(target)
    new = sorted(emails - existing)

    result = SyncResult(
        fetched=len(emails),
        added=0 if dry_run else len(new),
        already_present=len(emails & existing),
        path=target,
        emails=emails,
        by_reason=by_reason,
    )

    if new and not dry_run:
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            ts = iso_now()
            with open(target, "a", encoding="utf-8") as fh:
                for email in new:
                    fh.write(
                        '{"email": "%s", "source": "sendgrid", "reason": "%s", "ts": "%s"}\n'
                        % (email, supp.get(email, "suppressed"), ts)
                    )
            _LOG.info("merged %d new SendGrid suppression(s) into %s", len(new), target)
        except OSError as exc:
            _LOG.warning("suppression file append failed for %s: %s", target, exc)
            result.added = 0

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="backend.outreach.sendgrid_suppression_sync",
        description="Sync SendGrid suppression lists into the outreach "
                    "suppression file (read-only pull; idempotent append).",
    )
    parser.add_argument("--path", help="Suppression file (default: the batch's).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + report; write nothing.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    r = sync(path=args.path, dry_run=args.dry_run)
    print("=== SendGrid suppression sync ===")
    print(f"file      : {r.path}")
    print(f"fetched   : {r.fetched}  ({', '.join(f'{k}={v}' for k, v in sorted(r.by_reason.items())) or 'none'})")
    print(f"already   : {r.already_present}")
    print(f"{'would add' if args.dry_run else 'added'} : {len(r.emails - campaign.load_suppression(r.path)) if args.dry_run else r.added}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
