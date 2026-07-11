"""CLI for the social reel engine — script preview + reel production.

Runs in the host venv. Dormant by default: ``reel`` only produces a video when
``SAMUS_SOCIAL_REEL_ENABLED=true`` and a ``GEMINI_API_KEY`` is set (plus the
heavy deps ``edge-tts`` + ``moviepy`` + ffmpeg installed). The ``script``
subcommand is always free + offline (templated unless ``--use-llm``).

Examples
--------
Preview the reel script (templated, $0, no video)::

    python -m backend.social.video.cli script \\
        --title "The 44% rule in AI content" \\
        --summary "44% of AI citations come from the first 30% of a page." \\
        --key-points "Front-load the answer;Add an FAQ block;Use schema markup"

Produce the reel MP4 (requires the flag + key + deps)::

    python -m backend.social.video.cli reel --title "The 44% rule" \\
        --key-points "Front-load the answer;Add an FAQ block" --use-llm
"""

from __future__ import annotations

import argparse
import json
import sys

from backend.social.models import BlogInput


def _blog(args: argparse.Namespace) -> BlogInput:
    key_points = [p.strip() for p in (args.key_points or "").split(";") if p.strip()]
    return BlogInput(
        title=args.title,
        url=getattr(args, "url", "") or "",
        summary=args.summary or "",
        key_points=key_points,
        cluster=getattr(args, "cluster", "") or "",
    )


def _cmd_script(args: argparse.Namespace) -> int:
    from backend.social.video.script import build_reel_script

    script = build_reel_script(
        _blog(args),
        max_segments=args.max_segments,
        use_llm=args.use_llm,
        aspect=args.aspect,
    )
    print(json.dumps(script.to_dict(), indent=2, ensure_ascii=False))
    return 0


def _cmd_reel(args: argparse.Namespace) -> int:
    from backend.social.video.pipeline import produce_reel

    result = produce_reel(_blog(args), use_llm=args.use_llm, video_approved=args.approve_video)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0 if result.ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backend.social.video.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name, func, helptext in (
        ("script", _cmd_script, "preview the reel script (free, offline)"),
        ("reel", _cmd_reel, "produce the reel MP4 (needs flag + key + deps)"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--title", required=True)
        p.add_argument("--url", default="")
        p.add_argument("--summary", default="")
        p.add_argument("--key-points", default="", help="semicolon-separated")
        p.add_argument("--cluster", default="")
        p.add_argument("--aspect", default="9:16")
        p.add_argument("--max-segments", type=int, default=5, dest="max_segments")
        p.add_argument(
            "--use-llm", action="store_true", help="budget-gated LLM (default templated)"
        )
        if name == "reel":
            p.add_argument(
                "--approve-video",
                action="store_true",
                help="approve Veo motion clips (only with SAMUS_SOCIAL_REEL_VIDEO_ENABLED)",
            )
        p.set_defaults(func=func)

    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
