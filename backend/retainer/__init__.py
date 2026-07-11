"""Retainer (recurring-revenue) fulfillment workcell.

Stream 3 of the deliverable-fulfillment rebuild — owns the monthly-billed
SKUs: ``retainer_seo_optimization`` ($300/mo) and the three AI Ops Partner
tiers (``_starter`` $2k/mo, ``_growth`` $3.5k/mo, ``_scale`` $5k/mo). Sibling
packages handle one-shot SKUs.

Public surface:

  * :mod:`backend.retainer.registry`        — ProductConfig table for the 4 SKUs
  * :func:`backend.retainer.enroll.enroll_retainer`            — start of subscription
  * :func:`backend.retainer.monthly_cycle.run_monthly_cycle`   — monthly DAG runner
  * :func:`backend.retainer.visibility_report.render_visibility_report`

Persistence convention: each customer-month writes its plan + outputs under
``<SAMUS_ARTIFACT_ROOT>/customers/<slug>/<sku>/<YYYY-MM>/``.
"""
from __future__ import annotations

from .registry import RETAINER_SKUS, RetainerProductConfig, get_retainer_sku

__all__ = [
    "RETAINER_SKUS",
    "RetainerProductConfig",
    "get_retainer_sku",
]
