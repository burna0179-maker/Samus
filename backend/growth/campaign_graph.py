"""Neural Campaign Graph (NCG).

JSON-persisted directed graph whose edges accumulate UCB-style reward stats.
Nodes represent content production tasks (objective x channel x angle);
edges represent campaign flow transitions and track wins/trials/reward_sum.

On each pass the engine:
  1. suggest_next_batch() -- scores nodes by best outgoing UCB edge score,
     returns ContentBrief list consumable by content.planner.plan_content_month()
  2. record_outcome()     -- updates edge stats from performance metrics +
     logs to ExperimentRegistry

Graph persisted at: $SAMUS_DATA_ROOT/growth/campaign_graph.json
Bootstraps with a minimal seed graph on first run.

Design rules (mirror campaign_cycle.py):
  * Idempotent reads + writes — safe to call on any cadence.
  * Fail-soft — load/save errors are logged, not raised.
  * No LLM calls here. Keyword derivation is deterministic.
  * No new external dependencies.

UCB formula per edge::

    score = avg_reward + C * sqrt(ln(total_trials + 1) / edge_trials)

where C = 1.4 (standard UCB1 exploration constant). Untried edges score inf
so every edge is tried before exploitation begins.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("samus.growth.campaign_graph")

_UCB_C: float = 1.4
_MAX_NODES: int = 200


def _growth_root() -> Path:
    base = Path(os.getenv("SAMUS_DATA_ROOT", "data"))
    p = base / "growth"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Graph shapes
# ---------------------------------------------------------------------------

@dataclass
class GraphNode:
    id: str
    objective: str       # "retain_existing" | "cold_top_funnel" | "upsell_vip" | ...
    channel: str         # "email" | "blog" | "social" | "outreach"
    angle: str           # content hook / approach
    industry: str = ""
    persona_mode: str = "helpful"
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = time.time()


@dataclass
class ContentBrief:
    """One suggested content production task from the campaign graph."""
    task_id: str
    node_id: str
    objective: str
    channel: str
    angle: str
    industry: str
    persona_mode: str
    primary_keywords: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Seed graph (bootstrapped on first run)
# ---------------------------------------------------------------------------

_SEED_NODES: list[dict[str, Any]] = [
    {
        "id": "email_retention_helpful",
        "objective": "retain_existing",
        "channel": "email",
        "angle": "helpful_tips",
        "industry": "",
        "persona_mode": "reassuring",
        "created_at": 0.0,
    },
    {
        "id": "blog_cold_authority",
        "objective": "cold_top_funnel",
        "channel": "blog",
        "angle": "authority_guide",
        "industry": "",
        "persona_mode": "bold",
        "created_at": 0.0,
    },
    {
        "id": "social_cold_curiosity",
        "objective": "cold_top_funnel",
        "channel": "social",
        "angle": "curiosity_hook",
        "industry": "",
        "persona_mode": "bold",
        "created_at": 0.0,
    },
    {
        "id": "email_vip_upsell",
        "objective": "upsell_vip",
        "channel": "email",
        "angle": "exclusive_offer",
        "industry": "",
        "persona_mode": "grateful",
        "created_at": 0.0,
    },
    {
        "id": "outreach_promising_activate",
        "objective": "activate_promising",
        "channel": "outreach",
        "angle": "quick_win_demo",
        "industry": "",
        "persona_mode": "energetic",
        "created_at": 0.0,
    },
]

_SEED_EDGES: list[dict[str, Any]] = [
    {"source": "social_cold_curiosity",       "target": "blog_cold_authority",
     "trials": 0, "wins": 0, "reward_sum": 0.0},
    {"source": "blog_cold_authority",          "target": "email_retention_helpful",
     "trials": 0, "wins": 0, "reward_sum": 0.0},
    {"source": "email_retention_helpful",      "target": "email_vip_upsell",
     "trials": 0, "wins": 0, "reward_sum": 0.0},
    {"source": "email_vip_upsell",             "target": "outreach_promising_activate",
     "trials": 0, "wins": 0, "reward_sum": 0.0},
    {"source": "outreach_promising_activate",  "target": "social_cold_curiosity",
     "trials": 0, "wins": 0, "reward_sum": 0.0},
]

# Deterministic keyword mapping: objective -> base keyword set
_OBJECTIVE_KEYWORDS: dict[str, list[str]] = {
    "retain_existing":    ["customer retention", "client success"],
    "cold_top_funnel":    ["local SEO", "small business marketing"],
    "upsell_vip":         ["website optimization", "business growth"],
    "activate_promising": ["online marketing", "quick wins"],
}


# ---------------------------------------------------------------------------
# UCB scoring
# ---------------------------------------------------------------------------

def _ucb_score(trials: int, reward_sum: float, total_trials: int) -> float:
    if trials == 0:
        return float("inf")
    avg = reward_sum / trials
    exploration = _UCB_C * math.sqrt(math.log(total_trials + 1) / trials)
    return avg + exploration


def _best_ucb_by_source(
    edges: list[dict[str, Any]],
    total_trials: int,
) -> dict[str, float]:
    best: dict[str, float] = {}
    for e in edges:
        src = str(e.get("source", ""))
        score = _ucb_score(
            int(e.get("trials", 0)),
            float(e.get("reward_sum", 0.0)),
            total_trials,
        )
        if src not in best or score > best[src]:
            best[src] = score
    return best


# ---------------------------------------------------------------------------
# Campaign Graph Engine
# ---------------------------------------------------------------------------

class CampaignGraphEngine:
    """UCB-driven campaign graph: schedules content tasks, learns from outcomes."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root if root is not None else _growth_root()
        self._root.mkdir(parents=True, exist_ok=True)
        self._graph_path = self._root / "campaign_graph.json"

    # --- persistence ---------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if not self._graph_path.exists():
            graph: dict[str, Any] = {
                "nodes": list(_SEED_NODES),
                "edges": list(_SEED_EDGES),
            }
            self._save(graph)
            return graph
        try:
            return json.loads(self._graph_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("campaign_graph load failed, resetting: %s", exc)
            return {"nodes": list(_SEED_NODES), "edges": list(_SEED_EDGES)}

    def _save(self, graph: dict[str, Any]) -> None:
        try:
            self._graph_path.write_text(
                json.dumps(graph, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("campaign_graph save failed: %s", exc)

    # --- keyword derivation --------------------------------------------------

    @staticmethod
    def _keywords_for_node(
        node: dict[str, Any],
        signals: dict[str, Any],
    ) -> list[str]:
        objective = str(node.get("objective", ""))
        angle = str(node.get("angle", ""))
        base = _OBJECTIVE_KEYWORDS.get(objective, ["digital marketing"])
        if "authority" in angle or "guide" in angle:
            return base + [f"{base[0]} guide", f"best {base[0]} tips"]
        if "offer" in angle or "upsell" in angle:
            return base + [f"{base[0]} results"]
        return base + [f"{base[0]} for small business"]

    # --- public API ----------------------------------------------------------

    def suggest_next_batch(
        self,
        signals: dict[str, Any],
        batch_size: int = 3,
    ) -> list[ContentBrief]:
        """Return up to batch_size ContentBrief objects ranked by UCB edge score.

        signals: growth signals dict (by_segment, refund_pressure, mrr, etc.)
        Connects to content.planner.plan_content_month() via primary_keywords.
        """
        graph = self._load()
        nodes: list[dict[str, Any]] = graph.get("nodes", [])
        edges: list[dict[str, Any]] = graph.get("edges", [])

        total_trials = sum(int(e.get("trials", 0)) for e in edges)
        best_score = _best_ucb_by_source(edges, total_trials)

        ranked = sorted(
            nodes,
            key=lambda n: best_score.get(str(n.get("id", "")), 0.0),
            reverse=True,
        )

        briefs: list[ContentBrief] = []
        for node in ranked[:batch_size]:
            briefs.append(ContentBrief(
                task_id=uuid.uuid4().hex,
                node_id=str(node.get("id", "")),
                objective=str(node.get("objective", "")),
                channel=str(node.get("channel", "")),
                angle=str(node.get("angle", "")),
                industry=str(node.get("industry", "")),
                persona_mode=str(node.get("persona_mode", "helpful")),
                primary_keywords=self._keywords_for_node(node, signals),
                context={"signals": signals},
            ))

        _LOG.info("campaign_graph suggested %d briefs", len(briefs))
        return briefs

    def record_outcome(
        self,
        node_id: str,
        metrics: dict[str, Any],
        registry: Any = None,
    ) -> None:
        """Update UCB edge stats for edges targeting node_id.

        metrics keys used (USD or unit counts):
          revenue, conversions, clicks, opens

        reward = revenue (primary) -> conversions -> clicks * 0.01
        Logs to registry (ExperimentRegistry) when provided.
        """
        reward = float(metrics.get("revenue", 0.0))
        if reward <= 0.0:
            reward = float(metrics.get("conversions", 0.0))
        if reward <= 0.0:
            reward = float(metrics.get("clicks", 0.0)) * 0.01

        graph = self._load()
        updated = False
        for e in graph.get("edges", []):
            if str(e.get("target", "")) != node_id:
                continue
            e["trials"] = int(e.get("trials", 0)) + 1
            if reward > 0.0:
                e["wins"] = int(e.get("wins", 0)) + 1
            e["reward_sum"] = float(e.get("reward_sum", 0.0)) + reward
            updated = True

        if updated:
            self._save(graph)

        if registry is not None:
            try:
                registry.log(
                    kind="content",
                    inputs={"node_id": node_id},
                    outputs=metrics,
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("experiment_registry log failed: %s", exc)

        _LOG.info(
            "campaign_graph recorded outcome node=%s reward=%.3f updated_edges=%s",
            node_id, reward, updated,
        )

    def add_node(self, node: GraphNode) -> None:
        """Append a new node (deduped by id). Evicts coldest nodes when over cap."""
        graph = self._load()
        nodes: list[dict[str, Any]] = graph.get("nodes", [])
        edges: list[dict[str, Any]] = graph.get("edges", [])

        if any(str(n.get("id", "")) == node.id for n in nodes):
            return  # already present

        if len(nodes) >= _MAX_NODES:
            total_trials = sum(int(e.get("trials", 0)) for e in edges)
            best_score = _best_ucb_by_source(edges, total_trials)
            nodes.sort(key=lambda n: best_score.get(str(n.get("id", "")), 0.0))
            nodes = nodes[1:]  # evict the coldest

        nodes.append(asdict(node))
        graph["nodes"] = nodes
        self._save(graph)
        _LOG.info("campaign_graph added node id=%s", node.id)

    def graph_stats(self) -> dict[str, Any]:
        """Return a lightweight summary of graph state for the control-tick snapshot."""
        graph = self._load()
        nodes = graph.get("nodes", [])
        edges = graph.get("edges", [])
        total_trials = sum(int(e.get("trials", 0)) for e in edges)
        total_wins = sum(int(e.get("wins", 0)) for e in edges)
        return {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "total_trials": total_trials,
            "total_wins": total_wins,
            "win_rate": round(total_wins / total_trials, 4) if total_trials else 0.0,
        }


__all__ = ["CampaignGraphEngine", "ContentBrief", "GraphNode"]
