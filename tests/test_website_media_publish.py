"""Wix Media upload methods + the media_publish bridge (no network — MockTransport)."""
from __future__ import annotations

import base64
import json as _json

import httpx
import pytest

from backend.website import media_publish
from backend.website.media_gen import MediaAsset
from backend.website.wix_client import WixClient, WixError

UPLOAD_URL = "https://upload.wixmp.test/u?token=abc"


def _body(request):
    return _json.loads(request.content.decode() or "{}")


def _routing_handler(record):
    """Routes the Wix Media + CMS calls and records them for assertions."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        record.append((request.method, path))
        if path.endswith("/files/generate-upload-url"):
            return httpx.Response(200, json={"uploadUrl": UPLOAD_URL})
        if path == "/u":  # the signed PUT target
            record.append(("PUT_BYTES", len(request.content)))
            return httpx.Response(200, json={"file": {
                "id": "img-1", "displayName": "hero_image.png",
                "url": "https://static.wixstatic.com/media/img-1.png",
                "mediaType": "image", "mimeType": "image/png",
                "wixUrl": "wix:image://v1/img-1/hero_image.png#originWidth=2048&originHeight=1152",
            }})
        if path.endswith("/files/import"):
            return httpx.Response(200, json={"file": {
                "id": "vid-1", "mediaType": "video", "mimeType": "video/mp4",
                "wixUrl": "wix:video://v1/vid-1/", "url": "https://video.wixstatic.com/vid-1.mp4",
            }})
        if path.endswith("/items/query"):
            return httpx.Response(200, json={"dataItems": [
                {"id": "row1", "data": {"_id": "row1", "ref": "main",
                                        "homeHeadline": "If you want a MIGHTY clean, call us!"}}]})
        if "/wix-data/v2/items/" in path:  # update
            record.append(("UPDATE_BODY", _body(request)))
            return httpx.Response(200, json={"dataItem": {"id": "row1"}})
        return httpx.Response(404, json={"message": "unrouted " + path})
    return handler


def _wix(record):
    return WixClient("k", account_id="a", transport=httpx.MockTransport(_routing_handler(record)))


def _img_asset():
    return MediaAsset(kind="hero_image", prompt="p", is_video=False, ok=True,
                      mime_type="image/png", data_b64=base64.b64encode(b"PNGBYTES").decode())


def _video_asset():
    return MediaAsset(kind="hero_video", prompt="p", is_video=True, ok=True,
                      mime_type="video/mp4", video_uri="https://veo.test/clip.mp4")


# ---------------------------------------------------------------------------
# WixClient media methods
# ---------------------------------------------------------------------------

def test_upload_bytes_generates_url_then_puts():
    rec = []
    desc = _wix(rec).upload_bytes(site_id="s1", data=b"PNGBYTES",
                                  filename="hero.png", mime_type="image/png")
    assert desc["wixUrl"].startswith("wix:image://")
    paths = [p for _, p in rec if isinstance(p, str)]
    assert any(p.endswith("/generate-upload-url") for p in paths)
    assert ("PUT_BYTES", len(b"PNGBYTES")) in rec


def test_upload_file_bytes_non_200_raises():
    def handler(request):
        return httpx.Response(500, text="boom")
    wix = WixClient("k", transport=httpx.MockTransport(handler))
    with pytest.raises(WixError):
        wix.upload_file_bytes(UPLOAD_URL, b"x", filename="f.png", mime_type="image/png")


def test_import_file_posts_url():
    rec = []
    desc = _wix(rec).import_file(site_id="s1", url="https://veo.test/clip.mp4",
                                 mime_type="video/mp4", display_name="hero")
    assert desc["mediaType"] == "video" and desc["wixUrl"].startswith("wix:video://")


# ---------------------------------------------------------------------------
# media_publish bridge
# ---------------------------------------------------------------------------

def test_publish_image_returns_ref():
    rec = []
    out = media_publish.publish_assets([_img_asset()], wix=_wix(rec), site_id="s1")
    assert out["refs"]["hero_image"]["wixUrl"].startswith("wix:image://")
    assert out["errors"] == []


def test_publish_video_imports_uri():
    rec = []
    out = media_publish.publish_assets([_video_asset()], wix=_wix(rec), site_id="s1")
    assert out["refs"]["hero_video"]["wixUrl"].startswith("wix:video://")


def test_publish_cms_merge_preserves_text():
    rec = []
    out = media_publish.publish_assets(
        [_img_asset()], wix=_wix(rec), site_id="s1",
        collection_id="SiteContent", update_cms=True,
    )
    assert out["cms_updated"] is True
    # the UPDATE body must keep the existing text AND add the media ref
    update = next(b for tag, b in rec if tag == "UPDATE_BODY")
    data = update["dataItem"]["data"]
    assert data["homeHeadline"].startswith("If you want a MIGHTY")   # preserved
    assert data["heroImage"].startswith("wix:image://")             # added


def test_publish_skips_failed_asset():
    bad = MediaAsset(kind="hero_image", prompt="p", is_video=False, ok=False, error="gen failed")
    out = media_publish.publish_assets([bad], wix=_wix([]), site_id="s1")
    assert out["refs"] == {}


def test_publish_upload_error_is_recorded_not_raised():
    def handler(request):
        if request.url.path.endswith("/generate-upload-url"):
            return httpx.Response(403, json={"message": "no media perm"})
        return httpx.Response(404)
    wix = WixClient("k", transport=httpx.MockTransport(handler))
    out = media_publish.publish_assets([_img_asset()], wix=wix, site_id="s1")
    assert out["refs"] == {} and any("hero_image" in e for e in out["errors"])
