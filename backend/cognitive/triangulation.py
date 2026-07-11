"""Three-way triangulation of the EOD production picture (operator design 2026-07-02).

Three INDEPENDENT analyses of the same day:
  A. Samus's own local EOD review   (self-report)
  B. OpenAI strategic advisor       (external, one compressed call)
  C. Local adversarial auditor      (skeptical local model, this module) — the leg
     that was missing.

Convergence: findings that TWO OR MORE reports agree on are high-confidence and
lead the next-day GAMEPLAN; single-source findings are flagged divergent (for
review). Cost: the auditor (C) and the synthesis both run on the LOCAL model
(zero external cost); OpenAI is only the single advisor call already in the cycle.
Fail-soft everywhere — if the local model is unreachable, a DETERMINISTIC
keyword-overlap convergence runs instead, so the gameplan is never blocked.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable, Optional

_LOG = logging.getLogger("samus.cognitive.triangulation")

LlmFn = Callable[[str, str], str]

# H1 — durable heuristic installed 2026-07-08 after an adversarial-sampling
# pathology observed on 2026-07-07. Any leg-C / auditor / synthesis reasoning
# that reviews claims from another report is vulnerable to concluding from a
# TRUNCATED view (top of a log file, top of a sorted list) instead of the raw
# underlying ledger. Injected verbatim into every reasoning system prompt in
# this module so the heuristic is greppable and reusable if other reasoning
# surfaces are added later.
_H1_ADVERSARIAL_SAMPLING_HEURISTIC = (
    "When you review claims from another report, do NOT conclude from a "
    "sampled or truncated view (e.g. the first N lines of a log file, the "
    "top of a sorted list). Cross-check each substantive claim against the "
    "RAW underlying ledger or full-file evidence. A 2026-07-07 pathology: "
    "an auditor read only the top of each dial_run file (cooled hot-tier, "
    "all skipped_cooldown) and falsely concluded 'voice channel connected "
    "with nobody' — while warm-tier dials with real call_ids sat further "
    "down the same files."
)

_AUDITOR_SYSTEM = (
    "You are a SKEPTICAL, adversarial operations auditor reviewing an autonomous "
    "revenue agent's daily production. Find what is NOT working: missed objectives, "
    "hidden risks, bottlenecks, anomalies, and places where the day's story reads "
    "too optimistically. Be specific and critical — no praise, no filler. Report "
    "ONLY significant findings (real accomplishments, critical risks, bottlenecks, "
    "high-value anomalies). Terse bullet points.\n\n"
    f"HEURISTIC H1 (adversarial sampling): {_H1_ADVERSARIAL_SAMPLING_HEURISTIC}"
)

_CLAUDE_SYSTEM = (
    "You are an independent senior operations analyst giving a daily third-party "
    "read on an autonomous revenue agent's production. You are NOT the agent and "
    "NOT its strategic advisor — bring a genuinely INDEPENDENT, rigorous, "
    "evidence-based perspective. Identify what materially worked, what is at real "
    "risk, what the agent AND its advisor are likely MISSING, and the "
    "highest-leverage next actions. Be specific and honest; do not echo the "
    "agent's own framing. Report ONLY significant findings (accomplishments, "
    "critical risks, bottlenecks, high-value anomalies, autonomy-advancement "
    "opportunities). Terse bullet points.\n\n"
    f"HEURISTIC H1 (adversarial sampling): {_H1_ADVERSARIAL_SAMPLING_HEURISTIC}"
)

_SYNTH_SYSTEM = (
    "You are the intelligence synthesizer for an autonomous revenue agent. You are "
    "given THREE independent analyses of today's production: (A) the agent's own "
    "review, (B) an external strategic advisor, (C) a skeptical local auditor. "
    "Identify the key findings in each. Flag findings TWO OR MORE agree on as "
    "high-confidence. Tier every finding: 1=critical (production/stability/"
    "governance/strategic), 2=high-value (opportunity/optimization/risk), "
    "3=informational. Then produce a concrete next-day GAMEPLAN that leads with the "
    "corroborated findings. Output STRICT JSON only, no prose:\n"
    '{"corroborated":[{"finding":"","tier":1,"sources":["A","C"]}],'
    '"divergent":[{"finding":"","source":"B"}],'
    '"gameplan":{"tier1":[""],"tier2":[""],"tier3":[""]}}\n\n'
    f"HEURISTIC H1 (adversarial sampling): {_H1_ADVERSARIAL_SAMPLING_HEURISTIC}"
)


def _lmstudio_url(base: str) -> str:
    """OpenAI-compatible chat-completions URL for LM Studio. Handles a base that
    already carries /v1 (SAMUS_LM_STUDIO_URL is commonly '.../v1') without
    doubling it into '/v1/v1/chat/completions'."""
    base = (base or "").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _local_llm(system: str, prompt: str) -> str:
    """One completion on the LOCAL LM Studio model (OpenAI-compatible), independent
    of the OPENAI_API_KEY routing so it is always zero external cost. Defensive:
    raises on any failure so the caller can fall back to deterministic."""
    import httpx

    url = _lmstudio_url(os.environ.get("SAMUS_LM_STUDIO_URL", "http://localhost:1234"))
    payload = {
        "model": os.environ.get("SAMUS_LM_STUDIO_MODEL", "local"),
        "messages": [
            {"role": "system", "content": system},
            # /no_think: disable the local model's thinking channel (gemma quirk).
            {"role": "user", "content": f"{prompt}\n/no_think"},
        ],
        "temperature": 0.3,
        "max_tokens": 1200,
    }
    resp = httpx.post(url, json=payload, timeout=90.0)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


_CLAUDE_KEY_ENVS = ("ANTHROPIC_API_KEY", "SAMUS_CLAUDE_API_KEY", "SAMUS_ANTHROPIC_API_KEY")


def _claude_key() -> str:
    """The Anthropic API key, seeded into the container env from DPAPI by the
    launcher (Start-SamusStack). "" if not present."""
    for env in _CLAUDE_KEY_ENVS:
        v = os.environ.get(env)
        if v:
            return v
    return ""


def claude_available() -> bool:
    return bool(_claude_key())


def _claude_llm(system: str, prompt: str) -> str:
    """One completion on Claude via the Anthropic Messages API, keyed from DPAPI.
    Raises on any failure so the caller can fall back to the local auditor."""
    import httpx

    key = _claude_key()
    if not key:
        raise RuntimeError("no Anthropic API key in env")
    model = os.environ.get("SAMUS_CLAUDE_MODEL", "claude-sonnet-5")
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": model, "max_tokens": 1200, "system": system,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ).strip()


def read_framework_report(day_iso: str) -> str:
    """Leg C (on-demand, PREFERRED) — the Framework Agent's (a convened Claude
    session) independent report, written to
    ``<artifact_root>/cognition/framework_report_<date>.md`` by the /samus-eod
    skill. Preferred over the API leg: a full convened session is a richer read
    than one API call, and it's free. "" if none written today."""
    try:
        from backend.common import storage
        p = storage.root() / "cognition" / f"framework_report_{day_iso}.md"
        return p.read_text(encoding="utf-8").strip() if p.is_file() else ""
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("framework report read failed: %s", exc)
        return ""


def build_claude_report(day_summary: str, *, llm_fn: Optional[LlmFn] = None) -> str:
    """Leg C — CLAUDE's independent read via the Anthropic API (DPAPI-keyed). "" if
    the key is absent or the call fails (caller then uses the local-auditor leg)."""
    fn = llm_fn or _claude_llm
    try:
        return fn(_CLAUDE_SYSTEM, day_summary).strip()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("claude leg unavailable: %s", exc)
        return ""


def build_auditor_report(
    day_summary: str, *, llm_fn: Optional[LlmFn] = None,
) -> str:
    """Leg C FALLBACK — the skeptical LOCAL auditor's read, used only when the
    Claude leg is unavailable. Returns "" if the local model is also down
    (triangulation then runs two-way)."""
    fn = llm_fn or _local_llm
    try:
        return fn(_AUDITOR_SYSTEM, day_summary).strip()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("local auditor unavailable: %s", exc)
        return ""


# --- convergence -------------------------------------------------------------

_STOP = set("the a an and or of to for with in on at is are was were be this that "
            "it its as by from we our you your samus day today production report".split())
_RISK_WORDS = ("risk", "fail", "block", "bottleneck", "governance", "outage", "down",
               "breach", "over budget", "runway", "compliance", "stalled", "critical")

# --- finding vs. boilerplate -------------------------------------------------
# The three EOD reports embed a lot of NON-actionable boilerplate that a naive
# line-splitter would ingest as "findings": labeled stats ("Open (actionable):
# 69", "Mean success score: 1.0"), run metadata ("Date: … | Generated: …"),
# embedded dicts ("By status: {'proposed': 83, …}"), numeric one-liners
# ("Runway 0.8 days.", "Target $40,000 in 5 days.") and — when a report echoes
# the raw guidance JSON — literal object fragments ('"recommendation": "…"').
# None of these are findings; routing them into the guidance ledger polluted it
# with junk proposed recs (operator report 2026-07-07). A finding must read like
# a sentence: enough content words and none of the stat/JSON shapes below.
_JSON_KEY_FRAG = re.compile(r"""^["'][\w ./-]+["']\s*:""")   # '"recommendation": …'
_LABEL_VALUE = re.compile(r"^[^:]{1,40}:\s*(\S.*)$")          # 'Label: value'
_CONTENT_WORD = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")        # a word, >=3 letters


def _looks_boilerplate(s: str) -> bool:
    """True for stat / metadata / dict / JSON-fragment lines (see _findings)."""
    # A raw JSON object fragment: a quoted key immediately followed by a colon.
    if _JSON_KEY_FRAG.match(s):
        return True
    # An embedded dict / JSON literal: "By status: {'proposed': 83, …}".
    if re.search(r"[{}]", s) and re.search(r""":\s*[\d\[{"']""", s):
        return True
    m = _LABEL_VALUE.match(s)
    if m:
        rest = m.group(1)
        # A real finding after a colon reads as prose (mostly letters); a stat's
        # value is dominated by digits / dates / symbols. Pipe-delimited
        # metadata ("Date: … | Generated: …") is always boilerplate.
        letters = sum(c.isalpha() for c in rest)
        if letters <= max(3, len(rest) // 3):
            return True
        if "|" in s and re.search(r"\d", s):
            return True
    return False


def _is_finding(s: str) -> bool:
    """A candidate line is a real finding only when it reads like a sentence —
    not a stat/metadata/JSON-fragment line, and not a bare numeric one-liner."""
    if _looks_boilerplate(s):
        return False
    words = _CONTENT_WORD.findall(s)
    # A numeric one-liner ("Runway 0.8 days.", "Target $40,000 in 5 days.") is a
    # stat, not a finding — require more prose when a number is present.
    if re.search(r"\d", s) and len(words) < 3:
        return False
    return len(words) >= 2


def _findings(text: str) -> list[str]:
    """Split a report into candidate finding lines (bullets/sentences), keeping
    only sentence-like lines. Stat / metadata / dict / JSON-fragment lines are
    dropped so they are never routed into the guidance ledger as recs."""
    out: list[str] = []
    for raw in re.split(r"[\n\r]+|(?<=[.!?])\s+", text or ""):
        s = re.sub(r"^[\s\-*•\d.)]+", "", raw).strip()
        if len(s) >= 12 and _is_finding(s):
            out.append(s[:200])
    return out[:40]


def _keyset(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in _STOP and len(w) > 3}


def _tier_of(finding: str) -> int:
    low = finding.lower()
    if any(w in low for w in _RISK_WORDS):
        return 1
    return 2


def _deterministic_convergence(reports: dict[str, str]) -> dict[str, Any]:
    """Keyword-overlap convergence when the local model is unavailable. A finding
    is corroborated if another report shares >=2 significant keywords with it."""
    tagged = [(src, f, _keyset(f)) for src, txt in reports.items() if txt for f in _findings(txt)]
    corroborated: list[dict[str, Any]] = []
    divergent: list[dict[str, Any]] = []
    used: set[int] = set()
    for i, (src_i, f_i, k_i) in enumerate(tagged):
        if i in used or len(k_i) < 2:
            continue
        sources = {src_i}
        for j, (src_j, f_j, k_j) in enumerate(tagged):
            if j <= i or src_j in sources:
                continue
            if len(k_i & k_j) >= 2:
                sources.add(src_j)
                used.add(j)
        if len(sources) >= 2:
            corroborated.append({"finding": f_i, "tier": _tier_of(f_i),
                                 "sources": sorted(sources)})
            used.add(i)
    for i, (src_i, f_i, _k) in enumerate(tagged):
        if i not in used and _tier_of(f_i) == 1:  # keep single-source CRITICAL only
            divergent.append({"finding": f_i, "source": src_i})
    corroborated.sort(key=lambda c: c["tier"])
    gameplan = {
        "tier1": [c["finding"] for c in corroborated if c["tier"] == 1][:5],
        "tier2": [c["finding"] for c in corroborated if c["tier"] == 2][:5],
        "tier3": [],
    }
    return {"corroborated": corroborated[:15], "divergent": divergent[:8],
            "gameplan": gameplan, "method": "deterministic"}


def _extract_json_object(raw: str) -> str:
    """Isolate the JSON object from a raw LLM completion.

    Handles the two shapes small local models actually emit around the object:
      * a fenced ```json … ``` block (the fence is stripped);
      * leading/trailing prose around a single top-level ``{ … }``.

    Returns the substring from the first ``{`` through its balanced closing
    ``}`` (brace-counting that respects string literals + escapes), which is
    tighter than a greedy ``\\{.*\\}`` regex — that over-captures when the
    prose after the object also contains a brace. Falls back to the whole
    string if no balanced object is found (the caller's parse then decides)."""
    if not raw:
        return ""
    # Strip a ```json / ``` code fence if the model wrapped the object in one.
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    start = raw.find("{")
    if start < 0:
        return raw
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[start : i + 1]
    return raw[start:]


def _loads_lenient(blob: str) -> dict[str, Any]:
    """Parse ``blob`` as JSON, tolerating the malformations small local models
    emit. Raises ``ValueError`` only when every repair pass fails.

    Passes, cheapest first:
      1. strict=False — accepts literal control characters (raw newlines / tabs)
         inside string values, the ``Invalid control character at …`` failure.
      2. trailing-comma strip — removes ``,`` immediately before ``}`` or ``]``
         (a common ``Expecting value`` cause), then re-parses lenient.
      3. control-character scrub — replaces raw control chars OUTSIDE strings
         with a space and re-parses, catching stray characters the object
         picked up between tokens.
    """
    # Pass 1: literal control chars inside strings are the usual local-model sin.
    try:
        return json.loads(blob, strict=False)
    except ValueError:
        pass
    # Pass 2: strip trailing commas (``… ],`` / ``… },`` before a close).
    repaired = re.sub(r",\s*([}\]])", r"\1", blob)
    try:
        return json.loads(repaired, strict=False)
    except ValueError:
        pass
    # Pass 3: scrub disallowed control chars, then retry the trailing-comma strip.
    scrubbed = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", repaired)
    return json.loads(scrubbed, strict=False)


def triangulate(
    reports: dict[str, str],
    *,
    llm_fn: Optional[LlmFn] = None,
) -> dict[str, Any]:
    """Converge {A: samus, B: openai, C: auditor} into corroborated findings +
    a tiered next-day gameplan. LLM-primary (local, free), deterministic fallback.
    Never raises. Reports with empty text are simply absent from the convergence."""
    present = {k: v for k, v in reports.items() if v and v.strip()}
    if len(present) < 2:
        # Nothing to triangulate — pass the lone report through as tier-2 gameplan.
        lone = next(iter(present.values()), "")
        return {"corroborated": [], "divergent": [], "method": "insufficient",
                "gameplan": {"tier1": [], "tier2": _findings(lone)[:5], "tier3": []}}

    fn = llm_fn or _local_llm
    prompt = "\n\n".join(
        f"=== REPORT {src} ===\n{txt}" for src, txt in present.items())
    raw = ""
    try:
        raw = fn(_SYNTH_SYSTEM, prompt)
        data = _loads_lenient(_extract_json_object(raw))
        if not isinstance(data, dict):
            raise ValueError(f"synthesis JSON was {type(data).__name__}, not an object")
        data.setdefault("corroborated", [])
        data.setdefault("divergent", [])
        data.setdefault("gameplan", {"tier1": [], "tier2": [], "tier3": []})
        # The synthesizer sometimes lifts stat/metadata lines out of the reports
        # verbatim as "findings"; drop those before they reach the guidance
        # ledger. Only well-formed dicts with a sentence-like finding survive.
        data["corroborated"] = [
            c for c in data["corroborated"]
            if isinstance(c, dict) and _is_finding(str(c.get("finding", "")))
        ]
        data["divergent"] = [
            d for d in data["divergent"]
            if isinstance(d, dict) and _is_finding(str(d.get("finding", "")))
        ]
        data["method"] = "llm"
        return data
    except Exception as exc:  # noqa: BLE001
        # Log the raw payload (truncated) so a parse regression is debuggable
        # instead of silently degrading — the deterministic matcher's near-
        # useless output was previously the only symptom.
        _LOG.warning(
            "triangulation LLM failed (%s) -> deterministic. raw payload (first 800 chars): %r",
            exc, (raw or "")[:800],
        )
        return _deterministic_convergence(present)
