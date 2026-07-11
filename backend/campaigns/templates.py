"""Load + validate campaign templates and client instances from YAML/dicts.

The declarative shape (see ``templates/school_enrollment_campaign.yaml``):

    campaign_template:
      template_id: school_enrollment_campaign
      vertical: education
      required_inputs: [client_id, school_name, ...]
      kpis: [page_views, cta_clicks, ...]        # bare keys or full mappings
      nodes: [{id, type, target_workcell, capability, approval_required}, ...]
      edges: [{from, to, condition?}, ...]

A client instance (``clients/<client>/campaign.yaml``):

    campaign_instance:
      campaign_id: sample_school_enrollment_2026
      client_id: sample_school
      template_id: school_enrollment_campaign
      inputs: {school_name: ..., enrollment_deadline: ...}

Both the ``campaign_template:`` / ``campaign_instance:`` wrapper key and a bare
top-level mapping are accepted, so a fixture can hand either shape. Validation
delegates to the Pydantic models plus a graph integrity pass.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import (
    CampaignInstance,
    CampaignKPI,
    CampaignTemplate,
)
from .registry import assert_routable, discover_template_files


class TemplateError(ValueError):
    """Raised when a template/instance file is malformed or fails validation."""


def _load_yaml(source: str | Path) -> dict[str, Any]:
    path = Path(source)
    if not path.exists():
        raise TemplateError(f"template file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TemplateError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise TemplateError(f"top-level YAML in {path} must be a mapping")
    return data


def _unwrap(data: dict[str, Any], *wrappers: str) -> dict[str, Any]:
    """Return the inner mapping, tolerating an optional wrapper key."""
    for key in wrappers:
        inner = data.get(key)
        if isinstance(inner, dict):
            return inner
    return data


def _normalize_kpis(raw: Any) -> list[dict[str, Any]]:
    """Accept ``["page_views", ...]`` or ``[{key: ..., target: ...}]``."""
    out: list[dict[str, Any]] = []
    for item in raw or []:
        if isinstance(item, str):
            out.append({"key": item, "label": item.replace("_", " ").title()})
        elif isinstance(item, dict) and item.get("key"):
            out.append(item)
        else:
            raise TemplateError(f"invalid KPI entry: {item!r}")
    return out


def parse_template(data: dict[str, Any]) -> CampaignTemplate:
    """Validate a template mapping into a :class:`CampaignTemplate`."""
    inner = _unwrap(data, "campaign_template", "template")
    payload = dict(inner)
    if "kpis" in payload:
        payload["kpis"] = _normalize_kpis(payload["kpis"])
    try:
        template = CampaignTemplate.model_validate(payload)
    except Exception as exc:  # pydantic ValidationError -> uniform error
        raise TemplateError(f"template validation failed: {exc}") from exc
    validate_graph_integrity(template)
    return template


def parse_instance(data: dict[str, Any]) -> CampaignInstance:
    """Validate an instance mapping into a :class:`CampaignInstance`."""
    inner = _unwrap(data, "campaign_instance", "instance")
    try:
        return CampaignInstance.model_validate(inner)
    except Exception as exc:
        raise TemplateError(f"instance validation failed: {exc}") from exc


def load_template(source: str | Path) -> CampaignTemplate:
    """Load + validate a template from a YAML file path."""
    return parse_template(_load_yaml(source))


def load_instance(source: str | Path) -> CampaignInstance:
    """Load + validate a client instance from a YAML file path."""
    return parse_instance(_load_yaml(source))


def load_template_by_id(template_id: str) -> CampaignTemplate:
    """Resolve a template by its file stem via on-disk discovery."""
    files = discover_template_files()
    path = files.get(template_id)
    if path is None:
        raise TemplateError(
            f"unknown template_id {template_id!r}; known: {sorted(files)}"
        )
    template = load_template(path)
    if template.template_id != template_id:
        # The file stem is the lookup key; keep it consistent with the declared
        # id so discovery and dispatch never diverge.
        raise TemplateError(
            f"template file {path.name} declares template_id "
            f"{template.template_id!r} but was discovered as {template_id!r}"
        )
    return template


def validate_graph_integrity(template: CampaignTemplate) -> None:
    """Structural + routing validation shared by load and API paths.

    * unique node ids;
    * every edge endpoint references a real node;
    * every node routes to a real ``(workcell, capability)`` pair;
    * KPIs referenced by a node are declared on the template.
    """
    ids = [n.id for n in template.nodes]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise TemplateError(f"duplicate node ids: {sorted(dupes)}")
    id_set = set(ids)
    if not id_set:
        raise TemplateError("template has no nodes")

    for edge in template.edges:
        if edge.from_node not in id_set:
            raise TemplateError(f"edge from unknown node {edge.from_node!r}")
        if edge.to_node not in id_set:
            raise TemplateError(f"edge to unknown node {edge.to_node!r}")

    declared_kpis = {k.key for k in template.kpis}
    for node in template.nodes:
        try:
            assert_routable(node.id, node.target_workcell, node.capability)
        except ValueError as exc:
            raise TemplateError(str(exc)) from exc
        for kpi in node.kpis:
            if kpi not in declared_kpis:
                raise TemplateError(
                    f"node {node.id!r} references undeclared KPI {kpi!r}"
                )


def kpi_definitions(template: CampaignTemplate) -> dict[str, CampaignKPI]:
    """Convenience: template KPI defs keyed by ``key``."""
    return {k.key: k for k in template.kpis}
