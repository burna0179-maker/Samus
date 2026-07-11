"""CLI for the AIO visibility workcell — probe AI engines for brand citations.

Runs in the host venv. Claude probes are budget-gated; OpenAI/Perplexity run
only if their keys are set. All probing is read-only.

Examples
--------
    python -m backend.visibility.cli probe \\
        --questions "what is the best AI SDR tool;best autonomous prospecting software" \\
        --brand "Hustleforge,Samus" \\
        --competitors "Apollo,Clay,Outreach" \\
        --platforms claude

    python -m backend.visibility.cli probe --questions-file icp_questions.txt \\
        --brand "Hustleforge" --platforms claude,openai,perplexity
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from backend.visibility.probe import run_probes


def _split(value: str) -> list[str]:
    sep = ";" if ";" in value else ","
    return [v.strip() for v in value.split(sep) if v.strip()]


def _cmd_probe(args: argparse.Namespace) -> int:
    if args.questions_file:
        text = Path(args.questions_file).read_text(encoding="utf-8")
        questions = [ln.strip() for ln in text.splitlines() if ln.strip()]
    else:
        questions = _split(args.questions or "")
    if not questions:
        print("no questions provided", file=sys.stderr)
        return 2

    brand = _split(args.brand or "")
    competitors = _split(args.competitors or "")
    platforms = _split(args.platforms or "claude")

    report = run_probes(
        questions,
        brand,
        competitors,
        platforms,  # type: ignore[arg-type]
        persist=not args.no_persist,
    )
    out = {
        "questions": len(questions),
        "platforms": report.platforms,
        "sov": asdict(report.sov) if report.sov else None,
        "probes": [asdict(p) for p in report.probes],
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backend.visibility.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    pr = sub.add_parser("probe", help="probe AI engines for brand citations")
    pr.add_argument("--questions", default="", help="';'- or ','-separated ICP questions")
    pr.add_argument("--questions-file", default="", help="file with one question per line")
    pr.add_argument("--brand", required=True, help="brand aliases, comma-separated")
    pr.add_argument("--competitors", default="", help="competitor names, comma-separated")
    pr.add_argument("--platforms", default="claude", help="claude,openai,perplexity")
    pr.add_argument("--no-persist", action="store_true", help="don't write the ledger")
    pr.set_defaults(func=_cmd_probe)
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
