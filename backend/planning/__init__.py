"""Planning layer — goal tree + multi-horizon plans + auto-replanning.

HOTL Tranche 4 (framework Phases 4-5). This package turns the revenue target
that lived only as constants in ``backend/cognitive/intelligence_cycle.py``
into a persisted, decomposed goal tree (year -> 90d -> 30d -> weekly ->
daily), and replaces the one-shot MAPE-K output of ``backend/common/autonomy``
with persisted :class:`~backend.planning.models.Plan` objects whose
assumptions are checkable predicates against the unified business-event
stream. A violated assumption regenerates the plan automatically (Plan B) and
mints a decision record; a replan that would breach budget posture or risk
tier escalates to the operator via the approval queue.

Public entry points:
  * :func:`backend.planning.goal_tree.seed_goal_tree` — decompose + persist.
  * :func:`backend.planning.planner.generate_plan` — mint + persist a plan.
  * :func:`backend.planning.planner.evaluate_and_replan` — the control-tick hook.
  * :func:`backend.planning.routes.register_planning_routes` — gateway wiring.
"""
