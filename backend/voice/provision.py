"""Operator CLI — provision a Vapi DID for an AI Digital Receptionist client.

For a one-client pilot, the inbound assistant is created by hand in the Vapi
dashboard (receptionist persona, recording enabled, an ``inbound_summary``
structured-output schema, a ``transferCall`` tool). This CLI handles the one
programmatic step that benefits from automation: buying a phone number and
binding it to that assistant, then printing the ids the operator pastes into

  * the client's ``customers/<slug>/receptionist/config.yaml``
    (``vapi_assistant_id`` + ``vapi_phone_number_id`` + ``phone_numbers``)
  * the voice workcell ``.env`` (``VAPI_INBOUND_ASSISTANT_ID`` +
    ``VAPI_INBOUND_PHONE_NUMBER_ID``)

Usage::

    python -m backend.voice.provision --list
    python -m backend.voice.provision buy --assistant-id <id> \\
        [--name "Acme Receptionist"] [--area-code 530]
    python -m backend.voice.provision import-twilio --assistant-id <id> \\
        --number +15305551234 [--name "Acme Main Line"] [--no-sms]

``import-twilio`` is the migration path off Vapi-bought numbers: Vapi will
not port those out, so you buy a number in your OWN Twilio account, import it
here (requires TwilioAccountSid / TwilioAuthToken seeded in DPAPI), cut Samus
over to the printed id, then release the old Vapi DID. The number stays owned
by Twilio.

Nothing here mutates Samus state — it only calls the Vapi REST API and prints.
"""

from __future__ import annotations

import argparse
import sys

from backend.common.config import get_settings

from .client import VapiClient, VapiError


def _client() -> VapiClient:
    """Build a VapiClient from settings, or exit 2 when the key is unset."""
    key = (get_settings().vapi_api_key or "").strip()
    if not key:
        print("error: VAPI_API_KEY is not set — cannot reach the Vapi REST API.", file=sys.stderr)
        raise SystemExit(2)
    return VapiClient(api_key=key)


def list_dids() -> int:
    """Print every provisioned Vapi phone number."""
    try:
        numbers = _client().list_phone_numbers()
    except VapiError as exc:
        print(f"error: vapi list_phone_numbers failed: {exc}", file=sys.stderr)
        return 1
    if not numbers:
        print("(no Vapi phone numbers provisioned)")
        return 0
    for pn in numbers:
        print(
            f"  {pn.id}  {pn.number or '(pending)':<16}  "
            f"assistant={pn.assistantId or '(unbound)'}  name={pn.name or ''}"
        )
    return 0


def buy_did(*, assistant_id: str, name: str, area_code: str | None) -> int:
    """Buy a DID, bind it to ``assistant_id``, print the ids to record."""
    try:
        pn = _client().create_phone_number(
            assistant_id=assistant_id,
            name=name,
            area_code=area_code,
        )
    except VapiError as exc:
        print(f"error: vapi create_phone_number failed: {exc}", file=sys.stderr)
        return 1
    print("Provisioned a Vapi inbound DID:")
    print(f"  phone number     : {pn.number or '(provisioning — re-run --list)'}")
    print(f"  vapi_phone_number_id : {pn.id}")
    print(f"  bound assistant  : {pn.assistantId or assistant_id}")
    print()
    print("Record these:")
    print(f"  config.yaml  -> vapi_assistant_id: {assistant_id}")
    print(f"  config.yaml  -> vapi_phone_number_id: {pn.id}")
    if pn.number:
        print(f"  config.yaml  -> phone_numbers: ['{pn.number}']")
    print(f"  .env         -> VAPI_INBOUND_ASSISTANT_ID={assistant_id}")
    print(f"  .env         -> VAPI_INBOUND_PHONE_NUMBER_ID={pn.id}")
    return 0


def import_twilio_did(*, assistant_id: str, number: str, name: str, sms_enabled: bool) -> int:
    """Import a number you OWN in Twilio into Vapi as a twilio-provider DID.

    This is the migration path off Vapi-bought numbers: Vapi will not port
    those out, so the operator buys a number in their own Twilio account,
    runs this to register it with Vapi (Vapi verifies it against the Twilio
    credentials and wires its webhooks), then cuts ``VAPI_PHONE_NUMBER_ID`` /
    ``VAPI_INBOUND_PHONE_NUMBER_ID`` over to the printed id and releases the
    old Vapi DID. The number stays owned by Twilio throughout.
    """
    settings = get_settings()
    sid = (settings.twilio_account_sid or "").strip()
    tok = (settings.twilio_auth_token or "").strip()
    api_key = (settings.twilio_api_key or "").strip()
    api_secret = (settings.twilio_api_secret or "").strip()
    have_api_key = bool(api_key and api_secret)
    # Fail closed — need the Account SID plus ONE auth method.
    if not sid or not (have_api_key or tok):
        if not sid:
            print(
                "error: TWILIO_ACCOUNT_SID (the Account SID, AC...) not set — "
                "seed TwilioAccountSid via Set-HfSecret -Scope Samus.",
                file=sys.stderr,
            )
        else:
            print(
                "error: no Twilio auth set — seed either TwilioApiKey + "
                "TwilioApiSecret (preferred) or TwilioAuthToken via "
                "Set-HfSecret -Scope Samus before importing.",
                file=sys.stderr,
            )
        return 2
    try:
        pn = _client().create_phone_number(
            assistant_id=assistant_id,
            name=name,
            provider="twilio",
            number=number,
            twilio_account_sid=sid,
            twilio_auth_token=tok or None,
            twilio_api_key=api_key or None,
            twilio_api_secret=api_secret or None,
            sms_enabled=sms_enabled,
        )
    except (VapiError, ValueError) as exc:
        print(f"error: vapi twilio import failed: {exc}", file=sys.stderr)
        return 1
    print("Imported a Twilio-owned number into Vapi:")
    print(f"  phone number     : {pn.number or number}")
    print(f"  vapi_phone_number_id : {pn.id}")
    print("  provider         : twilio (owned in your Twilio account)")
    print(f"  bound assistant  : {pn.assistantId or assistant_id}")
    print()
    print("Cut Samus over to this id (the digits stay; only the id changes):")
    print(f"  config.yaml  -> vapi_phone_number_id: {pn.id}")
    print(f"  .env         -> VAPI_PHONE_NUMBER_ID={pn.id}   # outbound dialer")
    print(f"  .env         -> VAPI_INBOUND_PHONE_NUMBER_ID={pn.id}   # receptionist")
    print("Then release the old Vapi-bought DID in the Vapi dashboard.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.voice.provision",
        description="Provision a Vapi inbound DID for an AI Digital Receptionist client.",
    )
    parser.add_argument(
        "--list", action="store_true", help="list provisioned Vapi phone numbers and exit"
    )
    sub = parser.add_subparsers(dest="command")
    buy = sub.add_parser("buy", help="buy + bind a new Vapi-managed inbound DID")
    buy.add_argument(
        "--assistant-id", required=True, help="Vapi inbound assistant id to bind the DID to"
    )
    buy.add_argument("--name", default="", help="operator-facing label for the number")
    buy.add_argument("--area-code", default=None, help="preferred NANP area code (best-effort)")

    imp = sub.add_parser(
        "import-twilio",
        help="import a number you OWN in Twilio as a Vapi twilio-provider DID",
    )
    imp.add_argument(
        "--assistant-id", required=True, help="Vapi assistant id to bind the imported number to"
    )
    imp.add_argument(
        "--number", required=True, help="E.164 number you own in Twilio (e.g. +15305551234)"
    )
    imp.add_argument("--name", default="", help="operator-facing label for the number")
    imp.add_argument(
        "--no-sms", action="store_true", help="do not let Vapi manage the Twilio messaging webhook"
    )

    args = parser.parse_args(argv)
    if args.list:
        return list_dids()
    if args.command == "buy":
        return buy_did(assistant_id=args.assistant_id, name=args.name, area_code=args.area_code)
    if args.command == "import-twilio":
        return import_twilio_did(
            assistant_id=args.assistant_id,
            number=args.number,
            name=args.name,
            sms_enabled=not args.no_sms,
        )
    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
