#!/usr/bin/env python3
"""samus_activity_digest.py

Read-only "what did Samus do today" production activity digest.

Pulls the real production ledgers out of the running `samus-gateway`
Docker container (samus-data volume at /opt/samus/data/), filters to a
single UTC date, and prints a concise human-readable rollup so an
operator can see the day's actual output in one place instead of
hand-reading six scattered ledgers under /health log spam.

ADDITIVE / READ-ONLY: this script only reads. It shells out to
`docker exec samus-gateway sh -c 'cat ...'` and never writes to the
container, the volume, or the repo.

Usage:
    python samus_activity_digest.py [--date YYYY-MM-DD] [--container NAME]

Pure stdlib. No new dependencies.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
from collections import Counter, defaultdict

CONTAINER_DEFAULT = "samus-gateway"
DATA_ROOT = "/opt/samus/data"

# Ledger paths (relative to DATA_ROOT).
LEDGERS = {
    "ticks": "telemetry/control_ticks.jsonl",
    "seo": "seo/seo_audit.jsonl",
    "crm": "crm/crm_audit.jsonl",
    "voice": "voice/voice_events.jsonl",
    "delivery": "artifacts/_seo_delivery_audit.json",
}


# --------------------------------------------------------------------------- #
# Container I/O
# --------------------------------------------------------------------------- #
def _read_container_file(container: str, rel_path: str) -> str | None:
    """cat a file out of the container. Returns None if missing/unreadable.

    Never raises. A missing file, stopped container, or missing docker
    binary all collapse to None so the caller can render "0" for that
    section.
    """
    remote = f"{DATA_ROOT}/{rel_path}"
    cmd = ["docker", "exec", container, "sh", "-c", f"cat {remote}"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        # File missing, container down, etc. Treat as empty.
        return None
    return proc.stdout


def _iter_jsonl(raw: str | None):
    """Yield parsed JSON objects from JSONL text, skipping blank/bad lines."""
    if not raw:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # Strip a UTF-8 BOM if one leaked into a line.
        if line.startswith("﻿"):
            line = line[1:]
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            yield obj


def _ts_date(obj: dict) -> str | None:
    """Extract the YYYY-MM-DD (UTC) prefix from an object's `ts` field."""
    ts = obj.get("ts")
    if not isinstance(ts, str) or len(ts) < 10:
        return None
    # ts is ISO-8601 UTC, e.g. 2026-07-01T13:00:12Z -> date prefix is safe.
    return ts[:10]


def _num(val) -> float:
    """Coerce to float, treating None/non-numeric as 0.0."""
    try:
        if val is None:
            return 0.0
        return float(val)
    except (ValueError, TypeError):
        return 0.0


# --------------------------------------------------------------------------- #
# Section builders
# --------------------------------------------------------------------------- #
def section_voice(raw: str | None, date: str) -> dict:
    calls = [o for o in _iter_jsonl(raw) if _ts_date(o) == date]
    outcomes = Counter()
    companies = Counter()
    total_cost = 0.0
    for c in calls:
        outcomes[c.get("outcome") or "unknown"] += 1
        company = c.get("company")
        if company:
            companies[company] += 1
        total_cost += _num(c.get("vapi_cost"))
    return {
        "count": len(calls),
        "outcomes": outcomes,
        "companies": companies,
        "total_cost": total_cost,
    }


def section_seo(raw: str | None, date: str) -> dict:
    runs = [
        o
        for o in _iter_jsonl(raw)
        if _ts_date(o) == date and o.get("action") == "audit_site"
    ]
    # Dedup by input_hash (same page content audited again). Fall back to
    # task_id (the URL) when input_hash is absent.
    hash_counts = Counter()
    sites = set()
    for r in runs:
        key = r.get("input_hash") or r.get("task_id") or "(no-key)"
        hash_counts[key] += 1
        url = r.get("task_id")
        if url:
            sites.add(url)
    dups = {k: n for k, n in hash_counts.items() if n > 1}
    return {
        "total_runs": len(runs),
        "unique_sites": len(sites),
        "unique_hashes": len(hash_counts),
        "dups": dups,
    }


def section_crm(raw: str | None, date: str) -> dict:
    rows = [o for o in _iter_jsonl(raw) if _ts_date(o) == date]
    by_action = Counter()
    for r in rows:
        by_action[r.get("action") or "unknown"] += 1
    return {"count": len(rows), "by_action": by_action}


def section_ticks(raw: str | None, date: str) -> dict:
    ticks = [o for o in _iter_jsonl(raw) if _ts_date(o) == date]
    scanned = staked = skipped = failed = 0
    ok_count = 0
    latest_entropy = None
    latest_ts = None
    for t in ticks:
        if t.get("ok"):
            ok_count += 1
        stake = t.get("auto_stake") or {}
        if isinstance(stake, dict):
            scanned += int(_num(stake.get("scanned")))
            staked += int(_num(stake.get("staked")))
            skipped += int(_num(stake.get("skipped")))
            failed += int(_num(stake.get("failed")))
        ts = t.get("ts")
        if ts and (latest_ts is None or ts > latest_ts):
            latest_ts = ts
            entropy = t.get("entropy") or {}
            if isinstance(entropy, dict):
                latest_entropy = entropy.get("entropy_score")
    return {
        "count": len(ticks),
        "ok_count": ok_count,
        "latest_entropy": latest_entropy,
        "latest_ts": latest_ts,
        "scanned": scanned,
        "staked": staked,
        "skipped": skipped,
        "failed": failed,
    }


def section_delivery(raw: str | None) -> dict:
    """Latest SEO delivery result (single JSON object, not date-filtered)."""
    if not raw or not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(obj, dict):
        return {}
    issues = obj.get("issues") or []
    sev = Counter()
    if isinstance(issues, list):
        for i in issues:
            if isinstance(i, dict):
                sev[i.get("severity") or "unknown"] += 1
    return {
        "url": obj.get("url"),
        "seo_score": obj.get("seo_score"),
        "issue_count": len(issues) if isinstance(issues, list) else 0,
        "severities": sev,
        "ts": obj.get("ts"),
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _fmt_counter(counter: Counter, sep=", ") -> str:
    if not counter:
        return "0"
    return sep.join(f"{k}={v}" for k, v in counter.most_common())


def render(date: str, container: str, data: dict) -> str:
    v = data["voice"]
    s = data["seo"]
    c = data["crm"]
    t = data["ticks"]
    d = data["delivery"]

    lines = []
    bar = "=" * 66
    lines.append(bar)
    lines.append(f"  SAMUS ACTIVITY DIGEST  -  {date} (UTC)")
    lines.append(f"  container: {container}")
    lines.append(bar)

    # Voice
    lines.append("")
    lines.append(f"VOICE CALLS: {v['count']}")
    if v["count"]:
        lines.append(f"  outcomes : {_fmt_counter(v['outcomes'])}")
        lines.append(f"  vapi_cost: ${v['total_cost']:.2f}")
        comp = v["companies"]
        if comp:
            top = ", ".join(f"{k} ({n})" for k, n in comp.most_common(10))
            more = "" if len(comp) <= 10 else f" (+{len(comp) - 10} more)"
            lines.append(f"  companies ({len(comp)}): {top}{more}")

    # SEO
    lines.append("")
    lines.append(f"SEO AUDITS: {s['total_runs']} runs")
    if s["total_runs"]:
        redundant = s["total_runs"] - s["unique_sites"]
        lines.append(
            f"  unique sites: {s['unique_sites']}  "
            f"(redundant re-audits: {redundant})"
        )
        if s["dups"]:
            top = sorted(s["dups"].items(), key=lambda kv: kv[1], reverse=True)
            lines.append(f"  duplicate input_hash counts ({len(s['dups'])}):")
            for h, n in top[:8]:
                lines.append(f"    {h[:16]}... x{n}")
            if len(top) > 8:
                lines.append(f"    (+{len(top) - 8} more dup hashes)")
        else:
            lines.append("  duplicate input_hash counts: 0")

    # Latest delivered SEO artifact
    if d:
        lines.append("")
        score = d.get("seo_score")
        score_s = "n/a" if score is None else str(score)
        lines.append(f"LATEST SEO DELIVERY: {d.get('url') or '(unknown)'}")
        lines.append(
            f"  seo_score: {score_s}  issues: {d.get('issue_count', 0)}  "
            f"[{_fmt_counter(d.get('severities', Counter()))}]"
        )

    # CRM
    lines.append("")
    lines.append(f"CRM WRITES: {c['count']}")
    if c["count"]:
        lines.append(f"  by action: {_fmt_counter(c['by_action'])}")

    # Control ticks
    lines.append("")
    lines.append(f"CONTROL TICKS: {t['count']}  (ok={t['ok_count']})")
    if t["count"]:
        ent = t["latest_entropy"]
        ent_s = "n/a" if ent is None else f"{ent}"
        lines.append(f"  latest entropy: {ent_s}  (at {t['latest_ts']})")
        lines.append(
            f"  auto_stake: scanned={t['scanned']} staked={t['staked']} "
            f"skipped={t['skipped']} failed={t['failed']}"
        )

    # Revenue-action one-liner
    lines.append("")
    lines.append(bar)
    lines.append(
        "REVENUE ACTIONS: "
        f"{v['count']} calls made | "
        f"{c['count']} CRM writes | "
        f"{s['total_runs']} SEO audits | "
        f"{t['staked']} stakes ({t['scanned']} scanned)"
    )
    lines.append(bar)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Samus daily production activity digest."
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Target UTC date (YYYY-MM-DD). Default: today (UTC).",
    )
    parser.add_argument(
        "--container",
        default=CONTAINER_DEFAULT,
        help=f"Docker container name (default: {CONTAINER_DEFAULT}).",
    )
    args = parser.parse_args(argv)

    if args.date:
        try:
            _dt.date.fromisoformat(args.date)
        except ValueError:
            print(f"error: invalid --date '{args.date}' (want YYYY-MM-DD)", file=sys.stderr)
            return 2
        date = args.date
    else:
        date = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")

    raw = {name: _read_container_file(args.container, path) for name, path in LEDGERS.items()}

    # Flag if the container appears entirely unreachable (every ledger None).
    if all(v is None for v in raw.values()):
        print(
            f"warning: could not read any ledger from container "
            f"'{args.container}' (is it running?). Showing zeros.",
            file=sys.stderr,
        )

    data = {
        "voice": section_voice(raw["voice"], date),
        "seo": section_seo(raw["seo"], date),
        "crm": section_crm(raw["crm"], date),
        "ticks": section_ticks(raw["ticks"], date),
        "delivery": section_delivery(raw["delivery"]),
    }

    print(render(date, args.container, data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
