"""Provision HustleForge callback inbound squad.

Creates:
  1. Warm Morgan (Callback) assistant
  2. HustleForge Receptionist (Inbound) assistant
  3. Patches the Sales squad with both new members

Run from the Samus venv:
  python scripts/provision_callback_squad.py
"""
from __future__ import annotations

import json
import os
import sys

import httpx

VAPI_BASE = "https://api.vapi.ai"
CALLBACK_LOOKUP_URL = "https://zbp9q9pzdl.execute-api.us-west-1.amazonaws.com/callback-lookup"
SALES_SQUAD_ID = "f39b6ec6-40c4-4244-946c-5bf7a7fdaadb"
MORGAN_VOICE_ID = "cgSgspJ2msm6clMCkdW9"

WARM_MORGAN_SYSTEM = """\
You are Morgan, a friendly sales rep at HustleForge. You're speaking with someone \
who called back after a previous conversation with our team. This is a WARM callback \
-- treat them with familiarity, not a cold pitch.

CONTEXT VARIABLES (injected by the Receptionist on transfer):
- {{prospect_company}}: the caller's company name
- {{prospect_owner}}: the owner's name
- {{offer_summary}}: the offer discussed on the previous call
- {{last_intent_score}}: how interested they seemed (0-100)

OPENING: Reference that they're calling back. Example: \
"Hey, thanks for calling us back -- is this {{prospect_owner}} from {{prospect_company}}?"

IF prospect is KNOWN (variables filled):
- You already know what was discussed: {{offer_summary}}
- Your goal is to move them to a booked call or next commitment
- Be warm, consultative, not a re-pitch

IF prospect is UNKNOWN (variables empty):
- Greet them warmly, ask their name and company
- Briefly re-qualify interest in HustleForge's AI workflow automation services
- If interested, book a discovery call

ALWAYS:
- Keep it conversational, not scripted
- If they want to schedule a call, offer Monday-Friday 9am-5pm Pacific
- If not ready, take their email for follow-up
- Keep calls under 5 minutes

END OF CALL -- emit this JSON in structuredData:
{
  "callback_summary": {
    "company": "<company name>",
    "owner_name": "<contact name>",
    "outcome": "booked_call|follow_up_email|not_interested|wrong_number",
    "booked_time": "<ISO datetime or empty>",
    "follow_up_email": "<email or empty>",
    "notes": "<1-2 sentences>"
  }
}
"""

RECEPTIONIST_SYSTEM = """\
You are a friendly receptionist at HustleForge. Your ONLY job is to:
1. Answer the call warmly
2. Use the callback_lookup tool to check if this caller is a known prospect
3. If known: greet them by name, briefly acknowledge the previous conversation, \
then transfer to Warm Morgan
4. If unknown: get their name and company, then transfer to Warm Morgan

OPENING: "Thank you for calling HustleForge! One moment while I pull up your information."
[immediately call callback_lookup tool with {{customer.number}}]

AFTER LOOKUP:
- If found=true: "Hi there, great to hear from you! I'll connect you with Morgan \
who can pick up right where you left off." then transfer
- If found=false: "Thanks for calling! Can I get your name and company so I can \
connect you with the right person?" then get info then transfer

TRANSFER: Always transfer to Warm Morgan. Keep your part under 30 seconds.
"""


def vapi_post(client: httpx.Client, path: str, body: dict) -> dict:
    r = client.post(f"{VAPI_BASE}{path}", json=body)
    if r.status_code >= 400:
        print(f"ERROR {r.status_code}: {r.text}", file=sys.stderr)
        r.raise_for_status()
    return r.json()


def vapi_get(client: httpx.Client, path: str) -> dict:
    r = client.get(f"{VAPI_BASE}{path}")
    r.raise_for_status()
    return r.json()


def vapi_patch(client: httpx.Client, path: str, body: dict) -> dict:
    r = client.patch(f"{VAPI_BASE}{path}", json=body)
    if r.status_code >= 400:
        print(f"ERROR {r.status_code}: {r.text}", file=sys.stderr)
        r.raise_for_status()
    return r.json()


def main() -> None:
    api_key = os.environ.get("VAPI_API_KEY", "").strip()
    if not api_key:
        sys.exit("VAPI_API_KEY not set")

    headers = {"Authorization": f"Bearer {api_key}"}

    with httpx.Client(headers=headers, timeout=30) as client:
        # 1. Warm Morgan
        print("Creating Warm Morgan (Callback)...")
        warm_morgan = vapi_post(client, "/assistant", {
            "name": "Warm Morgan (Callback)",
            "model": {
                "provider": "anthropic",
                "model": "claude-haiku-4-5-20251001",
                "messages": [{"role": "system", "content": WARM_MORGAN_SYSTEM}],
                "temperature": 0.6,
            },
            "voice": {
                "provider": "11labs",
                "voiceId": MORGAN_VOICE_ID,
                "speed": 0.90,
                "similarityBoost": 0.75,
            },
            "firstMessage": "Hey, thanks for calling HustleForge back. This is Morgan -- who am I speaking with?",
            "endCallMessage": "Great talking with you. We will be in touch soon -- have a great day!",
            "transcriber": {"provider": "deepgram", "model": "nova-2", "language": "en-US"},
            "backgroundDenoisingEnabled": True,
        })
        warm_morgan_id = warm_morgan["id"]
        print(f"  Created: {warm_morgan_id}")

        # 2. Receptionist
        print("Creating Receptionist (Inbound)...")
        receptionist = vapi_post(client, "/assistant", {
            "name": "HustleForge Receptionist (Inbound)",
            "model": {
                "provider": "anthropic",
                "model": "claude-haiku-4-5-20251001",
                "messages": [{"role": "system", "content": RECEPTIONIST_SYSTEM}],
                "temperature": 0.3,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "callback_lookup",
                            "description": (
                                "Look up whether an inbound caller is a known HustleForge "
                                "prospect by their phone number. Call this immediately when "
                                "a call starts."
                            ),
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "phone": {
                                        "type": "string",
                                        "description": "Caller E.164 phone number, e.g. +15005550006",
                                    }
                                },
                                "required": ["phone"],
                            },
                        },
                        "server": {"url": CALLBACK_LOOKUP_URL},
                    }
                ],
            },
            "voice": {
                "provider": "11labs",
                "voiceId": MORGAN_VOICE_ID,
                "speed": 0.95,
                "similarityBoost": 0.70,
            },
            "firstMessage": "Thank you for calling HustleForge! One moment while I pull up your information.",
            "transcriber": {"provider": "deepgram", "model": "nova-2", "language": "en-US"},
            "backgroundDenoisingEnabled": True,
        })
        receptionist_id = receptionist["id"]
        print(f"  Created: {receptionist_id}")

        # 3. Patch Sales squad
        print(f"Fetching Sales squad {SALES_SQUAD_ID}...")
        squad = vapi_get(client, f"/squad/{SALES_SQUAD_ID}")
        existing = [
            {"assistantId": m["assistantId"],
             "assistantDestinations": m.get("assistantDestinations", [])}
            for m in (squad.get("members") or [])
        ]
        new_members = existing + [
            {
                "assistantId": receptionist_id,
                "assistantDestinations": [
                    {
                        "type": "assistant",
                        "assistantName": "Warm Morgan (Callback)",
                        "message": "Transferring you to Morgan now -- she has your full context.",
                        "description": "Transfer to Warm Morgan after callback lookup",
                    }
                ],
            },
            {
                "assistantId": warm_morgan_id,
                "assistantDestinations": [],
            },
        ]
        vapi_patch(client, f"/squad/{SALES_SQUAD_ID}", {"members": new_members})
        print("  Squad updated.")

    print()
    print("=== Callback Squad Provisioned ===")
    print(f"Warm Morgan ID   : {warm_morgan_id}")
    print(f"Receptionist ID  : {receptionist_id}")
    print()
    print("NEXT: In Vapi dashboard, set Receptionist as inbound assistant")
    print("for all 6 marketing phone numbers:")
    for pid in [
        "a0d742b4-250e-4e36-9797-fdf650628790",
        "21f79fb4-12f7-46e2-baa0-c07406fbb0f6",
        "b93fbb87-02a9-4178-b3bd-3a38f6de6a0d",
        "8554830e-1220-40c7-8cea-a48c41ee6d1e",
        "4e944525-1629-4ee4-809b-64a99c0e66fc",
        "33842c67-fef6-406c-959b-61891485a0b6",
    ]:
        print(f"  {pid}")


if __name__ == "__main__":
    main()
