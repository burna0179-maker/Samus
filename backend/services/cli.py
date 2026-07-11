"""Services workcell CLI. Exposes ``samus services sla-check`` for overdue sweep + fulfill-driver."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from backend.services import sla_timer
from backend.services.fulfill_service import fulfill_service
from backend.services.registry import list_skus


def _cmd_sla_check(_args: argparse.Namespace) -> int:
    from backend.memory.customers import CustomerStore

    store = CustomerStore()
    fired = sla_timer.sweep_overdue(store)
    open_alerts = sla_timer.read_open_alerts(limit=25)
    sys.stdout.write(f"sla_check: fired={len(fired)} open_alerts_total={len(open_alerts)}\n")
    for a in fired:
        sys.stdout.write(
            f"  FIRED  {a['sku_id']:<30}  {a['customer_id']}  deadline={a['sla_deadline']}\n"
        )
    if open_alerts:
        sys.stdout.write("\nrecent alerts (newest last):\n")
        for a in open_alerts:
            sys.stdout.write(
                f"  {a.get('fired_at', '?'):<22}  {a.get('sku_id', '?'):<30}  "
                f"{a.get('customer_id', '?')}\n"
            )
    return 0


def _cmd_list_skus(_args: argparse.Namespace) -> int:
    for sku in list_skus():
        price = "TBD" if sku.price_usd_cents is None else f"${sku.price_usd_cents / 100:.2f}"
        sys.stdout.write(
            f"{sku.sku_id:<32}  {sku.display_name:<32}  {price:<10}  sla={sku.sla_hours}h\n"
        )
    return 0


def _cmd_fulfill(args: argparse.Namespace) -> int:
    intake: dict[str, Any] = {}
    if args.intake_json:
        intake = json.loads(args.intake_json)
    elif args.intake_file:
        with open(args.intake_file, "r", encoding="utf-8") as fh:
            intake = json.load(fh)
    intake.setdefault("email", args.email)
    if args.bottleneck:
        intake["bottleneck"] = args.bottleneck

    result = fulfill_service(
        sku_id=args.sku,
        email=args.email,
        intake_payload=intake,
        name=args.name,
        company=args.company,
        send_email=not args.no_send,
    )
    sys.stdout.write(_render(result) + "\n")
    return 0 if result.ok else 1


def _render(result) -> str:
    sep = "=" * 75
    out: list[str] = [sep]
    out.append(
        f"SAMUS SERVICES FULFILL  [{'OK' if result.ok else 'FAILED'}]  "
        f"{result.sku_id}  ->  {result.email}"
    )
    out.append(sep)
    if result.customer_id:
        out.append(f"  customer:        {result.customer_id}")
        out.append(f"  state:           {result.prior_state} -> {result.final_state}")
    if result.scope_path:
        out.append(f"  scope artifact:  {result.scope_path}")
    if result.sla_deadline:
        out.append(f"  sla deadline:    {result.sla_deadline}")
    if result.email_message_id:
        out.append(f"  email msg id:    {result.email_message_id}")
    if result.out_of_scope_reason:
        out.append(f"  scope-gate flag: {result.out_of_scope_reason}")
    out.append("")
    out.append("  steps:")
    for s in result.steps:
        out.append(f"    [{s.status:>7}]  {s.name:<32}  ({s.elapsed_ms} ms)  {s.detail}")
    out.append(sep)
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="samus services", description="Services workcell CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_skus = sub.add_parser("list-skus", help="List registered service SKUs")
    p_skus.set_defaults(func=_cmd_list_skus)

    p_sla = sub.add_parser("sla-check", help="Sweep overdue SLAs + print open alerts")
    p_sla.set_defaults(func=_cmd_sla_check)

    p_ful = sub.add_parser(
        "fulfill", help="Run the scope-confirmation chain for one customer + SKU"
    )
    p_ful.add_argument("--sku", required=True, help="SKU id (e.g. service_workflow_rescue)")
    p_ful.add_argument("--email", required=True, help="Customer email")
    p_ful.add_argument("--name", default="")
    p_ful.add_argument("--company", default="")
    p_ful.add_argument(
        "--bottleneck", default="", help="Customer's bottleneck text (overrides intake file)"
    )
    p_ful.add_argument("--intake-json", default="", help="Intake payload as inline JSON")
    p_ful.add_argument("--intake-file", default="", help="Intake payload JSON file path")
    p_ful.add_argument("--no-send", action="store_true", help="Skip the scope-confirmation email")
    p_ful.set_defaults(func=_cmd_fulfill)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
