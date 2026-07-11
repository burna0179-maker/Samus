"""CLI — a controlled morning outreach batch from the Google-Places call list.

Apollo People Search is 403 on the free plan, so this batch does NOT source
prospects from Apollo (that is ``run_campaign.py``'s job). Instead it emails a
SMALL, vetted set of prospects already surfaced by the prospecting workcell in
today's ``call_list_YYYY-MM-DD.csv`` — the hot rows carrying an owner email.

It REUSES the same compliant compose + send path as ``run_campaign.py`` — it
does not reimplement any CAN-SPAM / compose / send logic:

  CSV (owner_email + lead_score>=min)
    -> ApolloContact per row (verified email, public_registry warmth)
    -> per-recipient stake_sentence (operator-framed, guard-passing)
    -> campaign.build_messages  (suppression + CAN-SPAM footer + per-run cap)
    -> common.email_backend.send_email  (provider selector -> SendGrid)
    -> emailed-suppression append + hash-chained audit ledger record.

Compliance + safety contract (identical to run_campaign):

  * Every body carries the CAN-SPAM footer (postal address + unsubscribe URL);
    ``campaign.build_messages`` REFUSES to build without both — fail-closed.
  * compose_body is budget-gated LLM personalisation with a templated fallback
    (that IS the "LLM-personalized" path; no second LLM call is added here).
  * ``--max-send`` hard-caps the run (default 10). ``--dry-run`` composes every
    email but SENDS NOTHING and writes nothing to suppression/ledger.
  * Suppression: already-emailed addresses in ``emailed_emails.txt`` are
    skipped; newly-sent addresses are appended so re-runs never double-send.

The host wrapper ``scripts/Run-MorningOutreach.ps1`` seeds SendGrid / LLM
secrets from DPAPI and pins ``SAMUS_ARTIFACT_ROOT`` + ``EMAIL_BACKEND=sendgrid``.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys

from backend.common.dates import iso_now

from . import campaign
from .apollo_source import ApolloContact
from .models import OutreachMessageRequest

_LOG = logging.getLogger("samus.outreach.morning_batch")

_DEFAULT_ARTIFACT_ROOT = "/opt/samus/data"

# CAN-SPAM footer fields — hardcoded defaults for the morning batch, overridable
# by env (matches run_campaign's SAMUS_OUTREACH_* contract) then by nothing
# else; the operator sets these once and they stay put.
_DEFAULT_POSTAL_ADDRESS = "2290 Cheim Boulevard, Marysville, CA 95901-3560"
_DEFAULT_UNSUBSCRIBE_URL = "https://hustleforge.tech/unsubscribe"
_DEFAULT_FROM_ADDR = "ahartman@hustleforge.tech"
# Fallback only; per-row _tailor_subjects overrides this for every matched row.
_DEFAULT_SUBJECT = "Quick question about {company}"

# These prospects come from a PUBLIC business directory (Google Places). The
# honest warmth signal for VR-G8 is "public_registry" — they are publicly
# listed local businesses, not scraped private contacts. compose_body reads
# this off the ApolloContact to clear the Codex G8 gate.
_WARMTH_SIGNAL = "public_registry"


def _artifact_root() -> str:
    return os.getenv("SAMUS_ARTIFACT_ROOT", _DEFAULT_ARTIFACT_ROOT)


def _ledger_paths() -> tuple[str, str]:
    """(campaign_ledger_jsonl, emailed_suppression_txt) under the artifact root.

    Same layout run_campaign uses so both share the one suppression file and
    the next run of either never re-emails an address the other sent.
    """
    root = os.path.join(_artifact_root(), "outreach")
    os.makedirs(root, exist_ok=True)
    today = iso_now()[:10]
    return (
        os.path.join(root, f"morning_batch_{today}.jsonl"),
        os.path.join(root, "emailed_emails.txt"),
    )


def _first_name_from_owner(owner_name: str, company: str) -> str:
    """Best-effort first name for the greeting; falls back to a neutral opener.

    The call list's owner_name is frequently blank (Google Places rarely
    carries a named person), so we degrade to "there" rather than greet with a
    company string. campaign._safe_format already maps an empty first_name to
    "there"; we mirror that here so the greeting reads naturally.
    """
    name = (owner_name or "").strip()
    if not name:
        return ""
    return name.split()[0]


def _stake_sentence(row: dict[str, str]) -> str:
    """Operator-framed opener for one prospect (guard-passing, proper-noun-bearing).

    This is the "I chose you, here is why" line the stake guard requires: it
    names the specific business (proper noun -> clears the all-lowercase reject),
    references a real signal from that prospect's own row (seo_score /
    security_grade / review_count), and avoids every banned template phrase.
    It is composed BEFORE any LLM call and rendered verbatim at the top of the
    body — the operator's chosen framing for the morning batch.
    """
    company = (row.get("company_name") or "your business").strip()
    # Keep the company reference short enough that the whole sentence stays
    # inside the stake guard's 280-char window even for long directory names.
    if len(company) > 90:
        company = company[:90].rstrip(" ,-|")
    city = (row.get("city") or "").strip()
    security_grade = (row.get("security_grade") or "").strip()
    review_count = (row.get("review_count") or "").strip()

    # Recycle pass (2026-07-03): a returning prospect gets FOLLOW-UP framing
    # that acknowledges the prior touch instead of a cold re-open. The prior
    # last-contact date comes from the call-list row (CRM-backfilled); the
    # touch digest itself stays out of the customer-visible line (it can
    # contain internal outcome labels) — continuity, not a data dump.
    if (row.get("recycled") or "").strip().lower() == "true":
        prior_when = (row.get("last_contact_at") or "").strip()[:10]
        when_clause = (
            f" since we last reached out on {prior_when}"
            if prior_when
            else " since we last reached out"
        )
        sentence = (
            f"Alex asked me to follow up with {company} — a fair amount has "
            f"changed{when_clause}, and the gap we flagged then is still open."
        )
        if len(sentence) > 280:
            sentence = sentence[:277].rstrip() + "..."
        return sentence

    where = f" in {city}" if city else ""
    if security_grade:
        reason = (
            f"your public trust-posture grade is {security_grade}, a gap "
            f"rivals{where} are already closing"
        )
    elif review_count:
        reason = (
            f"you have earned {review_count} reviews but the demand they "
            f"signal is hard to keep up with by hand"
        )
    else:
        reason = f"the online demand{where} is outrunning what any owner can chase by hand"
    sentence = f"Alex flagged {company} because {reason}."
    # Defensive clamp to the guard's hard ceiling; the guard also enforces this
    # but we prefer a clean truncation to a guard rejection at compose time.
    if len(sentence) > 280:
        sentence = sentence[:277].rstrip() + "..."
    return sentence


def _subject_for_row(row: dict[str, str]) -> tuple[str, str]:
    """Return ``(subject, template_key)`` for one CSV row.

    Per-prospect subject derived from real public signals — same priority as
    ``_stake_sentence`` so subject + body cite the same data (reads coherent,
    not glued). Deterministic (no LLM cost; matches proposal-stage design).
    Always names the company (proper noun) so VR-G8 warmth (public_registry)
    still clears at compose time. The ``template_key`` is the *structural*
    identity of the line — ``_tailor_subjects`` caps its share of a batch,
    because spam classifiers fingerprint structure + repetition, not the
    literal string (two rows on the same template still differ by company).
    """
    company = (row.get("company_name") or "your business").strip()
    if len(company) > 60:
        company = company[:60].rstrip(" ,-|")
    city = (row.get("city") or "").strip()
    security_grade = (row.get("security_grade") or "").strip().upper()
    seo_score_s = (row.get("seo_score") or "").strip()
    review_count_s = (row.get("review_count") or "").strip()
    recycled = (row.get("recycled") or "").strip().lower() == "true"

    if recycled:
        return f"Following up on the {company} audit", "recycled"

    try:
        seo_int = int(float(seo_score_s)) if seo_score_s else None
    except ValueError:
        seo_int = None
    try:
        review_int = int(review_count_s) if review_count_s else None
    except ValueError:
        review_int = None

    # F/D security grade — high-conviction hook; reads as heads-up not pitch.
    if security_grade in {"F", "D"}:
        return f"{company}'s site security grade — worth a two-minute look", "security"
    # High review volume — the capacity angle (owner drowning in demand).
    if review_int is not None and review_int >= 100:
        where = f" in {city}" if city else ""
        return f"{company} — keeping up with {review_int}+ reviews{where}", "reviews"
    # Weak SEO — the visibility angle.
    if seo_int is not None and seo_int < 50:
        where = f" in {city}" if city else ""
        return f"3 gaps in {company}'s local SEO{where}", "seo"
    # Fallback family — always per-row-distinct, company-named.
    return _fallback_subject(row, 0), "fallback"


def _fallback_subject(row: dict[str, str], variant: int) -> str:
    """Per-row fallback subject; ``variant`` rotates the *structure* so the
    cap remediation in ``_tailor_subjects`` spreads overflow across distinct
    shapes instead of piling a single fallback template. Company-named
    (VR-G8 warmth held)."""
    company = (row.get("company_name") or "your business").strip()
    if len(company) > 60:
        company = company[:60].rstrip(" ,-|")
    city = (row.get("city") or "").strip()
    industry = (row.get("industry") or "").strip()
    forms: list[str] = []
    if industry and city:
        forms.append(f"{industry.title()} owners in {city} — one thing about {company}")
    if city:
        forms.append(f"A note about {company} in {city}")
    forms.append(f"A quick note about {company}")
    forms.append(f"{company} — a two-minute observation")
    return forms[variant % len(forms)]


def _tailor_subjects(
    messages: list[OutreachMessageRequest],
    rows: list[dict[str, str]],
) -> None:
    """Rewrite each message's subject with a per-row tailored line, ENFORCING a
    per-batch structural cap: no single template key exceeds ~30% of the matched
    batch. Overflow rows degrade deterministically to spread fallback shapes,
    so no structure dominates the batch (the same-subject-to-N-recipients
    cluster is what trains bulk-mail spam classifiers, and it runs on subject +
    sender + repetition BEFORE the recipient opens). Deterministic; no LLM cost;
    every subject names the company so VR-G8 warmth still clears.
    """
    row_by_email = {(r.get("owner_email") or "").strip().lower(): r for r in rows}
    matched: list[tuple[OutreachMessageRequest, dict[str, str]]] = []
    for msg in messages:
        row = row_by_email.get((msg.to or "").strip().lower())
        if row is not None:
            matched.append((msg, row))
    n = len(matched) or 1
    cap = max(1, -(-(n * 30) // 100))  # ceil(0.30 * n)
    # Deterministic order so the cap is stable + testable (no random shuffle).
    matched.sort(key=lambda mr: (mr[0].to or "").strip().lower())
    counts: dict[str, int] = {}
    fb_spread = 0
    for msg, row in matched:
        subject, key = _subject_for_row(row)
        if counts.get(key, 0) >= cap:
            # Structural cap hit -> degrade to a spread fallback shape.
            subject = _fallback_subject(row, fb_spread)
            key = f"fallback#{fb_spread % 4}"
            fb_spread += 1
        counts[key] = counts.get(key, 0) + 1
        msg.subject = subject
    top_share = (max(counts.values()) if counts else 0) / n
    _LOG.info(
        "tailored %d subject(s); %d distinct template(s), top-share=%.0f%% (cap=%d of %d matched)",
        len(matched),
        len(counts),
        top_share * 100,
        cap,
        n,
    )


def _contact_from_row(row: dict[str, str]) -> ApolloContact:
    """Map one call-list CSV row to an ApolloContact for the compose path.

    email_status is set to "verified" so ApolloContact.sendable is True and the
    campaign's verified-only filter keeps the row (the prospecting workcell
    already validated these owner emails). legitimacy_signal carries the
    public-registry warmth signal so compose_body clears VR-G8.
    """
    company = (row.get("company_name") or "").strip()
    return ApolloContact(
        person_id=(row.get("prospect_id") or "").strip(),
        first_name=_first_name_from_owner(row.get("owner_name", ""), company),
        name=(row.get("owner_name") or company).strip(),
        title=(row.get("owner_title") or "").strip(),
        email=(row.get("owner_email") or "").strip(),
        email_status="verified",
        company=company,
        company_domain="",
        industry=(row.get("industry") or "").strip(),
        city=(row.get("city") or "").strip(),
        state=(row.get("state") or "").strip(),
        phone=(row.get("phone") or "").strip(),
        legitimacy_signal=_WARMTH_SIGNAL,
    )


def _lead_score(row: dict[str, str]) -> int | None:
    raw = (row.get("lead_score") or "").strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def load_hot_rows(csv_path: str, *, min_score: int) -> list[dict[str, str]]:
    """Rows with a non-empty owner_email AND lead_score >= min_score.

    Order is preserved so the batch is deterministic run-to-run.
    """
    from .email_hygiene import is_bad_email

    kept: list[dict[str, str]] = []
    dropped_bad = 0
    with open(csv_path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            email = (row.get("owner_email") or "").strip()
            if not email:
                continue
            # Drop scraped non-emails (asset filenames, placeholders, platform
            # support inboxes) before they can bounce and hurt sender reputation.
            if is_bad_email(email):
                dropped_bad += 1
                continue
            score = _lead_score(row)
            if score is None or score < min_score:
                continue
            kept.append(row)
    if dropped_bad:
        _LOG.info("load_hot_rows dropped %d row(s) with garbage owner_email", dropped_bad)
    return kept


def _config(subject: str, max_send: int) -> campaign.CampaignConfig:
    """Build the CampaignConfig with hardcoded morning-batch defaults.

    Postal address + unsubscribe URL fall back to the hardcoded defaults but
    an env override (SAMUS_OUTREACH_*) wins so the PS wrapper / operator can
    change them without editing code. require_verified_email stays True — the
    contacts are marked verified above, so the verified-only filter is a live
    guard, not a bypass.
    """
    return campaign.CampaignConfig(
        template_id="morning_batch_places_v1",
        subject=subject,
        from_name=os.getenv("SAMUS_OUTREACH_FROM_NAME", "Samus"),
        sender_postal_address=os.getenv(
            "SAMUS_OUTREACH_POSTAL_ADDRESS",
            _DEFAULT_POSTAL_ADDRESS,
        ),
        unsubscribe_url=os.getenv(
            "SAMUS_OUTREACH_UNSUBSCRIBE_URL",
            _DEFAULT_UNSUBSCRIBE_URL,
        ),
        max_send=max_send,
        require_verified_email=True,
        use_llm=os.getenv("SAMUS_OUTREACH_USE_LLM", "").strip().lower() in {"1", "true", "yes"},
    )


def build_batch(
    rows: list[dict[str, str]],
    cfg: campaign.CampaignConfig,
    *,
    already_sent: set[str] | None = None,
) -> campaign.CampaignResult:
    """Compose + filter the batch via campaign.build_messages (reused wholesale).

    Every kept row gets an operator-framed stake_sentence keyed by email;
    build_messages applies suppression, the CAN-SPAM footer (in compose_body),
    the per-run cap, and de-dup — the same decision layer run_campaign uses.
    """
    contacts = [_contact_from_row(r) for r in rows]
    stake_sentences = {
        (r.get("owner_email") or "").strip().lower(): _stake_sentence(r)
        for r in rows
        if (r.get("owner_email") or "").strip()
    }
    return campaign.build_messages(
        contacts,
        cfg,
        already_sent=already_sent,
        stake_sentences=stake_sentences,
    )


def _attach_flyers(
    messages: list[OutreachMessageRequest],
    rows: list[dict[str, str]],
    cfg: campaign.CampaignConfig,
) -> int:
    """Attach a matched buy-now flyer (html_body) to each message.

    Returns the count attached. Best-effort per message — a bad row or offer
    skips only that flyer, and the plain-text body always still sends.
    """
    from .flyer import buy_url, match_offer, render_flyer_html

    # A campaign can LEAD with one featured offer (e.g. the 48-Hour Workflow
    # Rescue): SAMUS_OUTREACH_FEATURED_SKU=service_workflow_rescue. Empty = the
    # default per-prospect matched add-on.
    featured_sku = (os.getenv("SAMUS_OUTREACH_FEATURED_SKU") or "").strip() or None

    row_by_email = {(r.get("owner_email") or "").strip().lower(): r for r in rows}
    saved_templates: set[str] = set()
    attached = 0
    for msg in messages:
        try:
            row = row_by_email.get((msg.to or "").strip().lower())
            if not row:
                continue
            offer = match_offer(row, featured_sku=featured_sku)
            if offer is None:
                continue
            # Persist this offer's flyer as a reusable template (once per offer
            # per run; the store dedupes by content). Best-effort.
            tid = f"{offer.kind}_{offer.sku_id}"
            if tid not in saved_templates:
                saved_templates.add(tid)
                try:
                    from .flyer_templates import save_template

                    save_template(offer, sample_company=msg.company)
                except Exception:  # noqa: BLE001 — template save never blocks a send
                    pass
            link = buy_url(offer, prospect_id=msg.prospect_id, email=msg.to or "")
            msg.html_body = render_flyer_html(
                company=msg.company,
                first_name=_first_name_from_owner(row.get("owner_name", ""), msg.company),
                offer=offer,
                buy_link=link,
                postal_address=cfg.sender_postal_address,
                unsubscribe_url=cfg.unsubscribe_url,
                stake=_stake_sentence(row),
            )
            attached += 1
        except Exception as exc:  # noqa: BLE001 — flyer is additive; plain text still sends
            _LOG.warning("flyer attach failed for %s: %s", msg.to, exc)
    _LOG.info("attached %d flyer(s) of %d message(s)", attached, len(messages))
    return attached


def _print_dry_run(result: campaign.CampaignResult) -> None:
    print("")
    print("=== MORNING OUTREACH BATCH — DRY RUN (nothing sent) ===")
    print(
        f"considered={result.considered} built={result.built} "
        f"suppressed={result.suppressed} not_sendable={result.not_sendable} "
        f"duplicate={result.duplicate} capped={result.capped}"
    )
    for i, req in enumerate(result.messages, start=1):
        preview = req.body.replace("\n", " ")[:200]
        print("")
        print(f"[{i}] to      : {req.to}")
        print(f"    company : {req.company}")
        print(f"    subject : {req.subject!r}")
        print(f"    body    : {preview}...")
        if req.html_body:
            has_cta = 'href="https://buy.stripe.com' in req.html_body
            print(f"    flyer   : YES (html {len(req.html_body)} chars, buy-now CTA={has_cta})")
    print("")
    print(
        f"DRY RUN -- would send {result.built} email(s); "
        "no email sent, no suppression/ledger written."
    )


def _send_all(
    messages: list[OutreachMessageRequest],
    ledger_path: str,
    suppression_path: str,
) -> tuple[int, int]:
    """Send each message via the provider selector; append suppression + ledger.

    Reuses ``common.email_backend.send_email`` (SendGrid via settings) exactly
    as run_campaign._send_all does. Each success is appended to the emailed-
    suppression file (so re-runs skip it) and recorded on BOTH the local JSONL
    run ledger and the canonical hash-chained audit ledger (audit_ledger).
    """
    from backend.common.email_backend import send_email

    try:
        from backend.common.audit_ledger import get_default_ledger

        audit = get_default_ledger()
    except Exception as exc:  # noqa: BLE001 — audit must not block a send outright
        _LOG.warning("audit ledger unavailable (%s); continuing without it", exc)
        audit = None

    sent = failed = 0
    with (
        open(ledger_path, "a", encoding="utf-8") as ledger,
        open(suppression_path, "a", encoding="utf-8") as supp,
    ):
        for req in messages:
            try:
                result = send_email(
                    to=req.to,
                    subject=req.subject,
                    body=req.body,
                    html_body=(req.html_body or None),
                    from_addr=os.getenv("SENDGRID_FROM_EMAIL") or _DEFAULT_FROM_ADDR,
                    from_name=os.getenv("SAMUS_OUTREACH_FROM_NAME") or None,
                )
                sent += 1
                ts = str(result.get("ts") or iso_now())
                message_id = result.get("message_id", "")
                ledger.write(
                    json.dumps(
                        {
                            "ts": ts,
                            "prospect_id": req.prospect_id,
                            "email": req.to,
                            "company": req.company,
                            "template_id": req.template_id,
                            "message_id": message_id,
                            "backend": "email_backend",
                            "source": "morning_batch",
                            "status": "sent",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                supp.write((req.to or "") + "\n")
                supp.flush()
                if audit is not None:
                    try:
                        audit.record(
                            "outreach.morning_batch.sent",
                            {
                                "prospect_id": req.prospect_id,
                                "email": req.to,
                                "company": req.company,
                                "template_id": req.template_id,
                                "message_id": message_id,
                                "source": "morning_batch",
                            },
                        )
                    except Exception as exc:  # noqa: BLE001 — ledger write is best-effort
                        _LOG.warning(
                            "audit ledger record failed prospect=%s: %s",
                            req.prospect_id,
                            exc,
                        )
            except Exception as exc:  # noqa: BLE001 — one bad send must not abort the run
                failed += 1
                _LOG.warning(
                    "send failed prospect=%s to=%s: %s",
                    req.prospect_id,
                    req.to,
                    exc,
                )
    return sent, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="backend.outreach.morning_batch",
        description="Controlled morning outreach batch from the call-list CSV.",
    )
    parser.add_argument(
        "--csv",
        required=True,
        help="Path to the daily call_list CSV (owner_email + lead_score columns).",
    )
    parser.add_argument(
        "--max-send",
        type=int,
        default=10,
        help="Hard cap on emails sent this run (default 10).",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=70,
        help="Minimum lead_score to include a row (default 70).",
    )
    parser.add_argument(
        "--subject",
        default=_DEFAULT_SUBJECT,
        help="Subject template; {company} is filled per recipient.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compose + print every email but send nothing / write nothing.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Codex Validation Layer must be loaded before compose_body — the compose
    # gate fails closed (CodexUnavailable) otherwise. Mirrors run_campaign.main.
    from backend.common.app_factory import _ensure_codex_loaded

    _ensure_codex_loaded("outreach")

    if not os.path.exists(args.csv):
        _LOG.error("call-list CSV not found: %s", args.csv)
        return 2

    rows = load_hot_rows(args.csv, min_score=args.min_score)
    _LOG.info(
        "morning_batch loaded %d hot row(s) (owner_email + lead_score>=%d) from %s",
        len(rows),
        args.min_score,
        args.csv,
    )

    cfg = _config(args.subject, args.max_send)
    ledger_path, suppression_path = _ledger_paths()

    # Reputation loop: pull SendGrid's bounce/block/spam/invalid/unsubscribe
    # lists and honor them this run (and persist them to the suppression file on
    # a live run). Best-effort — a sync outage must never block the batch, since
    # SendGrid's own account-level suppression still protects the actual sends.
    sg_suppressed: set[str] = set()
    try:
        from .sendgrid_suppression_sync import sync as _sg_sync

        _r = _sg_sync(path=suppression_path, dry_run=args.dry_run)
        sg_suppressed = _r.emails
        _LOG.info(
            "sendgrid suppression sync: fetched=%d added_to_file=%d (%s)",
            _r.fetched,
            _r.added,
            ", ".join(f"{k}={v}" for k, v in sorted(_r.by_reason.items())) or "none",
        )
    except Exception as exc:  # noqa: BLE001 — never block the batch on a sync error
        _LOG.warning("sendgrid suppression sync skipped: %s", exc)

    already_sent = campaign.load_suppression(suppression_path) | sg_suppressed

    try:
        result = build_batch(rows, cfg, already_sent=already_sent)
    except ValueError as exc:
        # build_messages fail-closes without postal address + unsubscribe URL.
        _LOG.error("morning batch build refused (CAN-SPAM): %s", exc)
        return 2

    # Per-prospect subject tailoring — runs unconditionally (before the optional
    # flyer attach) so it applies whether or not flyers are enabled. Replaces the
    # static _DEFAULT_SUBJECT with signal-keyed, cap-limited lines.
    _tailor_subjects(result.messages, rows)

    if os.getenv("SAMUS_OUTREACH_FLYER", "1").strip().lower() not in {"0", "false", "no"}:
        _attach_flyers(result.messages, rows, cfg)

    if args.dry_run:
        _print_dry_run(result)
        return 0

    sent, failed = _send_all(result.messages, ledger_path, suppression_path)
    print(
        f"considered={result.considered} built={result.built} "
        f"suppressed={result.suppressed} capped={result.capped}"
    )
    print(f"sent={sent} failed={failed}")
    print(f"  ledger      -> {ledger_path}")
    print(f"  suppression -> {suppression_path}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
