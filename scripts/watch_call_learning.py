"""Watch-CallLearning — Framework-Agent supervisor for Samus's outbound calls.

For each recent call it answers the two questions the operator cares about:
  1. CONTENT + OUTCOME — what happened on the call (tier, outcome, what landed
     / flopped, reward).
  2. DID SAMUS LEARN — was the call stamped with a ``variant_arm_id`` and did
     its reward flow into the bandit so the NEXT call's script is adjusted?
     A completed call that does NOT feed the bandit is a flagged failure — the
     whole point of supervising rather than just watching.

Read-only. Reads the voice analyses the transcript-analyzer writes + the
gateway HUD; never places or mutates a call. Run ad hoc or on a loop:

    python scripts/watch_call_learning.py            # last 10 analyzed calls
    python scripts/watch_call_learning.py 25         # last 25
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.request

try:  # Windows consoles default to cp1252 — force UTF-8 so glyphs don't crash.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

GATEWAY = "http://127.0.0.1:8100"
VOICE = "samus-voice"
ANALYSES = "/opt/samus/data/artifacts/voice/analyses"


def _gw(path: str):
    try:
        with urllib.request.urlopen(GATEWAY + path, timeout=8) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        return {"_error": f"{type(e).__name__}"}


def _dexec(cmd: str) -> str:
    try:
        out = subprocess.run(
            ["docker", "exec", VOICE, "sh", "-c", cmd],
            capture_output=True, text=True, timeout=25,
        )
        return out.stdout
    except Exception as e:  # noqa: BLE001
        return f"__ERR__ {type(e).__name__}"


def _recent_analyses(n: int) -> list[dict]:
    listing = _dexec(f"ls -t {ANALYSES}/*.json 2>/dev/null | head -{n}")
    files = [f for f in listing.splitlines() if f.endswith(".json")]
    out: list[dict] = []
    for f in files:
        raw = _dexec(f"cat '{f}' 2>/dev/null")
        try:
            d = json.loads(raw)
            d["_file"] = f.rsplit("/", 1)[-1]
            out.append(d)
        except Exception:  # noqa: BLE001
            continue
    return out


def _first(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, "", [], {}):
            return d[k]
    return default


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    bar = "=" * 78
    print(bar)
    print("  SAMUS CALL-LEARNING MONITOR")
    print(bar)

    stats = _gw("/api/crm/stats")
    print("  HUD  calls_today={} connect_rate={} booked_today={} emails_today={}".format(
        stats.get("calls_today", "?"), stats.get("connect_rate", "?"),
        stats.get("booked_today", "?"), stats.get("emails_today", "?")))

    analyses = _recent_analyses(n)
    if not analyses:
        print("\n  No analyzed calls yet. The monitor activates on the first call.")
        print("  (autonomous dial is armed but consent-fenced; trigger a dial on a")
        print("   warm/consented prospect to see the loop run end-to-end.)")
        return 0

    learned = decoupled = 0
    print(f"\n  {len(analyses)} recent analyzed call(s):\n")
    for a in analyses:
        call_id = _first(a, "call_id", "id", "vapi_call_id", default=a.get("_file", "?"))
        outcome = _first(a, "outcome", "call_outcome", "disposition", default="?")
        reward = _first(a, "reward", "reward_value", "score", default="?")
        tier = _first(a, "prospect_tier", "tier", "tier_correction", default="?")
        arm = _first(a, "variant_arm_id", "arm_id", "assistant_config_version")
        landed = _first(a, "talking_points_landed", "landed", default=[])
        flopped = _first(a, "talking_points_flopped", "flopped", default=[])
        fed = arm is not None
        learned += int(fed)
        decoupled += int(not fed)
        flag = "LEARN✓" if fed else "LEARN✗ (no arm_id — reward can't reach the bandit)"
        print(f"  • call {str(call_id)[:20]:<20} outcome={outcome:<14} reward={reward} tier={tier}")
        if landed:
            print(f"      landed:  {', '.join(map(str, landed))[:90]}")
        if flopped:
            print(f"      flopped: {', '.join(map(str, flopped))[:90]}")
        print(f"      {flag}")

    print(f"\n  LEARNING: {learned}/{len(analyses)} calls fed the bandit; {decoupled} decoupled.")
    if decoupled:
        print("  ⚠ decoupled calls exist — reward is NOT reaching the script bandit;")
        print("    Samus is talking but not adjusting. Investigate arm_stamp wiring.")
    else:
        print("  ✓ every analyzed call is feeding the learning loop.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
