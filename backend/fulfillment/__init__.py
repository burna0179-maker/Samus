"""Fulfillment workcell — execution planning, risk gating, runbook assembly.

Per doc §7. Pure in-process planning; no side effects beyond audit and the
idempotency cache. Hands off the runbook + execution graph to a downstream
executor in a later phase.
"""
