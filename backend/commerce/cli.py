"""CLI for the Medusa commerce integration. Dormant without config.

Examples::

    python -m backend.commerce.cli products
    python -m backend.commerce.cli orders
    python -m backend.commerce.cli publish --title "Mini LED Lamp" --price-cents 2999
"""
from __future__ import annotations

import argparse
import json
import sys


def _cmd_products(args: argparse.Namespace) -> int:
    from backend.commerce.medusa_client import MedusaClient
    from backend.common.config import get_settings

    client = MedusaClient.from_settings(get_settings())
    items = client.list_products(limit=args.limit)
    print(json.dumps({"configured": client.configured, "count": len(items),
                      "products": [p.to_dict() for p in items]}, indent=2, ensure_ascii=False))
    return 0


def _cmd_orders(args: argparse.Namespace) -> int:
    from backend.commerce.catalog_sync import reconcile_orders
    from backend.common.config import get_settings

    print(json.dumps(reconcile_orders(settings=get_settings(), limit=args.limit),
                     indent=2, ensure_ascii=False))
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    from backend.commerce.catalog_sync import publish_product
    from backend.common.config import get_settings

    result = publish_product(
        title=args.title, description=args.description, price_usd_cents=args.price_cents,
        thumbnail=args.thumbnail, settings=get_settings(),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backend.commerce.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("products", help="list Medusa products")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=_cmd_products)

    o = sub.add_parser("orders", help="reconcile Medusa orders (products stream)")
    o.add_argument("--limit", type=int, default=50)
    o.set_defaults(func=_cmd_orders)

    pub = sub.add_parser("publish", help="create a draft product in Medusa")
    pub.add_argument("--title", required=True)
    pub.add_argument("--description", default="")
    pub.add_argument("--price-cents", type=int, default=0, dest="price_cents")
    pub.add_argument("--thumbnail", default="")
    pub.set_defaults(func=_cmd_publish)

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
