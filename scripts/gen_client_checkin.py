"""Generate a warm, personal client check-in draft (customer success touch).

For each active client in the SEO registry, compose a genuine, non-salesy
check-in that (1) asks how they're doing, (2) surveys for any value HustleForge
could add, and (3) shares ONE relevant, TRUE update (a new offering or a
general security/SEO awareness note) — grounded in that client's real situation
(recent audit score, known issues, industry, upcoming meeting).

Uses the LOCAL model (LM Studio / gemma) — free, no broker dependency. Writes a
DRAFT for operator review; never sends. Sending is a separate, operator-armed
step. Personal client comms are too high-stakes to auto-send until the tone is
trusted.

Grounding facts are passed in explicitly; the model is instructed NOT to invent
news, dates, or specifics — only true, general guidance + what we give it.

Usage: python gen_client_checkin.py [customer_id]   (default: all active)
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone

LM_URL = "http://localhost:1234/v1/chat/completions"
CLIENTS = r"D:\Hustleforge\Samus\.data\host_artifacts\seo_clients\clients.json"
OPERATOR_NAME = "Andrew"
OPERATOR_EMAIL = "ahartman@hustleforge.tech"


def _latest_audit_summary(slug: str) -> str:
    import glob
    import os
    base = rf"D:\Hustleforge\Samus\.data\host_artifacts\seo_clients\{slug}"
    files = sorted(glob.glob(os.path.join(base, "audit_*.json")))
    if not files:
        return ""
    try:
        a = json.load(open(files[-1], encoding="utf-8-sig"))
        f = a.get("findings", {})
        issues = "; ".join(i.get("message", "") for i in a.get("issues", [])[:3])
        sec = (f.get("security") or {}).get("grade")
        return (f"Most recent SEO health score: {a.get('seo_score')}/100. "
                f"Top items we're working on: {issues}. "
                + (f"Site security currently grades {sec} (a hardening opportunity we can offer)." if sec and sec not in ("A", "A+") else ""))
    except Exception:
        return ""


def _slug(name: str) -> str:
    import re
    return re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()


def generate(client: dict) -> dict:
    first = (client.get("contact") or "").split()[0] or "there"
    slug = _slug(client.get("name", ""))
    grounding = _latest_audit_summary(slug)
    meeting = client.get("next_meeting", "")

    system = (
        f"You are {OPERATOR_NAME} from HustleForge, writing a SHORT, warm, "
        "genuinely personal check-in email to a real client you value. This is "
        "customer success, NOT sales or marketing. Rules:\n"
        "- Sound like a real human who cares, not a template or a bot. Warm, plain, "
        "concise (120-160 words). No corporate filler, no 'I hope this email finds "
        "you well', no exclamation-point hype.\n"
        "- Include exactly three beats, woven naturally: (1) a genuine personal "
        "check-in on how they and their organization are doing; (2) an open "
        "invitation/survey — is there anything about their website or online "
        "presence that's been frustrating, or anything we could take off their "
        "plate; (3) ONE brief, useful update or heads-up relevant to them.\n"
        "- For beat (3), you MAY reference the grounding facts provided and true, "
        "GENERAL best-practice awareness (e.g., browsers flagging insecure sites, "
        "the value of website security hardening, local-search basics). Do NOT "
        "invent specific news, statistics, dates, product names, or events. If you "
        "don't have a specific true fact, keep it general.\n"
        "- Warm sign-off from " + OPERATOR_NAME + ". Output as JSON: "
        '{"subject": "...", "body": "..."} and nothing else.'
    )
    ctx = [
        f"Client: {client.get('name')} ({client.get('location','')}).",
        f"Contact first name: {first}.",
        f"Their business: {client.get('industry') or 'a private Christian school'}.",
        f"They pay for: {client.get('plan')} (${client.get('mrr_usd')}/mo).",
    ]
    if grounding:
        ctx.append("Grounding facts (true, use as helpful): " + grounding)
    if meeting:
        ctx.append(f"NOTE: an in-person meeting is already scheduled: {meeting}. "
                   "Reference it warmly / look forward to it, rather than asking to set one up.")
    user = "\n".join(ctx) + "\n\nWrite the personal check-in email now (JSON only)."

    payload = {
        "model": "local",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.7,
        "max_tokens": 700,
    }
    req = urllib.request.Request(LM_URL, data=json.dumps(payload).encode(),
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=240) as r:
        raw = json.loads(r.read().decode())["choices"][0]["message"]["content"]
    # tolerate the model wrapping JSON in prose/fences
    s = raw.strip()
    if "```" in s:
        s = s.split("```")[1].lstrip("json").strip() if s.count("```") >= 2 else s
    try:
        obj = json.loads(s[s.find("{"): s.rfind("}") + 1])
    except Exception:
        obj = {"subject": f"Checking in — {client.get('name')}", "body": raw}
    return obj


CADENCE_DAYS = 49  # ~7 weeks (operator: every 6-8 weeks)
QUEUE = r"D:\Hustleforge\Samus\.data\host_artifacts\seo_clients\checkin_queue.jsonl"
HOLD_HOURS = 24.0


def _due(cl: dict, today: date, force: bool) -> bool:
    if force:
        return True
    last = cl.get("last_checkin")
    if not last:
        return True
    try:
        d = date.fromisoformat(str(last)[:10])
    except ValueError:
        return True
    return (today - d).days >= CADENCE_DAYS


def _enqueue(cl: dict, slug: str, draft_rel: str) -> None:
    now = datetime.now(timezone.utc)
    entry = {
        "id": f"{cl.get('customer_id')}_{now.strftime('%Y%m%dT%H%M%SZ')}",
        "customer_id": cl.get("customer_id"),
        "to": cl.get("email"), "company": cl.get("name"), "contact": cl.get("contact"),
        "from_name": cl.get("checkin_from_name") or "HustleForge",
        "reply_to": cl.get("checkin_reply_to") or OPERATOR_EMAIL,
        "draft_path": draft_rel.replace("\\", "/"),
        "drafted_at": now.isoformat(),
        "send_after": (now + timedelta(hours=HOLD_HOURS)).isoformat(),
        "status": "pending",
    }
    import os
    os.makedirs(os.path.dirname(QUEUE), exist_ok=True)
    with open(QUEUE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main() -> int:
    from datetime import date as _date
    reg = json.load(open(CLIENTS, encoding="utf-8-sig"))
    want = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else None
    force = "--force" in sys.argv
    today = _date.today()
    today_s = today.isoformat()
    drafted = 0
    for cl in reg.get("clients", []):
        if cl.get("status") != "active":
            continue
        if want and cl.get("customer_id") != want:
            continue
        if not _due(cl, today, force):
            print(f"skip {cl.get('name')} — last check-in {cl.get('last_checkin')} (< {CADENCE_DAYS}d)")
            continue
        slug = _slug(cl.get("name", ""))
        draft_rel = f"seo_clients/{slug}/CHECKIN_DRAFT_{today_s}.md"
        out = rf"D:\Hustleforge\Samus\.data\host_artifacts\{draft_rel}".replace("/", "\\")
        try:
            d = generate(cl)
        except Exception as exc:  # noqa: BLE001 — LLM down (no local model / broker): skip, don't crash
            print(f"skip {cl.get('name')} — generation unavailable: {type(exc).__name__}: {str(exc)[:80]}")
            continue
        doc = (f"# CLIENT CHECK-IN DRAFT — {cl.get('name')} — {today_s}\n"
               f"**Status: DRAFT — review before sending (auto-sends after {int(HOLD_HOURS)}h unless stopped).** "
               f"To: {cl.get('email')} · From: {OPERATOR_EMAIL}\n\n"
               f"**Subject:** {d.get('subject','')}\n\n---\n\n{d.get('body','')}\n")
        open(out, "w", encoding="utf-8").write(doc)
        _enqueue(cl, slug, draft_rel)
        cl["last_checkin"] = today_s
        drafted += 1
        print(f"drafted + queued (24h hold) -> {out}")
    # persist last_checkin updates
    json.dump(reg, open(CLIENTS, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    print(f"done: {drafted} check-in(s) drafted+queued")
    return 0


if __name__ == "__main__":
    sys.exit(main())
