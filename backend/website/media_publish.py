"""Publish generated media to the Wix Media Manager + (optionally) wire CMS refs.

Bridges ``media_gen`` (which produces bytes / a Veo URI) to the live site:
  * images -> ``WixClient.upload_bytes`` (generate signed URL -> PUT bytes)
  * video  -> ``WixClient.import_file`` (Veo returns a URI; import it)
and returns the durable Wix media refs (``wix:image://…`` / ``wix:video://…``).

Optionally (``update_cms``) it writes those refs into the ``SiteContent`` row so
the Velo binding can set element ``src`` by field — using a SAFE read-merge-write
that preserves the existing text fields (never a blind overwrite). Requires the
collection to carry the media fields (``heroImage`` etc.); if it doesn't, Wix
drops the unknown keys and the text is still preserved.

Fail-closed per asset: an upload/import failure is recorded and skipped, never
crashes the batch. Dormant by construction — only runs on assets you pass it.
"""
from __future__ import annotations

import base64
import logging
from typing import Any

from .media_gen import MediaAsset
from .wix_client import WixClient, WixError

_LOG = logging.getLogger("samus.website.media_publish")

# MediaAsset.kind -> the SiteContent field that should hold its wix media ref.
KIND_FIELD: dict[str, str] = {
    "hero_image": "heroImage",
    "section_image": "sectionImage",
    "og_image": "ogImage",
    "texture": "textureImage",
    "hero_video": "heroVideo",
}


def _ref_of(descriptor: dict[str, Any]) -> dict[str, str]:
    return {
        "wixUrl": str(descriptor.get("wixUrl", "") or ""),
        "url": str(descriptor.get("url", "") or ""),
        "id": str(descriptor.get("id", "") or ""),
    }


def _existing_row(wix: WixClient, site_id: str, collection_id: str) -> dict[str, Any] | None:
    """Return the first row's full data dict (incl _id), or None — best-effort."""
    try:
        resp = wix.query_data_items(site_id=site_id, collection_id=collection_id)
    except WixError:
        return None
    items = resp.get("dataItems") or resp.get("items") or []
    if not items:
        return None
    first = items[0]
    data = first.get("data") if isinstance(first.get("data"), dict) else first
    if isinstance(data, dict) and not data.get("_id"):
        # surface the item id so the update targets the right row
        data = {**data, "_id": first.get("id") or data.get("id") or ""}
    return data if isinstance(data, dict) else None


def publish_assets(
    assets: list[MediaAsset],
    *,
    wix: WixClient,
    site_id: str,
    collection_id: str = "",
    update_cms: bool = False,
    parent_folder_id: str = "",
) -> dict[str, Any]:
    """Upload media assets to Wix and return their refs (+ optional CMS wiring)."""
    refs: dict[str, dict[str, str]] = {}
    errors: list[str] = []

    for a in assets:
        if not a.ok:
            continue
        try:
            if a.is_video:
                if a.data_b64:  # downloaded Veo bytes -> upload (Veo URI needs auth, Wix can't fetch it)
                    desc = wix.upload_bytes(
                        site_id=site_id, data=base64.b64decode(a.data_b64),
                        filename=f"{a.kind}.mp4", mime_type=a.mime_type or "video/mp4",
                    )
                elif a.video_uri:  # a public video URL Wix can fetch
                    desc = wix.import_file(
                        site_id=site_id, url=a.video_uri,
                        mime_type=a.mime_type or "video/mp4", display_name=a.kind,
                    )
                else:
                    errors.append(f"{a.kind}:no_video")
                    continue
            else:
                if not a.data_b64:
                    errors.append(f"{a.kind}:no_bytes")
                    continue
                data = base64.b64decode(a.data_b64)
                desc = wix.upload_bytes(
                    site_id=site_id, data=data, filename=f"{a.kind}.png",
                    mime_type=a.mime_type or "image/png", parent_folder_id=parent_folder_id,
                )
            refs[a.kind] = _ref_of(desc)
        except WixError as exc:
            errors.append(f"{a.kind}:{exc}")

    cms_updated = False
    if update_cms and collection_id and refs:
        cms_updated = _merge_refs_into_cms(wix, site_id, collection_id, refs, errors)

    return {"refs": refs, "errors": errors, "cms_updated": cms_updated}


def _merge_refs_into_cms(
    wix: WixClient, site_id: str, collection_id: str,
    refs: dict[str, dict[str, str]], errors: list[str],
) -> bool:
    """Safe read-merge-write: add media refs onto the existing row, keep its text."""
    row = _existing_row(wix, site_id, collection_id)
    if not row or not row.get("_id"):
        errors.append("cms_update:no_existing_row")
        return False
    merged = dict(row)  # preserve every existing field (text content)
    for kind, field in KIND_FIELD.items():
        if kind in refs:
            merged[field] = refs[kind].get("wixUrl") or refs[kind].get("url")
    try:
        wix.update_data_item(
            site_id=site_id, collection_id=collection_id,
            item_id=str(row["_id"]), data=merged,
        )
        return True
    except WixError as exc:
        errors.append(f"cms_update:{exc}")
        return False


__all__ = ["publish_assets", "KIND_FIELD"]
