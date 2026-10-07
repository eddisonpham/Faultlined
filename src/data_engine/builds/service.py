"""Deterministic dataset builds (FR-006, FR-007, FR-008)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from data_engine.canonical import canonical_json


def build_hash(manifest: dict[str, Any]) -> str:
    """The content address of a manifest."""
    return "bld_" + hashlib.sha256(canonical_json(manifest)).hexdigest()


@dataclass(frozen=True)
class BuildResult:
    hash: str
    name: str
    manifest: dict[str, Any]
    episode_count: int
    profile_hash: str | None
    code_commit: str
    episodes: list[dict[str, Any]] = field(default_factory=list)


class BuildError(Exception):
    """A build that cannot be produced, with a reason a caller can act on."""


class DatasetBuilder:
    """Assembles a manifest from episodes that are already in the catalog."""

    def __init__(self, *, code_commit: str = "") -> None:
        self.code_commit = code_commit

    def build(
        self,
        episodes: list[dict[str, Any]],
        *,
        name: str,
        profile: dict[str, Any] | None = None,
    ) -> BuildResult:
        """Produce a manifest for `episodes`."""
        if not episodes:
            raise BuildError("a build needs at least one episode; the selection resolved to none")

        members: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in episodes:
            episode_id = str(row.get("id") or "")
            if not episode_id:
                raise BuildError("an episode row has no id; it cannot be cited")
            if episode_id in seen:
                raise BuildError(f"episode {episode_id} selected twice")
            seen.add(episode_id)
            members.append(
                {
                    "episode_id": episode_id,
                    "source_hash": str(row.get("source_hash") or ""),
                    "artifact_hash": str(row.get("artifact_hash") or ""),
                    "format": str(row.get("format") or ""),
                }
            )

        members.sort(key=lambda member: member["episode_id"])

        manifest: dict[str, Any] = {
            "kind": "dataset-build",
            "version": 1,
            "name": name,
            "code_commit": self.code_commit,
            "episode_count": len(members),
            "episodes": members,
        }
        if profile is not None:
            manifest["validation_profile"] = {
                "hash": str(profile.get("hash") or ""),
                "name": str(profile.get("name") or ""),
                "version": str(profile.get("version") or ""),
            }

        return BuildResult(
            hash=build_hash(manifest),
            name=name,
            manifest=manifest,
            episode_count=len(members),
            profile_hash=(manifest.get("validation_profile") or {}).get("hash") or None,
            code_commit=self.code_commit,
            episodes=members,
        )
