"""Leadgen workcell — B2B account scoring + qualification.

Pipeline (in-process):

    LeadRequest -> normalize_domain -> enrich -> classify_segment -> score
      -> tier -> build_recommendations -> LeadScore

HTTP surface in ``app.py``; SQS worker in ``worker.py``. All endpoints go through
``check_capability("leadgen", ...)``. Per-process idempotency is keyed on
``f"{normalized_domain}:{company.lower()}"`` and backed by
``backend.common.idempotency.GLOBAL_IDEMPOTENCY_STORE``.
"""
