"""Samus social reel engine — MoneyPrinterTurbo-style short-video generation.

Turns a script into a narrated, subtitled, vertical (9:16) social reel, the way
`MoneyPrinterTurbo <https://github.com/harry0703/MoneyPrinterTurbo>`_ does, but
built natively on Samus's existing primitives instead of vendoring its app:

* :mod:`backend.social.video.script` — a reel script (hook + per-shot narration
  + visual prompt), LLM-assisted with a deterministic template fallback (the
  same fail-to-template pattern as :mod:`backend.social.repurpose`). It can also
  parse an existing repurposed ``ig_reel`` asset.
* :mod:`backend.social.video.voiceover` — free **edge-tts** narration -> mp3 +
  word-timed SRT subtitles (via ``SubMaker``; no faster-whisper model needed).
* :mod:`backend.social.video.footage` — per-shot AI footage by **reusing**
  :mod:`backend.website.media_gen` (Gemini stills by default, Veo motion clips
  as an approval-gated premium), metered through the SAME paid-media budget
  (:class:`backend.common.media_budget.MediaBudgetStore`).
* :mod:`backend.social.video.compose` — MoviePy composition: Ken-Burns motion
  on stills, burned-in captions, voiceover + optional ducked music, 1080x1920.
* :mod:`backend.social.video.pipeline` — the orchestrator that chains them.

This package **produces an asset; it never posts**. It is dormant-by-construction
(``social_reel_enabled`` default OFF), fail-closed (every stage returns a result,
never raises), and budget-metered. The heavy dependencies (``edge-tts``,
``moviepy``, and the ``ffmpeg`` binary) are imported lazily inside the modules
that need them, so importing this package — and running its dry-run / disabled
paths — works even when they are not installed.
"""
