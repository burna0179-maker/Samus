"""CLI for TikTok Shop research + campaigns. Dormant without config.

Examples::

    python -m backend.tiktok.cli research --query "kitchen gadgets" --limit 10
    python -m backend.tiktok.cli orders          # tiktok_shop seller orders (dormant)
"""

from __future__ import annotations

import argparse
import json
import sys


def _cmd_research(args: argparse.Namespace) -> int:
    from backend.common.config import get_settings
    from backend.tiktok.product_research import find_trending, rank_by_opportunity

    settings = get_settings()
    products = rank_by_opportunity(find_trending(args.query, settings=settings, limit=args.limit))
    print(
        json.dumps(
            {
                "provider": getattr(settings, "tiktok_research_provider", "none"),
                "count": len(products),
                "products": [p.to_dict() for p in products],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _cmd_orders(args: argparse.Namespace) -> int:
    from backend.common.config import get_settings
    from backend.tiktok.shop_seller import fetch_orders

    print(
        json.dumps(
            fetch_orders(settings=get_settings(), limit=args.limit), indent=2, ensure_ascii=False
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backend.tiktok.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    r = sub.add_parser("research", help="find trending products (pluggable provider)")
    r.add_argument("--query", required=True)
    r.add_argument("--limit", type=int, default=20)
    r.set_defaults(func=_cmd_research)

    o = sub.add_parser("orders", help="TikTok Shop seller orders (dormant until armed)")
    o.add_argument("--limit", type=int, default=50)
    o.set_defaults(func=_cmd_orders)

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
