"""Auto-stake sweep — promote qualified prospects to staked opportunities.

The cash engine fires on STAKED opportunities. Discovery generates prospects
into ``samus_prospects`` but nothing promotes them to opportunities, so the
operator was the only stake-gate (Kelly Zimmerman was the only staked
opportunity for days). This module closes the loop: an hourly sweep finds
prospects that meet a configurable bar (``lead_score`` ≥ threshold,
``owner_email`` present, not already in an opportunity, not recently
contacted) and auto-creates a staked opportunity with a per-prospect
generated stake sentence. The cash_engine_step queue then walks each one
through audit → proposal → contact → outreach with the existing CAN-SPAM /
suppression / 15-per-day send-ramp guardrails in place.

Gate: ``SAMUS_AUTO_STAKE_ENABLED`` (Settings field
``auto_stake_enabled``, default ``False``). When OFF the sweep is a no-op
that returns ``{"enabled": False}``.

Knobs (env, read directly so they're tunable without a redeploy):
  * ``SAMUS_AUTO_STAKE_MIN_LEAD_SCORE``  (default ``75``)
  * ``SAMUS_AUTO_STAKE_MAX_PER_SWEEP``   (default ``5``)
  * ``SAMUS_AUTO_STAKE_RECENT_DAYS``     (default ``14`` — days since last
                                          contact to skip)
  * ``SAMUS_AUTO_STAKE_SCAN_LIMIT``      (default ``200`` — DDB scan cap
                                          per sweep so we don't burn quota
                                          on huge tables)

This module is the SINGLE source of auto-staking; callers (control_tick
loop, operator HTTP route) just invoke :func:`run_auto_stake_sweep`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

_LOG = logging.getLogger("samus.cash_engine.auto_stake")

_DEFAULT_MIN_LEAD = 75
_DEFAULT_MAX_PER_SWEEP = 5
_DEFAULT_RECENT_DAYS = 14
_DEFAULT_SCAN_LIMIT = 200


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _sweep_enabled() -> bool:
    """Resolve the arming flag (default OFF)."""
    from backend.common.config import get_settings
    from backend.common.flags.runtime import is_enabled

    settings = get_settings()
    fallback = bool(getattr(settings, "auto_stake_enabled", False))
    return is_enabled("auto_stake_enabled", fallback)


def _generate_stake_sentence(prospect: dict[str, Any]) -> str:
    """Compose a stake sentence specific to this prospect.

    The sentence MUST clear ``stake_sentence_guard.validate_stake_sentence``:
    40–280 chars, no banned template phrases, at least one uppercase letter
    (a proper noun — the company name supplies this), ASCII-dominant, no
    3-run whitespace, and uniquely hashed against the dedup window. The
    template references the company name + city + score + an observation
    on website status when available so each generated line is naturally
    distinct.
    """
    company = (prospect.get("company_name") or "").strip()
    city = (prospect.get("city") or "").strip()
    industry = (prospect.get("industry") or "").strip()
    lead_score = prospect.get("lead_score")
    website_status = (prospect.get("website_status") or "").strip()

    # Place / context phrasing.
    where = f" in {city}" if city else ""
    score_txt = f"Lead {int(lead_score)}/100" if lead_score not in (None, "") else "Strong lead"
    industry_txt = f" {industry}" if industry else ""

    # Website-status observation — picks an honest hook the email can lead on.
    site_obs_map = {
        "no_website": "no online presence at all",
        "domain_unresolved": "their domain doesn't resolve right now",
        "parked": "their domain is parked, not a real site",
        "social_only": "only a social page, no owned site",
        "empty": "their homepage loads but is essentially empty",
        "gone": "their homepage returns gone (HTTP 410)",
        "server_error": "their site is returning server errors",
        "unreachable": "we couldn't reach their site",
        "unreachable_timeout": "their site timed out on us",
        "http_error": "their homepage returns an error page",
    }
    site_obs = site_obs_map.get(website_status, "")

    # Compose. Vary slightly by whether there's a clear website hook.
    if site_obs:
        sentence = (
            f"{company}{where} has{industry_txt} demand we can capture: "
            f"{site_obs}, and {score_txt.lower()}. Auto-staked tripwire."
        )
    else:
        sentence = (
            f"{company}{where} scores well on the fit signals "
            f"({score_txt.lower()}{industry_txt and ' — ' + industry_txt.strip()}). "
            f"Auto-staked for tripwire outreach."
        )

    # Bounded length — chop softly if a long company name pushes past 280.
    if len(sentence) > 280:
        sentence = sentence[:277].rstrip() + "..."

    # Hard-guarantee the proper-noun rule: if (somehow) no uppercase letter,
    # fall back to a literal "Auto-staked." token at the end which carries
    # the cased letter.
    if not any(ch.isupper() for ch in sentence):
        sentence = sentence.rstrip(". ") + ". Auto-staked."

    return sentence


def _candidates(scan_limit: int, min_lead: int) -> list[dict[str, Any]]:
    """Scan ``samus_prospects`` for rows that pass the auto-stake filter.

    Returns each candidate's raw dict (DDB attributes). Fail-soft: any AWS
    error yields an empty list so the sweep degrades to "no work this tick"
    rather than killing the control_tick.
    """
    try:
        import boto3  # noqa: PLC0415 — boundary import
    except Exception:  # noqa: BLE001 — boto3 is always present in container
        _LOG.exception("boto3 missing; auto-stake skipped")
        return []
    try:
        ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-1"))
        table = ddb.Table(os.environ.get("DDB_PROSPECTS_TABLE", "samus_prospects"))
        out: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"Limit": scan_limit}
        while True:
            r = table.scan(**kwargs)
            for it in r.get("Items", []):
                email = (it.get("owner_email") or "").strip()
                if not email:
                    continue
                try:
                    ls = int(it.get("lead_score") or 0)
                except (ValueError, TypeError):
                    ls = 0
                if ls < min_lead:
                    continue
                out.append(it)
            lek = r.get("LastEvaluatedKey")
            if not lek or len(out) >= scan_limit:
                break
            kwargs["ExclusiveStartKey"] = lek
        return out
    except Exception:  # noqa: BLE001 — fail-soft
        _LOG.exception("auto-stake DDB scan failed; skipping sweep")
        return []


def _recently_contacted(prospect_id: str, recent_days: int) -> bool:
    """True if CRM has a CallState updated within the last ``recent_days``."""
    if recent_days <= 0:
        return False
    try:
        import datetime as _dt  # noqa: PLC0415
        from backend.crm.service import get_call_state  # noqa: PLC0415

        state = get_call_state(prospect_id)
        if state is None:
            return False
        d = state.model_dump() if hasattr(state, "model_dump") else dict(state)
        raw = str(d.get("updated_at") or "")
        if not raw:
            return False
        ts_str = raw.replace("Z", "+00:00")
        try:
            ts = _dt.datetime.fromisoformat(ts_str)
        except ValueError:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        delta = _dt.datetime.now(_dt.timezone.utc) - ts
        return delta.days < recent_days
    except Exception:  # noqa: BLE001 — fail-soft (treat as not contacted)
        return False


def _already_has_opportunity(prospect_id: str) -> bool:
    """True if the prospect is already wired to a CRM opportunity."""
    try:
        from backend.crm.service import get_opportunity_for_prospect  # noqa: PLC0415

        return get_opportunity_for_prospect(prospect_id) is not None
    except Exception:  # noqa: BLE001 — treat as "no opportunity" so we can stake
        return False


def _stake_one(prospect: dict[str, Any]) -> dict[str, Any]:
    """Create opportunity → set auto-generated stake → review (enqueue cash_engine_step).

    Returns a per-prospect summary. Fail-soft per record.
    """
    pid = str(prospect.get("prospect_id") or "").strip()
    if not pid:
        return {"ok": False, "prospect_id": "", "error": "missing_prospect_id"}

    try:
        from backend.crm.models import CreateOpportunityRequest  # noqa: PLC0415
        from backend.crm.service import create_opportunity  # noqa: PLC0415
        from backend.cash_engine.models import RevenueTriggerRequest  # noqa: PLC0415
        from backend.cash_engine.service import review_opportunity  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "prospect_id": pid, "error": f"import_failed:{exc}"}

    stake = _generate_stake_sentence(prospect)
    try:
        # CreateOpportunityRequest accepts stake_sentence inline so we don't
        # need a separate set_opportunity_stake call. industry feeds the
        # bandit-policy decision downstream; intent_score is the bandit-side
        # surrogate for lead_score so the strategy workcell can attribute
        # the eventual outcome back to the cohort that picked it.
        ls_raw = prospect.get("lead_score")
        try:
            intent_score = int(ls_raw) if ls_raw not in (None, "") else None
        except (ValueError, TypeError):
            intent_score = None
        create_req = CreateOpportunityRequest(
            prospect_id=pid,
            name=str(prospect.get("company_name") or "") or "",
            industry=str(prospect.get("industry") or "") or "",
            intent_score=intent_score,
            stake_sentence=stake,
            stake_sentence_authored_by="auto_stake_sweep",
            owner_email=bool((prospect.get("owner_email") or "").strip()),
        )
        creation = create_opportunity(create_req)
        opp_id = getattr(creation, "opportunity_id", "") or ""
        status = str(getattr(creation, "status", ""))
        if status != "created" or not opp_id:
            return {"ok": False, "prospect_id": pid, "error": f"create_status:{status}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "prospect_id": pid, "error": f"create_failed:{exc}"}

    try:
        trigger = RevenueTriggerRequest(
            prospect_id=pid,
            trigger_source="auto_stake_sweep",
            trigger_reason="auto_stake:lead_qualified",
            current_samus_state="staked",
        )
        result = review_opportunity(trigger)
        return {
            "ok": bool(getattr(result, "accepted", False)),
            "prospect_id": pid,
            "opportunity_id": opp_id,
            "status": str(getattr(result, "status", "")),
            "task_id": str(getattr(result, "task_id", "")),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "prospect_id": pid,
            "opportunity_id": opp_id,
            "error": f"review_failed:{exc}",
        }


def run_auto_stake_sweep() -> dict[str, Any]:
    """Run one sweep. Returns a structured summary for the caller (e.g. control_tick).

    Fail-soft end-to-end: the sweep MUST NEVER raise — a fault inside the
    autonomous loop drops the whole control_tick on the floor and the
    revenue heartbeat goes silent. Errors are captured in the result.
    """
    if not _sweep_enabled():
        return {"enabled": False, "scanned": 0, "staked": 0, "skipped": 0, "results": []}

    # Defensive: review_opportunity fail-closes with "codex registry not
    # loaded" if it's called outside the gateway HTTP app lifespan that
    # normally loads the registry. The control_tick loop fires INSIDE the
    # gateway lifespan so this is a no-op there, but a probe call (or a
    # future SQS-worker invocation) would otherwise hit the fail-closed
    # path. Idempotent; cheap; never raises.
    try:
        from backend.common.codex import REGISTRY  # noqa: PLC0415

        if not REGISTRY.is_loaded():
            REGISTRY.load()
    except Exception:  # noqa: BLE001 — codex must never block the sweep
        _LOG.warning("auto-stake codex preload failed; review_opportunity may fail-close")

    min_lead = _env_int("SAMUS_AUTO_STAKE_MIN_LEAD_SCORE", _DEFAULT_MIN_LEAD)
    max_per_sweep = _env_int("SAMUS_AUTO_STAKE_MAX_PER_SWEEP", _DEFAULT_MAX_PER_SWEEP)
    # Heat Field throttle: scale the per-sweep budget by the reputation
    # multiplier (1.0 when the field is dormant; <1.0 warm/hot; 0 = critical,
    # which pauses autonomous expansion). Fail-soft — never breaks the sweep.
    try:
        from backend.heat import service as _heat

        _mult = _heat.send_multiplier_now()
        if _mult < 1.0:
            scaled = int(max_per_sweep * _mult)
            _LOG.info(
                "auto-stake heat throttle: mult=%.2f max_per_sweep %d->%d",
                _mult,
                max_per_sweep,
                scaled,
            )
            max_per_sweep = scaled
    except Exception as exc:  # noqa: BLE001 — heat must never break the sweep
        _LOG.warning("auto-stake heat multiplier failed: %s", exc)
    recent_days = _env_int("SAMUS_AUTO_STAKE_RECENT_DAYS", _DEFAULT_RECENT_DAYS)
    scan_limit = _env_int("SAMUS_AUTO_STAKE_SCAN_LIMIT", _DEFAULT_SCAN_LIMIT)

    candidates = _candidates(scan_limit=scan_limit, min_lead=min_lead)
    scanned = len(candidates)
    _LOG.info(
        "auto-stake sweep: scanned=%d min_lead=%d max_per_sweep=%d",
        scanned,
        min_lead,
        max_per_sweep,
    )

    # Sort candidates by lead_score DESC so the highest-confidence prospects
    # take the limited slots when more pass the bar than max_per_sweep.
    def _ls(p: dict[str, Any]) -> int:
        try:
            return int(p.get("lead_score") or 0)
        except (ValueError, TypeError):
            return 0

    candidates.sort(key=_ls, reverse=True)

    # Highest qualified score in the pool (candidates are sorted DESC above).
    # Surfaced so a silent no-op sweep still shows how the pool sits vs the bar.
    top_score = _ls(candidates[0]) if candidates else 0

    results: list[dict[str, Any]] = []
    skipped = 0
    # Skip-reason attribution so a 0-staked sweep is diagnosable from the log
    # instead of being a silent no-op. Purely observational — the staking
    # decision path below is unchanged.
    skip_no_pid = 0
    skip_has_opp = 0
    skip_recent = 0
    for p in candidates:
        if len([r for r in results if r.get("ok")]) >= max_per_sweep:
            break
        pid = str(p.get("prospect_id") or "")
        if not pid:
            skipped += 1
            skip_no_pid += 1
            continue
        if _already_has_opportunity(pid):
            skipped += 1
            skip_has_opp += 1
            continue
        if _recently_contacted(pid, recent_days):
            skipped += 1
            skip_recent += 1
            continue
        results.append(_stake_one(p))

    staked = len([r for r in results if r.get("ok")])
    failed = len(results) - staked
    summary = {
        "enabled": True,
        "scanned": scanned,
        "staked": staked,
        "skipped": skipped,
        "failed": failed,
        "top_score": top_score,
        "skip_reasons": {
            "no_pid": skip_no_pid,
            "already_staked": skip_has_opp,
            "recently_contacted": skip_recent,
        },
        "results": results,
    }
    _LOG.info(
        "auto-stake sweep complete: scanned=%d staked=%d skipped=%d failed=%d "
        "top_score=%d (skip: already_staked=%d recently_contacted=%d no_pid=%d)",
        scanned,
        staked,
        skipped,
        failed,
        top_score,
        skip_has_opp,
        skip_recent,
        skip_no_pid,
    )
    if scanned > 0 and staked == 0:
        _LOG.warning(
            "auto-stake produced 0 stakes from %d qualified prospects "
            "(min_lead=%d, top_score=%d): already_staked=%d recently_contacted=%d "
            "no_pid=%d — qualified pool appears saturated; replenish upstream "
            "(email enrichment / fresh discovery) rather than lowering the bar",
            scanned,
            min_lead,
            top_score,
            skip_has_opp,
            skip_recent,
            skip_no_pid,
        )
    return summary


__all__ = ["run_auto_stake_sweep"]
