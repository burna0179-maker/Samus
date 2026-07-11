"""Samus social-content engine (Phase D of the growth-enrichment roadmap).

A DORMANT-by-construction package that turns the existing single-shot social
adapter into a content *system*:

* :mod:`backend.social.repurpose` — one blog post -> an 8-asset native social
  package (LinkedIn text / carousel / link, Instagram carousel / reel, X
  thread, Facebook community post / link post), LLM-assisted with deterministic
  template fallback.
* :mod:`backend.social.calendar` — the monthly-theme + weekly-rhythm content
  calendar that slots those assets across LinkedIn / Instagram / X / Facebook
  (18 slots/week) and enforces the 40/25/20/15 educate/prove/engage/convert mix.
* :mod:`backend.social.adapters` — unified dispatch across all four platforms.
  LinkedIn + Facebook delegate to the proven ``outreach.social_adapter``;
  Instagram + X are handled natively. Instagram supports **photo**, **reel**
  (public MP4 URL), **carousel** (multi-image via Graph API child containers),
  and **story** (image or video with ``media_type=STORIES``).
* :mod:`backend.social.image_gen` — carousel slide image generator: renders
  repurposed slide outlines into branded 1080x1080 PNGs (Pillow). Lazy-imports
  Pillow; degrades cleanly when absent.
* :mod:`backend.social.video` — MoneyPrinterTurbo-style reel engine: turns a
  script into a narrated, subtitled 9:16 MP4 (edge-tts voiceover + AI footage
  reusing ``website.media_gen`` + MoviePy composition). Dormant by default
  (``social_reel_enabled``) and heavy deps are lazy-imported.

Nothing here posts to a live network unless an operator sets
``SAMUS_SOCIAL_DRY_RUN=false`` *and* seeds per-platform credentials. The
generation + planning surface is pure and side-effect-free (other than an
auditable JSONL calendar ledger).
"""
