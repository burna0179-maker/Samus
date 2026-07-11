"""Flyer template library — persist every flyer Samus creates as a reusable,
merge-field template so proven designs COMPOUND into an asset library instead
of being regenerated ephemerally each send.

A rendered per-prospect flyer varies only by company / first name / buy-now URL
/ opening line; everything else (offer copy, layout, CTA, CAN-SPAM footer) is
fixed per offer. This stores ONE canonical template per offer (``<kind>_<sku>``),
rendered with ``{{merge_fields}}``, deduped by content and versioned in a
manifest so Samus can retrieve, reuse, and diff its own flyer designs.

Store (under ``storage.root()``):
  ``marketing/flyer_templates/<template_id>.html``   — the current template
  ``marketing/flyer_templates/manifest.jsonl``       — one row per version
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from backend.common import storage
from backend.common.dates import iso_now

_LOG = logging.getLogger("samus.outreach.flyer_templates")

# Merge-field placeholders the render leaves in the saved template.
_PH_COMPANY = "{{company}}"
_PH_FIRST_NAME = "{{first_name}}"
_PH_BUY_URL = "{{buy_now_url}}"
_PH_OPENING = "{{opening_line}}"

# CAN-SPAM constants are fixed across prospects, so they stay literal.
_POSTAL = "2290 Cheim Boulevard, Marysville, CA 95901-3560"
_UNSUB = "https://hustleforge.tech/unsubscribe"

_MANIFEST = "marketing/flyer_templates/manifest.jsonl"


def template_id(offer: object) -> str:
    """Stable id for an offer's flyer template, e.g. ``featured_service_workflow_rescue``."""
    kind = getattr(offer, "kind", "matched")
    sku = getattr(offer, "sku_id", "unknown")
    return f"{kind}_{sku}"


def _store_dir() -> Path:
    return storage.root() / "marketing" / "flyer_templates"


def render_template_html(offer: object) -> str:
    """Render the offer's flyer with merge-field placeholders (the template)."""
    from .flyer import render_flyer_html

    return render_flyer_html(
        company=_PH_COMPANY,
        first_name=_PH_FIRST_NAME,
        offer=offer,
        buy_link=_PH_BUY_URL,
        postal_address=_POSTAL,
        unsubscribe_url=_UNSUB,
        stake=_PH_OPENING,
    )


@dataclass
class SavedTemplate:
    template_id: str
    path: str
    content_sha: str
    changed: bool


def save_template(offer: object, *, sample_company: str = "") -> SavedTemplate | None:
    """Persist ``offer``'s flyer as a merge-field template. Idempotent: only
    rewrites (and appends a manifest version) when the content changed. Returns
    the SavedTemplate (``changed`` True on a new/updated version) or None on
    failure. Never raises — saving a template must never disturb a send.
    """
    try:
        html = render_template_html(offer)
        tid = template_id(offer)
        sha = hashlib.sha256(html.encode("utf-8")).hexdigest()[:16]
        d = _store_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{tid}.html"

        unchanged = path.exists() and path.read_text(encoding="utf-8") == html
        if unchanged:
            return SavedTemplate(tid, str(path), sha, changed=False)

        path.write_text(html, encoding="utf-8")
        _append_manifest(
            {
                "template_id": tid,
                "sku_id": getattr(offer, "sku_id", ""),
                "kind": getattr(offer, "kind", ""),
                "label": getattr(offer, "label", ""),
                "price_usd": getattr(offer, "price_usd", 0.0),
                "content_sha": sha,
                "saved_at": iso_now(),
                "sample_company": sample_company,
            }
        )
        _LOG.info("saved flyer template %s (sha=%s)", tid, sha)
        return SavedTemplate(tid, str(path), sha, changed=True)
    except Exception as exc:  # noqa: BLE001 — never disturb the caller
        _LOG.warning("save_template failed: %s", exc)
        return None


def _append_manifest(row: dict) -> None:
    try:
        p = storage.root() / _MANIFEST
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        _LOG.warning("flyer template manifest append failed: %s", exc)


def list_templates() -> list[str]:
    """Template ids currently in the library."""
    d = _store_dir()
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.html"))


def load_template(template_id_: str) -> str | None:
    """Return a template's HTML by id, or None if absent."""
    p = _store_dir() / f"{template_id_}.html"
    try:
        return p.read_text(encoding="utf-8") if p.exists() else None
    except OSError:
        return None
