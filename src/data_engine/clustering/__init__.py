"""Task-string clustering as the engine runs it: extract, group, propose, confirm."""

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
