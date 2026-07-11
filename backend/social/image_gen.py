"""Carousel slide image generator — text slides rendered as branded PNGs.

Renders carousel slide outlines (from ``repurpose.py``) into 1080x1080
Instagram-native images using Pillow. Each slide gets a branded background
color, title text, and body text. The resulting PNGs are written to the
writable state root and can be hosted for Instagram Graph API upload.

DORMANT by default — returns empty results when Pillow is absent or the
feature flag is off. Never raises from public API.
"""
from __future__ import annotations

import logging
import os
import re
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

_LOG = logging.getLogger("samus.social.image_gen")

# ---------------------------------------------------------------------------
# Lazy Pillow import — fail-closed when absent
# ---------------------------------------------------------------------------

_PIL_AVAILABLE: bool = False
try:
    from PIL import Image, ImageDraw, ImageFont  # type: ignore[import-untyped]

    _PIL_AVAILABLE = True
except ImportError:
    Image = None  # type: ignore[assignment,misc]
    ImageDraw = None  # type: ignore[assignment,misc]
    ImageFont = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Brand config (overridable via env)
# ---------------------------------------------------------------------------

_BG_COLOR: tuple[int, int, int] = tuple(
    int(v) for v in os.getenv("SAMUS_SLIDE_BG_COLOR", "15,23,42").split(",")
)[:3]  # type: ignore[assignment]
_TEXT_COLOR: tuple[int, int, int] = tuple(
    int(v) for v in os.getenv("SAMUS_SLIDE_TEXT_COLOR", "248,250,252").split(",")
)[:3]  # type: ignore[assignment]
_ACCENT_COLOR: tuple[int, int, int] = tuple(
    int(v) for v in os.getenv("SAMUS_SLIDE_ACCENT_COLOR", "251,146,60").split(",")
)[:3]  # type: ignore[assignment]

_SLIDE_WIDTH: int = int(os.getenv("SAMUS_SLIDE_WIDTH", "1080"))
_SLIDE_HEIGHT: int = int(os.getenv("SAMUS_SLIDE_HEIGHT", "1080"))
_FONT_SIZE_TITLE: int = int(os.getenv("SAMUS_SLIDE_FONT_TITLE", "64"))
_FONT_SIZE_BODY: int = int(os.getenv("SAMUS_SLIDE_FONT_BODY", "40"))
_PADDING: int = int(os.getenv("SAMUS_SLIDE_PADDING", "80"))


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlideImage:
    path: str
    slide_number: int
    width: int = _SLIDE_WIDTH
    height: int = _SLIDE_HEIGHT


@dataclass(frozen=True)
class CarouselImageResult:
    ok: bool
    slides: list[SlideImage] = field(default_factory=list)
    error: str = ""


# ---------------------------------------------------------------------------
# Slide text parser
# ---------------------------------------------------------------------------

_SLIDE_RE = re.compile(
    r"^(?:Slide\s+(\d+)|Final\s+slide)"
    r"(?:\s*\(([^)]+)\))?"
    r"\s*:\s*(.+)$",
    re.IGNORECASE,
)


def parse_carousel_text(raw: str) -> list[dict[str, str]]:
    """Parse the carousel outline emitted by ``repurpose.py``.

    Accepts lines like ``Slide 1 (cover): Title text`` or
    ``Final slide (CTA): Save this for later``.  Returns a list of dicts
    with keys ``number``, ``label``, and ``content``.
    """
    slides: list[dict[str, str]] = []
    next_number = 1
    for line in raw.strip().splitlines():
        m = _SLIDE_RE.match(line.strip())
        if not m:
            continue
        number = m.group(1) or str(next_number)
        label = (m.group(2) or "").strip().lower()
        content = m.group(3).strip()
        slides.append({"number": number, "label": label, "content": content})
        next_number = int(number) + 1
    return slides


# ---------------------------------------------------------------------------
# Font loader
# ---------------------------------------------------------------------------

_FONT_SEARCH_PATHS: Sequence[str] = (
    "arial.ttf",
    "Arial.ttf",
    "DejaVuSans.ttf",
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/Arial.ttf",
)


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if ImageFont is None:
        raise RuntimeError("Pillow not available")
    for name in _FONT_SEARCH_PATHS:
        try:
            return ImageFont.truetype(name, size)
        except (OSError, IOError):
            continue
    _LOG.debug("No system font found, falling back to Pillow default")
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Text wrapping helper
# ---------------------------------------------------------------------------


def _wrap_text(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, max_width: int) -> list[str]:
    """Word-wrap *text* so each line fits within *max_width* pixels."""
    avg_char_width = font.getlength("M") if hasattr(font, "getlength") else _FONT_SIZE_BODY * 0.6
    chars_per_line = max(1, int(max_width / avg_char_width))
    return textwrap.wrap(text, width=chars_per_line)


# ---------------------------------------------------------------------------
# Single slide renderer
# ---------------------------------------------------------------------------


def _render_slide(
    slide: dict[str, str],
    *,
    is_cover: bool,
    brand_name: str,
    font_title: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    font_body: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    font_watermark: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> Image.Image:
    img = Image.new("RGB", (_SLIDE_WIDTH, _SLIDE_HEIGHT), _BG_COLOR)
    draw = ImageDraw.Draw(img)

    usable_width = _SLIDE_WIDTH - 2 * _PADDING
    y_cursor = _PADDING

    label = slide.get("label") or f"slide {slide['number']}"
    draw.text((_PADDING, y_cursor), label.upper(), fill=_ACCENT_COLOR, font=font_body)
    y_cursor += _get_line_height(font_body) + 20

    content_font = font_title if is_cover else font_body
    lines = _wrap_text(slide["content"], content_font, usable_width)
    line_h = _get_line_height(content_font)
    total_text_h = line_h * len(lines)

    if is_cover:
        y_cursor = max(y_cursor, (_SLIDE_HEIGHT - total_text_h) // 2)
    else:
        remaining = _SLIDE_HEIGHT - y_cursor - _PADDING - _get_line_height(font_watermark) - 40
        if total_text_h < remaining:
            y_cursor += (remaining - total_text_h) // 2

    for line in lines:
        if is_cover:
            bbox = content_font.getbbox(line) if hasattr(content_font, "getbbox") else (0, 0, len(line) * 30, line_h)
            text_w = bbox[2] - bbox[0]
            x = (_SLIDE_WIDTH - text_w) // 2
        else:
            x = _PADDING
        draw.text((x, y_cursor), line, fill=_TEXT_COLOR, font=content_font)
        y_cursor += line_h

    wm_y = _SLIDE_HEIGHT - _PADDING
    wm_bbox = font_watermark.getbbox(brand_name) if hasattr(font_watermark, "getbbox") else (0, 0, len(brand_name) * 10, 20)
    wm_w = wm_bbox[2] - wm_bbox[0]
    wm_x = (_SLIDE_WIDTH - wm_w) // 2
    muted = tuple(max(0, min(255, c + 40)) for c in _BG_COLOR)
    draw.text((wm_x, wm_y), brand_name, fill=muted, font=font_watermark)

    return img


def _get_line_height(font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
    if hasattr(font, "getbbox"):
        bbox = font.getbbox("Ay")
        return int((bbox[3] - bbox[1]) * 1.3)
    return int(getattr(font, "size", _FONT_SIZE_BODY) * 1.3)


# ---------------------------------------------------------------------------
# Default output directory
# ---------------------------------------------------------------------------


def _default_out_dir() -> Path:
    try:
        from backend.common.state_paths import state_path

        return state_path("social", "carousel_images", str(int(time.time())))
    except Exception:
        import tempfile

        return Path(tempfile.mkdtemp(prefix="samus_carousel_"))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_carousel(
    carousel_text: str,
    *,
    out_dir: str | Path | None = None,
    brand_name: str = "HustleForge",
) -> CarouselImageResult:
    """Render a carousel outline into branded 1080x1080 PNGs.

    Parameters
    ----------
    carousel_text:
        Raw multi-line slide outline from ``repurpose.py``.
    out_dir:
        Directory for rendered PNGs.  Defaults to a timestamped folder
        under the writable state root.
    brand_name:
        Watermark text rendered at the bottom of each slide.

    Returns a :class:`CarouselImageResult`; never raises.
    """
    try:
        if not _PIL_AVAILABLE:
            return CarouselImageResult(ok=False, error="pillow_not_installed")

        slides = parse_carousel_text(carousel_text)
        if not slides:
            return CarouselImageResult(ok=False, error="no_slides_parsed")

        target = Path(out_dir) if out_dir else _default_out_dir()
        target.mkdir(parents=True, exist_ok=True)

        font_title = _load_font(_FONT_SIZE_TITLE)
        font_body = _load_font(_FONT_SIZE_BODY)
        font_watermark = _load_font(max(20, _FONT_SIZE_BODY // 2))

        result_slides: list[SlideImage] = []
        for idx, slide in enumerate(slides):
            is_cover = idx == 0
            img = _render_slide(
                slide,
                is_cover=is_cover,
                brand_name=brand_name,
                font_title=font_title,
                font_body=font_body,
                font_watermark=font_watermark,
            )
            filename = f"slide_{slide['number'].zfill(2)}.png"
            dest = target / filename
            img.save(str(dest), "PNG")
            result_slides.append(
                SlideImage(path=str(dest.resolve()), slide_number=int(slide["number"]))
            )
            _LOG.debug("Rendered slide %s -> %s", slide["number"], dest)

        _LOG.info("Carousel rendered: %d slides -> %s", len(result_slides), target)
        return CarouselImageResult(ok=True, slides=result_slides)

    except Exception:
        _LOG.exception("Carousel render failed")
        return CarouselImageResult(ok=False, error="render_failed")


def render_carousel_from_asset(
    asset_body: str,
    *,
    out_dir: str | Path | None = None,
    brand_name: str = "HustleForge",
) -> CarouselImageResult:
    """Convenience wrapper — renders a ``RepurposedAsset.body`` into slides."""
    return render_carousel(asset_body, out_dir=out_dir, brand_name=brand_name)


__all__ = [
    "CarouselImageResult",
    "SlideImage",
    "parse_carousel_text",
    "render_carousel",
    "render_carousel_from_asset",
]
