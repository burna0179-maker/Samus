"""Optimizer workcell — UCB1 bandit + portfolio scorer.

Two capabilities composed into one workcell:

  - **Bandit** (``select_arm`` / ``update_arm``): UCB1 multi-armed bandit for
    A/B/C-style outreach variant selection. State per ``campaign_id`` is kept
    in ``GLOBAL_IDEMPOTENCY_STORE`` so it survives within the process across
    requests. Reward is a weighted sum of reply / conversion / engagement
    signals fed by the outreach workcell.
  - **Portfolio** (``optimize_portfolio``): ranks opportunities by
    expected_value × conversion_prob × momentum × time_penalty - execution_cost
    and tiers them into accelerate / maintain / defer / deprioritize under
    a budget cap. Direct port of recovery/campaign_optimizer.py.

Both are pure-Python and stateless apart from the bandit's per-campaign
state. No external API calls.
"""
