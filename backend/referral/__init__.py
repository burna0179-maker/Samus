"""Dual-sided referral loop (Phase F).

Referral is the cheapest CAC. This engine makes it structural: a deterministic
share code per customer, a trigger that fires the referral ask at the moment of
first result (not at signup or billing), attribution of a referee back to a
code, qualification when the referee converts, and dual-sided reward
computation. Tracking is appended to an auditable JSONL ledger.

The customer-facing share UI is a website concern (deferred to the web branch);
this is the backend the UI and the CRM hook into.
"""
