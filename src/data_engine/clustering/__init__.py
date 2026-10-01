"""Task-string clustering as the engine runs it: extract, group, propose, confirm.

The public surface is deliberately small, because the interesting decisions are all
recorded as data rather than as configuration:

    from data_engine.clustering import build, Ignored, Lexicon

    proposals = build({"pick up the red mug": 12, "pick up the blue mug": 3},
                      ignored=Ignored(verb=True, colour=False, site=False))
    proposals.health()   # the numbers a reviewer needs
    proposals.proposals  # what to show

**No model, no extra dependency.** Everything here is pure Python on purpose. The stage
2.5 experiments needed MiniLM to find out that sentence embeddings were the wrong tool
here; the engine does not need them to group task strings, and keeping them out means
`pip install faultlined` still installs cleanly on a machine with no GPU and no
sentence-transformers wheel.

See `experiments/clustering/results/README.md` (EXP-2.5-08) for the measurement behind
the extract-then-group order, and `agents/decisions/0026-cluster-proposals.md` for the
decision to keep the word lists as data.
"""

from __future__ import annotations

from data_engine.clustering.extract import Coverage, Extraction, coverage, extract, extract_all
from data_engine.clustering.layout import Layout, convex_hull, project
from data_engine.clustering.lexicon import AXES, Ignored, Lexicon
from data_engine.clustering.online import (
    RULES,
    Assignment,
    Cluster,
    OnlineCentroids,
    RunningMean,
    token_vector,
)
from data_engine.clustering.proposals import (
    Member,
    Proposal,
    ProposalSet,
    build,
    from_texts,
    proposal_key,
    sample_tasks,
)

__all__ = [
    "AXES",
    "RULES",
    "Assignment",
    "Cluster",
    "Coverage",
    "Extraction",
    "Ignored",
    "Layout",
    "Lexicon",
    "Member",
    "OnlineCentroids",
    "Proposal",
    "ProposalSet",
    "RunningMean",
    "build",
    "convex_hull",
    "coverage",
    "extract",
    "extract_all",
    "from_texts",
    "project",
    "proposal_key",
    "sample_tasks",
    "token_vector",
]
