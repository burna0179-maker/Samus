"""Outreach workcell — multi-turn conversation orchestrator.

Per the recovered ``autonomous_closer.py`` + ``crm_feedback_engine.py``:

  - 7-state FSM tracks one outbound call:
      open -> pitch -> engage -> {handle_objection | close_attempt}
      -> {fallback | exit}
  - Each ``advance_call`` returns the next state + the action string the
    Vapi / human script reader should perform.
  - ``log_outcome`` writes to in-process metrics (top objections, best
    products by close rate, angle win-rate). Aggregate ``get_metrics()``
    snapshots for the optimizer to consume.
  - ``send_message`` is a capability slot for the upcoming SES / Twilio /
    Vapi adapter — currently raises NotImplementedError.
"""
