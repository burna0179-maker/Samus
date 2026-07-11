"""Operator CLI — sync Morgan's local assistant snapshot to the LIVE Vapi assistant.

The canonical, reviewable copy of Morgan's system prompt + voicemail settings
lives in ``backend/voice/assistant_configs/morgan_sdr.json``. The LIVE Vapi
assistant (id in that snapshot / ``VAPI_ASSISTANT_ID``) can drift from it —
either by hand-edits in the Vapi dashboard or by the mid-session prompt patcher
in :mod:`backend.voice.call_session_monitor`. This CLI pushes the snapshot's
system prompt and voicemail configuration back onto the live assistant so the
reviewed wording is what actually runs.

It PATCHes ONLY the fields it is asked to sync — the model system prompt and the
voicemail settings (``voicemailMessage`` + ``voicemailDetection`` if included).
It never touches ``server.url``/``secret`` (owned by Sync-VoiceWebhook.ps1),
voice, transcriber, or any unrelated field, so running this cannot clobber the
live tunnel binding.

SAFETY: DRY-RUN by default. It prints the exact PATCH body and a unified diff of
the live vs snapshot system prompt, then EXITS WITHOUT sending anything. You must
pass ``--apply`` to actually PATCH the live assistant. This mirrors the
``-Live/-Apply`` gate on Sync-MorganAssistant.ps1.

Usage::

    # dry-run — show what WOULD change, send nothing (default):
    python -m backend.voice.sync_assistant
    # actually PATCH the live assistant:
    python -m backend.voice.sync_assistant --apply
    # sync voicemail settings too (message + detection), not just the prompt:
    python -m backend.voice.sync_assistant --voicemail --apply

The Vapi api key + assistant id come from settings (DPAPI-backed
``VapiApiKey`` / ``VapiAssistantId``), never hard-coded.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any

from backend.common.config import get_settings

from .client import VapiClient, VapiError

# The reviewed snapshot lives next to this module.
_SNAPSHOT_PATH = Path(__file__).resolve().parent / "assistant_configs" / "morgan_sdr.json"


def load_snapshot(path: Path = _SNAPSHOT_PATH) -> dict[str, Any]:
    """Load and parse the local assistant snapshot JSON."""
    return json.loads(path.read_text(encoding="utf-8"))


def extract_system_prompt(snapshot: dict[str, Any]) -> str:
    """Pull the system-role message content from the snapshot's model.messages."""
    messages = (snapshot.get("model") or {}).get("messages") or []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content
    raise ValueError(
        f"no non-empty system message found in snapshot model.messages ({_SNAPSHOT_PATH.name})",
    )


def build_patch_body(snapshot: dict[str, Any], *, include_voicemail: bool) -> dict[str, Any]:
    """Build the minimal PATCH body from the snapshot.

    Always includes the system prompt. When ``include_voicemail`` is set, also
    syncs ``voicemailMessage`` and (if present) ``voicemailDetection`` — the
    detection block is copied verbatim from the snapshot so this never weakens
    it, only mirrors whatever the reviewed snapshot declares.

    Nothing else is included: no server, voice, transcriber, or other field is
    ever sent, so a sync cannot disturb the live tunnel binding or voice config.
    """
    system_prompt = extract_system_prompt(snapshot)
    # Vapi validates the model object on PATCH and REQUIRES model.provider (and
    # model.model). A messages-only body 400s ("model.provider must be one of
    # ..."). So send the snapshot's full model object — provider, model,
    # toolIds, knowledgeBase — with the reviewed system prompt substituted in.
    # The snapshot is the pulled live config, so these are the current valid
    # values; we only change the system message content.
    snap_model = dict((snapshot.get("model") or {}))
    snap_model["messages"] = [{"role": "system", "content": system_prompt}]
    body: dict[str, Any] = {"model": snap_model}
    if include_voicemail:
        vm_message = snapshot.get("voicemailMessage")
        if isinstance(vm_message, str) and vm_message.strip():
            body["voicemailMessage"] = vm_message
        vm_detection = snapshot.get("voicemailDetection")
        if isinstance(vm_detection, dict) and vm_detection:
            body["voicemailDetection"] = vm_detection
    return body


def _live_system_prompt(assistant: dict[str, Any]) -> str:
    """Extract the current system prompt from a live assistant payload."""
    messages = (assistant.get("model") or {}).get("messages") or []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            return str(msg.get("content") or "")
    return ""


def _prompt_diff(live_prompt: str, snapshot_prompt: str) -> str:
    """Unified diff (live -> snapshot) of the two system prompts."""
    diff = difflib.unified_diff(
        live_prompt.splitlines(),
        snapshot_prompt.splitlines(),
        fromfile="live (Vapi)",
        tofile="snapshot (morgan_sdr.json)",
        lineterm="",
    )
    return "\n".join(diff)


def _force_utf8_stdout() -> None:
    """Make stdout/stderr UTF-8 so printing the prompt diff (em-dashes, arrows)
    never crashes on a cp1252 Windows console."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover — best-effort
                pass


def sync(*, apply: bool, include_voicemail: bool) -> int:
    """Dry-run (default) or apply the snapshot -> live assistant sync.

    Returns a process exit code (0 = ok).
    """
    _force_utf8_stdout()
    settings = get_settings()
    api_key = (getattr(settings, "vapi_api_key", "") or "").strip()
    assistant_id = (getattr(settings, "vapi_assistant_id", "") or "").strip()
    if not api_key:
        print("error: VAPI_API_KEY is not set — cannot reach the Vapi REST API.", file=sys.stderr)
        return 2
    if not assistant_id:
        print("error: VAPI_ASSISTANT_ID is not set — nothing to sync to.", file=sys.stderr)
        return 2

    try:
        snapshot = load_snapshot()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: could not load snapshot {_SNAPSHOT_PATH}: {exc}", file=sys.stderr)
        return 1

    try:
        body = build_patch_body(snapshot, include_voicemail=include_voicemail)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    snapshot_prompt = extract_system_prompt(snapshot)

    client = VapiClient(api_key=api_key)

    # Fetch the live assistant so we can show a diff of what would change.
    try:
        live = client.get_assistant(assistant_id)
    except VapiError as exc:
        print(f"error: could not fetch live assistant {assistant_id}: {exc}", file=sys.stderr)
        return 1

    live_prompt = _live_system_prompt(live)
    diff = _prompt_diff(live_prompt, snapshot_prompt)

    print(f"assistant id : {assistant_id}")
    print(f"snapshot     : {_SNAPSHOT_PATH}")
    print(f"fields synced: {', '.join(sorted(body.keys()))}")
    print()
    if diff:
        print("SYSTEM PROMPT DIFF (live -> snapshot):")
        print(diff)
    else:
        print("SYSTEM PROMPT DIFF: (none — live already matches snapshot)")
    print()
    if include_voicemail:
        if "voicemailMessage" in body:
            print(f"voicemailMessage -> {body['voicemailMessage']!r}")
        if "voicemailDetection" in body:
            print(
                "voicemailDetection -> "
                + json.dumps(body["voicemailDetection"], ensure_ascii=False)
            )
        print()

    if not apply:
        print("DRY-RUN: no changes sent. Re-run with --apply to PATCH the live assistant.")
        return 0

    try:
        # Send the full build_patch_body directly. It carries the COMPLETE model
        # object (provider/model/toolIds/knowledgeBase + the reviewed messages) —
        # Vapi 400s on a messages-only model patch ("model.provider must be one
        # of ..."), which is exactly what client.patch_assistant_config builds,
        # so we bypass it here. Still only sends model (+ voicemail when asked);
        # never server/voice/transcriber, so the tunnel binding is untouched.
        client._patch(f"/assistant/{assistant_id}", body)  # noqa: SLF001
    except VapiError as exc:
        print(f"error: PATCH failed: {exc}", file=sys.stderr)
        return 1

    print(f"APPLIED: patched live assistant {assistant_id}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.voice.sync_assistant",
        description="Sync the reviewed Morgan snapshot (system prompt + voicemail) "
        "to the LIVE Vapi assistant. DRY-RUN unless --apply.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually PATCH the live assistant (default is a dry-run that only "
        "prints the diff and sends nothing)",
    )
    parser.add_argument(
        "--voicemail",
        action="store_true",
        help="also sync voicemailMessage + voicemailDetection from the snapshot "
        "(default: system prompt only)",
    )
    args = parser.parse_args(argv)
    return sync(apply=args.apply, include_voicemail=args.voicemail)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
