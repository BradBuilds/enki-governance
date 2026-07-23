"""
Auto-learn substrate for extraction workers.

Passive learning loop:
    1. Every signal that hits no_worker_for_type in the runner is
       observed by the shape profiler. The profiler records the
       payload field tree, types, and small sample values into the
       learned_mappings table (upserting by signal_type).
    2. After enough samples accumulate, mapping_learner proposes a
       candidate payload→envelope mapping using ~30 field-name
       heuristics.
    3. mapping_eval scores the proposal by running it against the
       accumulated samples.
    4. promoter (Phase 2.3) watches for stable high-scoring candidates
       and exports them to storage/mappings/learned/*.yaml where the
       registry picks them up on next boot.

All observations and proposals are LDU-stamped so we can audit which
active_set was in effect at each step. Learning never touches GDU
packs.

Public API:
    observe(signal_dict, conn)          # called by runner on no_worker_for_type
    propose_mapping(signal_type, conn)  # manual trigger for learner
    evaluate_candidate(mapping_dict, samples)
    get_status(conn)                    # CLI helper
"""

from core.extraction.learning.shape_profiler import observe, get_profile
from core.extraction.learning.mapping_learner import propose_mapping
from core.extraction.learning.mapping_eval import evaluate_candidate
from core.extraction.learning.mapping_store import get_status, learn_from_observations
from core.extraction.learning.promoter import (
    promote,
    reject,
    list_candidates,
    auto_promote_eligible,
)

__all__ = [
    "observe",
    "get_profile",
    "propose_mapping",
    "evaluate_candidate",
    "get_status",
    "learn_from_observations",
    "promote",
    "reject",
    "list_candidates",
    "auto_promote_eligible",
]
