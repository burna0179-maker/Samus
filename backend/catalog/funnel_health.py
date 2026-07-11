"""Checkout-funnel health — Samus's consciousness of whether marketing can convert.

The link audit (:mod:`backend.catalog.link_audit`) can DETECT a dead checkout,
but detection an operator has to remember to run is not consciousness. This
module makes funnel health a first-class signal the revenue engine reads on
every campaign tick and the morning brief surfaces every day:

  * A marketing campaign that pushes buy links into the world while the
    checkout behind them is archived converts ZERO and burns real CODB
    (SendGrid sends, LLM personalization tokens, Vapi minutes) for nothing.
    The 2026-07 incident — 4 archived payment links live in the funnel for
    weeks — is exactly this failure.
  * The snapshot is CACHED (JSON under the artifact root) so campaign ticks
    read a file, not Stripe. Refresh happens on a scheduled cadence (CLI) or
    lazily when the snapshot exceeds its TTL and a caller opts into refresh.

Consumption points:

  * ``backend.cash_engine.campaign_portfolio`` — checkout-pushing campaigns
    consult :func:`funnel_gate` in their eligibility check. WIRED-DORMANT:
    with ``SAMUS_FUNNEL_GATE_ENABLED`` unset/off the gate only LOGS (Samus is
    conscious, the operator sees it, nothing is blocked); set it on and a
    stale funnel makes those campaigns ineligible until the links are fixed.
  * ``backend.observability.production_health`` — the morning brief reads the
    snapshot read-only and alerts WARN/FAIL when the funnel is degraded, with
    the exact stale SKUs and the remediation command.

Fail-soft everywhere: no STRIPE_API_KEY → status "unknown" (never blocks, the
absence of signal is itself surfaced); audit crash → prior snapshot kept.

CLI::

    python -m backend.catalog.funnel_health              # refresh snapshot, print
    python -m backend.catalog.funnel_health --wp         # also scan WordPress
    python -m backend.catalog.funnel_health --read-only  # print cached, no network
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

_LOG = logging.getLogger("samus.catalog.funnel_health")

# Snapshot freshness contract. Payment links rot on the timescale of
# days-to-weeks (a manual rotation in the Stripe dashboard), so a 6-hour TTL
# keeps campaign ticks honest without hammering Stripe.
_DEFAULT_TTL_S = 6 * 3600.0

_SNAPSHOT_FILENAME = "funnel_health.json"

_STATUS_OK = "ok"
_STATUS_DEGRADED = "degraded"
_STATUS_UNKNOWN = "unknown"


def _gate_armed() -> bool:
    """WIRE vs ARM: the hard campaign gate is off unless the operator arms it."""
    return os.getenv("SAMUS_FUNNEL_GATE_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _snapshot_path() -> Path:
    from backend.common import storage

    d = storage.root() / "catalog"
    d.mkdir(parents=True, exist_ok=True)
    return d / _SNAPSHOT_FILENAME


@dataclass
class FunnelHealthSnapshot:
    """Cached result of one funnel audit pass.

    ``status``:
      * ``ok``       — every catalog payment link verified live on Stripe.
      * ``degraded`` — at least one stale catalog link, stale hardcoded URL,
                       or stale WordPress checkout link. Campaigns pushing
                       those SKUs convert zero.
      * ``unknown``  — audit could not verify (no STRIPE_API_KEY / fetch
                       failed). Not an alert by itself, but an absence of
                       signal the brief should mention.
    """

    status: str = _STATUS_UNKNOWN
    checked_ts: float = 0.0
    stale_skus: list[str] = field(default_factory=list)
    stale_sku_urls: dict[str, str] = field(default_factory=dict)
    hardcoded_stale: list[str] = field(default_factory=list)  # "file:line -> url"
    wp_stale: list[str] = field(default_factory=list)  # "page/slug -> url"
    wp_scanned: bool = False
    detail: str = ""

    @property
    def code_degraded(self) -> bool:
        """Degradation in the CATALOG or LIVE CODE — the surfaces an outbound
        email actually carries links from (send_lint enforces catalog URLs in
        every send). A WordPress-page placeholder does not make a sent email's
        link dead, so it is deliberately excluded here; the email channel gate
        keys on this, while the brief keys on the full status."""
        return bool(self.stale_skus or self.hardcoded_stale)

    def age_s(self, now_ts: Optional[float] = None) -> float:
        now = now_ts if now_ts is not None else time.time()
        return max(0.0, now - self.checked_ts)

    def is_fresh(self, *, ttl_s: float = _DEFAULT_TTL_S, now_ts: Optional[float] = None) -> bool:
        return self.checked_ts > 0 and self.age_s(now_ts) <= ttl_s

    def summary_line(self) -> str:
        if self.status == _STATUS_OK:
            extra = " (incl. WordPress)" if self.wp_scanned else ""
            return f"checkout funnel: all links live{extra}"
        if self.status == _STATUS_DEGRADED:
            parts = []
            if self.stale_skus:
                parts.append(
                    f"{len(self.stale_skus)} stale catalog SKU(s): " + ", ".join(self.stale_skus)
                )
            if self.hardcoded_stale:
                parts.append(f"{len(self.hardcoded_stale)} stale hardcoded URL(s)")
            if self.wp_stale:
                parts.append(f"{len(self.wp_stale)} stale WordPress link(s)")
            return "checkout funnel DEGRADED: " + "; ".join(parts)
        return f"checkout funnel: unverified ({self.detail or 'no signal'})"


_SNAPSHOT_FIELDS = frozenset(f.name for f in __import__("dataclasses").fields(FunnelHealthSnapshot))


def load_snapshot() -> FunnelHealthSnapshot:
    """Read the cached snapshot. Missing/corrupt file → empty 'unknown'."""
    path = _snapshot_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        known = {k: v for k, v in raw.items() if k in _SNAPSHOT_FIELDS}
        return FunnelHealthSnapshot(**known)
    except FileNotFoundError:
        return FunnelHealthSnapshot(detail="no snapshot yet")
    except Exception as exc:  # noqa: BLE001 — corrupt cache is not an outage
        _LOG.warning("funnel snapshot unreadable (%s) - treating as unknown", exc)
        return FunnelHealthSnapshot(detail=f"snapshot unreadable: {exc}")


def _save_snapshot(snap: FunnelHealthSnapshot) -> None:
    path = _snapshot_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(snap), indent=2), encoding="utf-8")
    tmp.replace(path)


def refresh_snapshot(
    *,
    include_wp: bool = False,
    api_key: Optional[str] = None,
    now_ts: Optional[float] = None,
) -> FunnelHealthSnapshot:
    """Run the audit and persist a fresh snapshot. Fail-soft.

    On a fetch failure the PRIOR snapshot is kept on disk (stale data beats
    losing the last known-good state) and an 'unknown' snapshot is returned
    to the caller without being persisted.
    """
    from backend.catalog import link_audit

    now = now_ts if now_ts is not None else time.time()
    key = api_key if api_key is not None else os.getenv("STRIPE_API_KEY", "")

    if not key:
        snap = FunnelHealthSnapshot(
            status=_STATUS_UNKNOWN,
            checked_ts=now,
            detail="no STRIPE_API_KEY - cannot verify links",
        )
        _save_snapshot(snap)
        return snap

    active, by_price = link_audit.fetch_active_links(key)
    if not active:
        # Fetch failed — do NOT overwrite the last known-good snapshot.
        return FunnelHealthSnapshot(
            status=_STATUS_UNKNOWN,
            checked_ts=now,
            detail="stripe fetch failed - prior snapshot retained",
        )

    stale = link_audit.audit_links(active_urls=active, by_price=by_price)
    hardcoded = [
        f"{h.file}:{h.line} -> {h.url}"
        for h in link_audit.audit_hardcoded_urls(active_urls=active)
        if h.stale
    ]
    wp_stale: list[str] = []
    if include_wp:
        wp_stale = [
            f"{w.post_type}/{w.slug} -> {w.url}"
            for w in link_audit.audit_wordpress_content(active_urls=active)
            if w.stale
        ]

    degraded = bool(stale or hardcoded or wp_stale)
    snap = FunnelHealthSnapshot(
        status=_STATUS_DEGRADED if degraded else _STATUS_OK,
        checked_ts=now,
        stale_skus=[s.sku_id for s in stale],
        stale_sku_urls={s.sku_id: s.current_link for s in stale},
        hardcoded_stale=hardcoded,
        wp_stale=wp_stale,
        wp_scanned=include_wp,
        detail="",
    )
    _save_snapshot(snap)
    if degraded:
        _LOG.warning("funnel health refresh: %s", snap.summary_line())
    else:
        _LOG.info("funnel health refresh: %s", snap.summary_line())
    return snap


def refresh_if_stale(
    *,
    include_wp: bool = True,
    ttl_s: float = _DEFAULT_TTL_S,
    now_ts: Optional[float] = None,
) -> FunnelHealthSnapshot:
    """Return the cached snapshot when fresh, otherwise refresh transparently.

    Intended for the morning brief and production-health path so the check
    never reports "stale" when it could just refresh. The gate path
    (:func:`funnel_gate`) stays read-only — only the observability surface
    triggers a lazy write.
    """
    snap = load_snapshot()
    if snap.is_fresh(ttl_s=ttl_s, now_ts=now_ts):
        return snap
    _LOG.info(
        "funnel snapshot stale (%.1fh) — triggering lazy refresh", snap.age_s(now_ts) / 3600.0
    )
    return refresh_snapshot(include_wp=include_wp, now_ts=now_ts)


@dataclass(frozen=True)
class FunnelGateOutcome:
    """Result of consulting the funnel before a checkout-pushing campaign."""

    allowed: bool
    armed: bool
    status: str
    reason: str


def funnel_gate(
    sku_ids: Optional[list[str]] = None,
    *,
    consider_wp: bool = True,
    ttl_s: float = _DEFAULT_TTL_S,
    now_ts: Optional[float] = None,
) -> FunnelGateOutcome:
    """Should a campaign that pushes checkout links be allowed to run?

    Reads the CACHED snapshot only — never touches the network, so it is safe
    on every campaign tick. Decision table:

      * snapshot ok / unknown / expired  → allowed (fail-open: absence of
        signal never silences production; the brief carries the "unverified"
        state instead).
      * degraded on a surface this campaign SHIPS + (sku_ids is None or
        intersects the stale set) → blocked IF the gate is armed
        (``SAMUS_FUNNEL_GATE_ENABLED``); otherwise allowed with a WARNING
        log — wired-dormant consciousness.

    ``consider_wp=False`` narrows "degraded" to the catalog + live-code
    surfaces (:pyattr:`FunnelHealthSnapshot.code_degraded`). An outbound email
    carries catalog links, never a WordPress page's markup, so the autonomous
    email channel must NOT be halted by a stale WordPress placeholder — that
    surface is the website's, handled by the brief + the WP remediation tool.
    The morning-brief surface keeps ``consider_wp=True`` for full visibility.
    """
    snap = load_snapshot()
    armed = _gate_armed()

    degraded = snap.code_degraded if not consider_wp else (snap.status == _STATUS_DEGRADED)

    if not degraded or not snap.is_fresh(ttl_s=ttl_s, now_ts=now_ts):
        return FunnelGateOutcome(
            allowed=True,
            armed=armed,
            status=snap.status,
            reason="funnel verified live"
            if snap.status == _STATUS_OK
            else (
                "degraded off-channel (WordPress) - email unaffected"
                if snap.status == _STATUS_DEGRADED and not degraded
                else "funnel unverified - fail-open"
            ),
        )

    # Degraded on a shipped surface + fresh. Does it touch the campaign's SKUs?
    hit_skus = snap.stale_skus
    if sku_ids is not None:
        hit_skus = sorted(set(sku_ids) & set(snap.stale_skus))
        wp_relevant = consider_wp and snap.wp_stale
        if not hit_skus and not wp_relevant and not snap.hardcoded_stale:
            return FunnelGateOutcome(
                allowed=True,
                armed=armed,
                status=snap.status,
                reason="degraded but campaign SKUs unaffected",
            )

    reason = snap.summary_line()
    if armed:
        _LOG.warning("funnel gate BLOCKED campaign (skus=%s): %s", hit_skus, reason)
        return FunnelGateOutcome(allowed=False, armed=True, status=snap.status, reason=reason)
    _LOG.warning(
        "funnel gate (dormant) would block campaign (skus=%s): %s - set "
        "SAMUS_FUNNEL_GATE_ENABLED=1 to enforce",
        hit_skus,
        reason,
    )
    return FunnelGateOutcome(allowed=True, armed=False, status=snap.status, reason=reason)


def main(argv: Optional[list[str]] = None) -> int:
    """Refresh (or print) the funnel-health snapshot. Exit 3 when degraded."""
    parser = argparse.ArgumentParser(
        prog="python -m backend.catalog.funnel_health",
        description="Refresh the checkout-funnel health snapshot the revenue "
        "engine and morning brief read. Exit 3 when degraded.",
    )
    parser.add_argument(
        "--wp", action="store_true", help="Also scan WordPress CMS content (network)."
    )
    parser.add_argument(
        "--read-only", action="store_true", help="Print the cached snapshot without refreshing."
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.read_only:
        snap = load_snapshot()
    else:
        snap = refresh_snapshot(include_wp=args.wp)

    print(snap.summary_line())
    if snap.checked_ts:
        print(
            f"  checked: {snap.age_s():.0f}s ago"
            f" (fresh={snap.is_fresh()}, wp_scanned={snap.wp_scanned})"
        )
    for sku, url in snap.stale_sku_urls.items():
        print(f"  stale sku: {sku} -> {url}")
    for h in snap.hardcoded_stale:
        print(f"  stale hardcoded: {h}")
    for w in snap.wp_stale:
        print(f"  stale wordpress: {w}")
    gate = funnel_gate()
    print(f"  gate: armed={gate.armed} allowed={gate.allowed} ({gate.reason})")
    return 3 if snap.status == _STATUS_DEGRADED else 0


if __name__ == "__main__":
    raise SystemExit(main())
