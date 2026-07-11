"""Strategy workcell — decision layer above fulfillment.

Scores opportunities + selects actions (replan/outreach/escalate/monitor)
+ dispatches via gateway. Reads CRM via signed_post_json; dispatches via
gateway. Phase 4 (initial), expandable to multi-armed bandits + LLM
portfolio manager (Layer 6).

Also hosts pure-function decision primitives (trust scorer, credit ledger,
capability marketplace) consumed by higher-level orchestration / governance
gates. Those primitives perform no I/O.
"""

from __future__ import annotations
