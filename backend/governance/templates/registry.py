"""TemplateRegistry — Samus's three pre-approved action templates.

Loads pre_approved_templates.spec.yaml (v0.5.0), filters to
owning_agent=samus, tracks per-template-per-day usage, refuses expired
templates per re_affirmation_horizon_days.

Template hit -> EFH bypass with attribution `template_<id>` recorded in
usage entry. Caller (commercial wrap) reads that attribution into
ValueExchangeRecord.efh_verdict_ref.
"""
from __future__ import annotations

import datetime as _dt
import logging
import uuid
from pathlib import Path
from typing import Any

import yaml

from backend.common.state_paths import state_path

_LOG = logging.getLogger("samus.governance.templates")

_SAMUS_ROOT = Path(__file__).resolve().parents[3]
_HUSTLE_ROOT = _SAMUS_ROOT.parent
# Candidate locations for the signed templates spec. First match wins.
# The worktree may not carry Darwin/; in that case fall back to the
# canonical D:\Hustleforge\Darwin checkout via env override.
_SPEC_CANDIDATES = (
    _HUSTLE_ROOT / "Darwin" / "axioms" / "pre_approved_templates.spec.yaml",
    Path("D:/Hustleforge/Darwin/axioms/pre_approved_templates.spec.yaml"),
)
# Usage counters persist on the writable data volume in-container (the image
# root /opt/samus is read-only). See backend/common/state_paths.py.
_USAGE_DIR = state_path("template_usage")


def _resolve_default_spec_path() -> Path:
    import os
    override = os.getenv("SAMUS_TEMPLATES_SPEC_PATH", "").strip()
    if override:
        return Path(override)
    for p in _SPEC_CANDIDATES:
        if p.is_file():
            return p
    return _SPEC_CANDIDATES[0]

_SAMUS_TEMPLATE_IDS = {
    "samus.standard_warm_prospect_outreach",
    "samus.standard_fulfillment_handoff",
    "samus.standard_subscription_renewal",
}


class TemplateExpiredError(RuntimeError):
    pass


class TemplateBudgetExceededError(RuntimeError):
    pass


class TemplateRegistry:
    """Looks up + tracks pre-approved templates for Samus actions."""

    def __init__(self, *, spec_path: str | Path | None = None) -> None:
        self._spec_path = Path(spec_path) if spec_path else _resolve_default_spec_path()
        self._templates: dict[str, dict] = {}
        self._spec_signed_ts: str | None = None
        _USAGE_DIR.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if not self._spec_path.is_file():
            _LOG.warning("samus.templates.spec_not_found: %s", self._spec_path)
            return
        try:
            doc = yaml.safe_load(self._spec_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            _LOG.error("samus.templates.spec_load_failed: %s", exc)
            return
        if not isinstance(doc, dict):
            return
        self._spec_signed_ts = str(doc.get("signed_ts") or "")
        for t in (doc.get("templates") or []):
            if not isinstance(t, dict):
                continue
            tid = t.get("id")
            if tid in _SAMUS_TEMPLATE_IDS and t.get("owning_agent") == "samus":
                self._templates[tid] = t
        _LOG.info(
            "samus.templates.loaded: count=%d ids=%s",
            len(self._templates), sorted(self._templates),
        )

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------
    def lookup_for_action(
        self, action_class: str, action_payload: dict | None = None,
    ) -> dict | None:
        """Return matching template or None if no template covers the action."""
        payload = action_payload or {}
        for tmpl in self._templates.values():
            shape = tmpl.get("action_shape") or {}
            if shape.get("action_class") != action_class:
                continue
            if not self._shape_matches(shape, payload):
                continue
            if self._is_expired(tmpl):
                _LOG.warning("samus.templates.expired: id=%s", tmpl.get("id"))
                continue
            return tmpl
        return None

    def _shape_matches(self, shape: dict, payload: dict) -> bool:
        """Best-effort shape predicate.

        Each shape field is interpreted as either a literal-equality
        constraint, a pipe-delimited enum, or a min/max bound. Anything
        we can't parse cleanly defaults to a tolerant match; tightening
        is a TODO once per-template tests land.
        """
        for k, v in shape.items():
            if k == "action_class":
                continue
            if k.endswith("_max_usd") or k.endswith("_max"):
                base = k.removesuffix("_max_usd").removesuffix("_max")
                pv = payload.get(base) or payload.get(k)
                if pv is None:
                    continue
                try:
                    if float(pv) > float(v):
                        return False
                except (TypeError, ValueError):
                    pass
                continue
            if k.endswith("_min_usd") or k.endswith("_min"):
                base = k.removesuffix("_min_usd").removesuffix("_min")
                pv = payload.get(base) or payload.get(k)
                if pv is None:
                    continue
                try:
                    if float(pv) < float(v):
                        return False
                except (TypeError, ValueError):
                    pass
                continue
            if k in payload:
                if isinstance(v, str) and "|" in v:
                    allowed = {s.strip() for s in v.split("|")}
                    if str(payload[k]).strip() not in allowed:
                        return False
                else:
                    if payload[k] != v and not isinstance(v, str):
                        return False
        return True

    # ------------------------------------------------------------------
    # Usage tracking
    # ------------------------------------------------------------------
    def record_use(self, template: dict, *, action_ref: str | None = None) -> dict:
        """Persist usage entry; raise on budget breach."""
        tid = template.get("id") or "unknown"
        today = _dt.date.today().isoformat()
        max_per_day = (template.get("usage_budget") or {}).get("max_per_day")
        used_today = self._count_used(tid, today)
        if isinstance(max_per_day, int) and used_today >= max_per_day:
            raise TemplateBudgetExceededError(
                f"template_max_per_day_reached: id={tid} max={max_per_day}"
            )
        entry = {
            "usage_id": str(uuid.uuid4()),
            "template_id": tid,
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "date": today,
            "action_ref": action_ref,
            "efh_attribution": f"template_{tid}",
        }
        target = _USAGE_DIR / f"{entry['usage_id']}.yaml"
        target.write_text(yaml.safe_dump(entry, sort_keys=False), encoding="utf-8")
        return entry

    def _count_used(self, template_id: str, date_iso: str) -> int:
        count = 0
        for f in _USAGE_DIR.glob("*.yaml"):
            try:
                doc = yaml.safe_load(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if (
                isinstance(doc, dict)
                and doc.get("template_id") == template_id
                and doc.get("date") == date_iso
            ):
                count += 1
        return count

    # ------------------------------------------------------------------
    # Expiry
    # ------------------------------------------------------------------
    def _is_expired(self, template: dict) -> bool:
        horizon = template.get("re_affirmation_horizon_days") or 10
        signed_ts_raw = template.get("signed_ts") or self._spec_signed_ts
        if not signed_ts_raw or str(signed_ts_raw).strip() in ("PENDING", ""):
            # Unsigned template — treat as inert (not yet active) per the spec.
            return True
        try:
            signed = _dt.date.fromisoformat(str(signed_ts_raw)[:10])
        except ValueError:
            return True
        return (_dt.date.today() - signed).days > int(horizon)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def loaded_ids(self) -> list[str]:
        return sorted(self._templates)
