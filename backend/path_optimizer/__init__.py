"""Path-optimizer workcell — dynamic execution-route selection.

Given a target workcell's recent performance (live ``efficiency_ema`` plus a
supplied recent-outcome history), choose how the next task should run:
autonomous LLM, hybrid template, deterministic scaffold, or safe static
fallback. Pure deterministic routing — zero LLM calls — so a failing workcell
stops burning tokens by retrying its own broken logic.
"""
