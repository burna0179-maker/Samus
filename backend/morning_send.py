"""Send the Samus morning briefing to email, Discord, and/or Telegram.

Reuses ``backend.morning.render_briefing()`` to build the text, then pushes to
whichever channels are configured. Channels are independent — a failure on one
does not suppress the others.

Channels are selected by environment variable:

    SAMUS_BRIEF_EMAIL_TO           email recipient (omit -> skip email)
    SAMUS_BRIEF_DISCORD_WEBHOOK    Discord webhook URL (omit -> skip discord)
    SAMUS_BRIEF_TELEGRAM_TOKEN     Telegram bot token   (with the chat id below)
    SAMUS_BRIEF_TELEGRAM_CHAT_ID   Telegram chat id     (both -> enable telegram)

Email path reuses ``backend.common.email_backend.send_email`` and therefore
needs ``SENDGRID_API_KEY`` + ``SENDGRID_FROM_EMAIL`` already in env (loaded
from DPAPI by the ``scripts/Send-Morning.ps1`` wrapper).

Discord path needs only the webhook URL — Discord does not authenticate
webhooks beyond the URL secret itself.

Exit code:
    0   at least one channel succeeded, OR no channels configured (graceful skip)
    1   at least one configured channel FAILED (other channels may still have
        succeeded — check stdout for OK/FAIL lines)

Run from the Samus venv (not directly — use the wrapper for secrets):

    python -m backend.morning_send
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

import httpx

from backend.common.email_backend import EmailBackendError, send_email
from backend.morning import render_briefing


_LOG = logging.getLogger("samus.morning_send")
_HTTP_TIMEOUT = 15.0

# Discord embed-description limit; we send full briefing as a .txt attachment
# instead of squeezing into a content field (2000 char cap) or embed (4096).
_DISCORD_HEADLINE_FALLBACK = "morning briefing ready (see attachment)"


# --------------------------------------------------------------------------
# Today's prospect call list (optional second attachment)
# --------------------------------------------------------------------------

def _today_call_list(today: date) -> tuple[Path, bytes] | None:
    """Return (path, contents) for today's call list, or None if missing.

    Mirrors backend.morning._today_call_list_path but also reads the bytes
    so the caller can attach it without a second filesystem hop. Defensive
    against any import or read failure — a missing/broken call list must
    not block the briefing from going out.
    """
    try:
        from backend.common import storage
        path = storage.root() / "daily_calls" / f"morning_call_list_{today.isoformat()}.txt"
        if not path.exists():
            return None
        return path, path.read_bytes()
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Briefing capture
# --------------------------------------------------------------------------

def _build_briefing() -> str:
    """Render the briefing with colors off (defensive — we're not a TTY).

    Also sweeps service-tier SLAs before rendering so any newly-overdue items
    show up in today's brief instead of waiting for tomorrow's. Sweep failures
    are swallowed — the brief must always render. Imports are lazy so a load
    error in services.sla_timer can't break morning_send itself.
    """
    os.environ["SAMUS_MORNING_NO_COLOR"] = "1"
    try:
        from backend.memory.customers import CustomerStore
        from backend.services import sla_timer
        sla_timer.sweep_overdue(CustomerStore())
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("services_sla_sweep_failed: %s", exc)
    return render_briefing()


def _extract_headline(briefing: str) -> str:
    """Pluck the ``CRITICAL --`` header line as a one-line summary.

    Used for the Discord message body so the operator gets an at-a-glance
    counter (e.g. ``CRITICAL -- TODAY (3 actions, 1 critical gaps, 2 overdue)``)
    in the notification without opening the attachment.
    """
    for raw in briefing.splitlines():
        line = raw.strip()
        if line.startswith("CRITICAL --"):
            return line
    return _DISCORD_HEADLINE_FALLBACK


def _html_escape(s: str) -> str:
    """Minimal HTML escape so the briefing's `<` / `>` / `&` render literally."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _build_subject(today: date) -> str:
    return f"Samus morning brief — {today.strftime('%a %b %d, %Y')}"


# --------------------------------------------------------------------------
# Email channel (SendGrid via existing adapter)
# --------------------------------------------------------------------------

def _send_email(
    briefing: str,
    to: str,
    today: date,
    *,
    call_list: tuple[Path, bytes] | None = None,
) -> dict[str, str]:
    """Send the briefing as a single email — plain-text body + HTML <pre>.

    When ``call_list`` is provided, the .txt file is attached so the operator
    can open it directly from the email (mobile inbox, remote machine, etc.)
    without needing host filesystem access to the artifact path.

    Raises whatever the underlying ``send_email`` raises (ValueError on
    missing required settings, EmailBackendError on SendGrid 4xx/5xx).
    """
    subject = _build_subject(today)
    html_body = (
        "<html><body>"
        "<pre style=\"font-family: Consolas, 'Courier New', monospace; "
        "font-size: 12px; line-height: 1.35; white-space: pre;\">"
        f"{_html_escape(briefing)}"
        "</pre></body></html>"
    )
    attachments: list[dict] = []
    if call_list is not None:
        path, contents = call_list
        attachments.append({
            "filename": path.name,
            "content": contents,
            "mime_type": "text/plain",
        })
    return send_email(
        to=to,
        subject=subject,
        body=briefing,
        html_body=html_body,
        attachments=attachments or None,
        message_kind="transactional",  # operator's own morning brief — not marketing
    )


# --------------------------------------------------------------------------
# Discord channel (webhook → multipart with .txt attachment)
# --------------------------------------------------------------------------

def _send_discord(
    briefing: str,
    webhook_url: str,
    today: date,
    *,
    call_list: tuple[Path, bytes] | None = None,
) -> dict[str, str]:
    """POST briefing to a Discord webhook as headline content + .txt attachment.

    Why an attachment instead of cramming into the content field: Discord caps
    content at 2000 chars and embed description at 4096; the briefing is
    routinely 5-10 KB. A .txt attachment renders inline with a preview pane
    in Discord and avoids any chunking logic on our side.

    When ``call_list`` is provided, it ships as a second attachment
    (``files[1]``) so the operator can open it from mobile / remote.
    """
    if not webhook_url.startswith(("http://", "https://")):
        raise ValueError("discord webhook URL must start with http:// or https://")

    headline = _extract_headline(briefing)
    call_list_line = "\n_Call list attached._" if call_list is not None else ""
    content = (
        f"**Samus morning brief — {today.strftime('%a %b %d, %Y')}**\n"
        f"```{headline}```\n"
        f"_Full briefing attached._{call_list_line}"
    )
    # Discord caps content at 2000 chars — our composed string is well under.
    if len(content) > 1900:
        content = content[:1900] + "…"

    files: dict[str, tuple[str, bytes, str]] = {
        "files[0]": (
            f"morning_brief_{today.isoformat()}.txt",
            briefing.encode("utf-8"),
            "text/plain",
        ),
    }
    if call_list is not None:
        cl_path, cl_bytes = call_list
        files["files[1]"] = (cl_path.name, cl_bytes, "text/plain")

    payload = {"content": content}
    data = {"payload_json": json.dumps(payload)}

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.post(webhook_url, data=data, files=files)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"discord_transport_error: {exc}") from exc

    if response.status_code >= 400:
        # Discord 4xx bodies are short JSON; truncate defensively.
        raise RuntimeError(
            f"discord_http_{response.status_code}: {response.text[:200]}"
        )

    return {
        "channel": "discord",
        "status_code": str(response.status_code),
        "to": webhook_url.split("/api/webhooks/", 1)[-1].split("/", 1)[0] or "?",
    }


# --------------------------------------------------------------------------
# Telegram channel (Bot API sendDocument with .txt attachment)
# --------------------------------------------------------------------------

_TELEGRAM_API = "https://api.telegram.org"


def _send_telegram(
    briefing: str,
    token: str,
    chat_id: str,
    today: date,
    *,
    call_list: tuple[Path, bytes] | None = None,
) -> dict[str, str]:
    """POST briefing to a Telegram chat as a .txt document + headline caption.

    Uses the Bot API ``sendDocument`` endpoint (same rationale as the Discord
    attachment: the briefing routinely exceeds the 4096-char ``sendMessage``
    cap). When ``call_list`` is provided it ships as a second, best-effort
    ``sendDocument`` — a call-list failure never fails the brief that already
    went out. Requires a bot token + chat id (both from env; a missing either
    means the channel is simply not selected in ``main``).
    """
    headline = _extract_headline(briefing)
    caption = (
        f"Samus morning brief — {today.strftime('%a %b %d, %Y')}\n{headline}"
    )
    if len(caption) > 1000:  # Telegram caption cap is 1024
        caption = caption[:1000] + "…"

    url = f"{_TELEGRAM_API}/bot{token}/sendDocument"
    data = {"chat_id": chat_id, "caption": caption}
    files = {
        "document": (
            f"morning_brief_{today.isoformat()}.txt",
            briefing.encode("utf-8"),
            "text/plain",
        ),
    }
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.post(url, data=data, files=files)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"telegram_transport_error: {exc}") from exc

    if response.status_code >= 400:
        raise RuntimeError(
            f"telegram_http_{response.status_code}: {response.text[:200]}"
        )

    # Second document — the call list. Best-effort: the brief already landed.
    if call_list is not None:
        cl_path, cl_bytes = call_list
        try:
            with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
                client.post(
                    url,
                    data={"chat_id": chat_id, "caption": "Today's call list"},
                    files={"document": (cl_path.name, cl_bytes, "text/plain")},
                )
        except httpx.HTTPError as exc:  # noqa: BLE001 — call list is optional
            _LOG.warning("telegram_call_list_failed: %s", exc)

    return {
        "channel": "telegram",
        "status_code": str(response.status_code),
        "to": str(chat_id),
    }


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    today = date.today()
    email_to = (os.getenv("SAMUS_BRIEF_EMAIL_TO") or "").strip()
    discord_url = (os.getenv("SAMUS_BRIEF_DISCORD_WEBHOOK") or "").strip()
    tg_token = (os.getenv("SAMUS_BRIEF_TELEGRAM_TOKEN") or "").strip()
    tg_chat = (os.getenv("SAMUS_BRIEF_TELEGRAM_CHAT_ID") or "").strip()
    telegram_on = bool(tg_token and tg_chat)

    if not email_to and not discord_url and not telegram_on:
        sys.stderr.write(
            "morning_send: no channels configured (set SAMUS_BRIEF_EMAIL_TO, "
            "SAMUS_BRIEF_DISCORD_WEBHOOK, and/or SAMUS_BRIEF_TELEGRAM_TOKEN + "
            "SAMUS_BRIEF_TELEGRAM_CHAT_ID)\n"
        )
        return 0  # graceful skip — not a failure

    # Build once; both channels share the same text + call-list attachment.
    briefing = _build_briefing()
    call_list = _today_call_list(today)
    cl_note = f" + {call_list[0].name}" if call_list is not None else " (no call list today)"

    successes: list[str] = []
    failures: list[str] = []

    if email_to:
        try:
            result = _send_email(briefing, email_to, today, call_list=call_list)
            msg_id = result.get("message_id") or "(no msg id)"
            successes.append(f"email -> {email_to}  msg_id={msg_id}{cl_note}")
        except (EmailBackendError, ValueError) as exc:
            failures.append(f"email -> {email_to}: {exc}")

    if discord_url:
        try:
            result = _send_discord(briefing, discord_url, today, call_list=call_list)
            wh_id = result.get("to", "?")
            successes.append(
                f"discord -> webhook {wh_id}  http={result['status_code']}{cl_note}"
            )
        except (RuntimeError, ValueError) as exc:
            failures.append(f"discord: {exc}")

    if telegram_on:
        try:
            result = _send_telegram(briefing, tg_token, tg_chat, today, call_list=call_list)
            successes.append(
                f"telegram -> chat {result['to']}  http={result['status_code']}{cl_note}"
            )
        except (RuntimeError, ValueError) as exc:
            failures.append(f"telegram: {exc}")

    for s in successes:
        print(f"OK:   {s}")
    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
