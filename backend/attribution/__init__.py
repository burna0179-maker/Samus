"""Attribution engine — which email variant (template / subject / CTA) earns
the best open / reply / close outcomes, learned with a UCB1 bandit.

Clones the live ``backend.strategy`` bandit shape (DDB + JSON store, UCB1
select, decide->learn) but keys the arm on a variant tuple
``template_id::subject_variant::cta_variant`` instead of
``industry::policy_family``. ``select_variant`` chooses; ``record_outcome``
learns from deliverability + CRM-close signals.

Wire-dormant: ``select_variant`` is a deterministic passthrough (returns the
first candidate, i.e. current behavior) unless ``settings.attribution_enabled``
is true. This is the explicitly-LAST component — it only pays off once there is
real send volume and a catalog of >1 variant per slot to choose between; the
engine ships ready so that wiring is a thin step when that volume exists.
"""

from __future__ import annotations

__all__: list[str] = []
