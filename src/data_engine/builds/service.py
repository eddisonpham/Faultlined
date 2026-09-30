"""Deterministic dataset builds (FR-006, FR-007, FR-008).

A build turns a set of registered episodes into a **content-addressed dataset**
with a lineage manifest: the exact episode ids and hashes it contains, the
validation policy that was in force, and the code that produced it.

The whole design turns on one property: **the build's identity is a hash of its
contents, and nothing else.** Rebuilding the same episodes under the same policy
and the same commit yields the same hash (NFR-004, which is a hard release
gate: 100%, not a target). That constrains three things, and each is a decision
rather than an accident:

- **No timestamps in the hashed body.** A build made at 09:14 and one at 09:15
  from identical inputs are the same build. `created_at` is a column, recorded
  once, never hashed.
- **No set or dict iteration order.** Episodes are sorted by id, so the manifest
  is a sequence, not a bag. `canonical_json` sorts keys, but it cannot save an
  unordered collection.
- **A resolved identity, not a name.** Selecting episodes by "the ones I liked"
  is unreproducible by definition. A build names episode ids.

Everything a reader needs to reconstruct or contest the build is in the manifest
except the time, which is the one thing that is not an input to it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from data_engine.canonical import canonical_json


def build_hash(manifest: dict[str, Any]) -> str:
    """The content address of a manifest.

    A prefix would make the hash self-describing at a glance in a log line, and
    costs nothing: the value is never parsed, only compared.
    """
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
    """Assembles a manifest from episodes that are already in the catalog.

    Deliberately pure: it takes episode rows and returns a manifest. It does not
    read the catalog, write rows, or know about job ids. That is what makes the
    determinism test possible without a database, and it keeps "what went into
    this build" a question with one answer.
    """

    def __init__(self, *, code_commit: str = "") -> None:
        # A build made by different code is a different build. That is the point
        # of recording it, so an empty commit is an explicit "unknown" rather
        # than a silent constant.
        self.code_commit = code_commit

    def build(
        self,
        episodes: list[dict[str, Any]],
        *,
        name: str,
        profile: dict[str, Any] | None = None,
    ) -> BuildResult:
        """Produce a manifest for `episodes`.

        Raises BuildError for the two ways a selection can be wrong: nothing
        selected (a build of zero episodes is not a dataset, and silently
        producing one is how an empty training run gets shipped), and the same
        episode selected twice.
        """
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

        # Sorted by id: the manifest is a sequence, so two builds of the same set
        # in a different order are the same build. This is the determinism rule
        # that is easiest to get wrong and impossible to notice by eye.
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
            # The policy by content address, so a build cannot cite a policy
            # whose content later changes underneath it.
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
