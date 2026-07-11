"""signal_filter admission gate adapter for the prospecting pipeline.

Bridges an enriched :class:`~backend.prospecting.models.ProspectRecord` onto
the :mod:`backend.signal_filter` pre-qualification gate without re-running any
network I/O. The signal_filter workcell's own ``enrich`` does DNS / SSL /
homepage fetches, but ``process_discovery`` has *already* fetched each
prospect's site in Step 2a — so this adapter builds the enrichment-shaped
dict that :func:`backend.signal_filter.scoring.signals_from_enrichment`
consumes straight from the data already on the record.

The whole module is best-effort: :func:`apply_signal_filter_gate` swallows any
fault and returns the prospect list untouched, so a signal_filter regression
can never tank a discovery run.
"""
from __future__ import annotations

import logging
from typing import Any

from .models import ProspectRecord

_LOG = logging.getLogger("samus.prospecting.signal_gate")

# A website_status the crawler treats as a live, real site. Everything else
# (no_website, unreachable, parked, …) is a dead/junk site for gate purposes.
_LIVE_WEBSITE_STATUS = "live"


def _enrichment_from_record(p: ProspectRecord) -> dict[str, Any]:
    """Project an enriched ProspectRecord onto a signal_filter enrichment dict.

    Mirrors the flat shape produced by
    :func:`backend.signal_filter.enrichment.enrich` closely enough for
    :func:`backend.signal_filter.scoring.signals_from_enrichment`. Fields the
    prospecting pipeline does not collect (DNS/MX, SSL handshake, paid
    firmographics) are left at their neutral defaults — ``signals_from_enrichment``
    already degrades gracefully on missing sub-dicts.
    """
    has_site = bool((p.website_url or "").strip())
    reachable = p.website_status == _LIVE_WEBSITE_STATUS
    # A site with a non-empty, non-"no_website" status string was actually
    # crawled; treat anything that did not classify "live" as dead-or-junk.
    dead_or_junk = not reachable

    owner_signals = {
        "owner_email": p.owner_email or "",
        "contact_emails": p.contact_emails or "",
        "social_facebook": p.social_facebook or "",
        "social_instagram": p.social_instagram or "",
        "social_linkedin": p.social_linkedin or "",
    }
    return {
        "domain": p.website_url or "",
        "has_website": has_site,
        # DNS / SSL are not probed by the prospecting pipeline — a reachable
        # "live" site implies the domain resolves and (almost always) serves
        # HTTPS, so credit those two derived signals; otherwise stay neutral.
        "dns": {"resolves": reachable, "has_mx": False},
        "ssl": {"ssl_valid": reachable, "has_cert": reachable},
        "site": {
            "reachable": reachable,
            "dead_or_junk": dead_or_junk,
            "seo_score": int(p.seo_score or 0),
            "seo_issues": [],
            "owner_signals": owner_signals,
        },
        "firmographics": {"available": False},
        "review_rating": p.review_rating or "",
        "review_count": p.review_count or "",
        "phone": p.phone or "",
    }


def admit_prospect(p: ProspectRecord) -> bool:
    """Return ``True`` iff ``p`` clears the signal_filter admission threshold.

    Raises nothing for a normal record; any unexpected fault is the caller's
    to swallow (see :func:`apply_signal_filter_gate`).
    """
    from backend.signal_filter.queue_gate import should_enqueue
    from backend.signal_filter.scoring import signals_from_enrichment

    signal = signals_from_enrichment(_enrichment_from_record(p))
    return should_enqueue(signal)


def apply_signal_filter_gate(
    prospects: list[ProspectRecord],
) -> tuple[list[ProspectRecord], int]:
    """Drop prospects that fail the signal_filter pre-qualification gate.

    Returns ``(admitted, rejected_count)``. Best-effort by contract: a missing
    signal_filter module or any scoring fault is logged and the *input list is
    returned unchanged* (``rejected_count == 0``) — the gate must never tank a
    discovery run. Per-prospect scoring faults drop only that prospect's gate
    decision (the prospect is kept, fail-open) rather than the whole batch.
    """
    try:
        from backend.signal_filter.queue_gate import should_enqueue  # noqa: F401
    except Exception as exc:  # noqa: BLE001 — best-effort: signal_filter optional
        _LOG.warning("signal_filter unavailable; admission gate skipped: %s", exc)
        return prospects, 0

    admitted: list[ProspectRecord] = []
    rejected = 0
    for p in prospects:
        # No-website prospects bypass the gate entirely. The gate's weighted
        # axes score digital presence (domain health 0.20 + infrastructure
        # 0.20 + SEO + contactability + reviews), so a business with no site
        # zeroes two axes and caps at 0.60 < ADMISSION_THRESHOLD 0.62 — it
        # can NEVER pass. But this segment is the strongest web-design pitch
        # there is (see process_discovery Step 2a) and the operator explicitly
        # wants it on the morning call list; absence of a website is the
        # signal, not a disqualifier.
        if (p.website_status or "").strip().lower() == "no_website":
            admitted.append(p)
            continue
        try:
            keep = admit_prospect(p)
        except Exception as exc:  # noqa: BLE001 — per-prospect fail-open
            _LOG.warning(
                "signal_filter scoring failed prospect=%s; keeping (fail-open): %s",
                p.prospect_id, exc,
            )
            admitted.append(p)
            continue
        if keep:
            admitted.append(p)
        else:
            rejected += 1
            _LOG.info(
                "signal_filter rejected prospect=%s company=%s",
                p.prospect_id, p.company_name,
            )
    return admitted, rejected


__all__ = ["admit_prospect", "apply_signal_filter_gate"]
