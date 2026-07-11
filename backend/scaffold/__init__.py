"""Scaffold workcell — asset generation (proposals, plans, briefs).

Per doc §6. In-process generation only; HTTP via ``app.py``, SQS via
``worker.py``. All endpoints go through ``check_capability("scaffold", ...)``.
"""
