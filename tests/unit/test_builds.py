"""Dataset builds: the determinism property everything else rests on.

NFR-004 is a hard gate - "identical content hash on rebuild, 100% or the release
fails" - so these are not ordinary unit tests. Each one pins a specific way the
hash can change without the data changing.
"""

from __future__ import annotations

import pytest

from data_engine.builds import BuildError, DatasetBuilder, build_hash

pytestmark = pytest.mark.unit


def _episode(episode_id: str, *, source: str = "src-1", artifact: str = "art-1") -> dict:
    return {
        "id": episode_id,
        "source_hash": source,
        "artifact_hash": artifact,
        "format": "synthetic-json",
    }


def _episodes() -> list[dict]:
    return [_episode("ep-b"), _episode("ep-a"), _episode("ep-c")]


# ---------------------------------------------------------------- determinism


def test_rebuilding_the_same_selection_gives_the_same_hash() -> None:
    """The whole promise. Same inputs, same address, forever."""
    first = DatasetBuilder(code_commit="abc123").build(_episodes(), name="baseline")
    second = DatasetBuilder(code_commit="abc123").build(_episodes(), name="baseline")
    assert first.hash == second.hash


def test_selection_order_does_not_change_the_hash() -> None:
    """A manifest is a sequence, so the builder sorts it.

    This is the failure that would never be noticed by eye and would break every
    rebuild: a database returning rows in a different order after a vacuum, a
    plan change, or a different version, silently producing a different "same"
    dataset.
    """
    forwards = DatasetBuilder(code_commit="abc123").build(_episodes(), name="baseline")
    backwards = DatasetBuilder(code_commit="abc123").build(
        list(reversed(_episodes())), name="baseline"
    )
    assert forwards.hash == backwards.hash
    assert forwards.manifest["episodes"] == backwards.manifest["episodes"]


def test_the_manifest_carries_no_timestamp() -> None:
    """A build made at 09:14 and one at 09:15 are the same build.

    `created_at` is a column on the build, recorded once and never hashed. If it
    ever moves into the manifest, determinism is gone and every rebuild looks
    like a change.
    """
    manifest = DatasetBuilder(code_commit="abc123").build(_episodes(), name="x").manifest
    for volatile in ("created_at", "built_at", "timestamp", "now", "generated_at"):
        assert volatile not in manifest, f"{volatile} in the manifest breaks determinism"


def test_different_code_makes_a_different_build() -> None:
    """Two commits are two builds, even over identical episodes."""
    old = DatasetBuilder(code_commit="aaa").build(_episodes(), name="x")
    new = DatasetBuilder(code_commit="bbb").build(_episodes(), name="x")
    assert old.hash != new.hash


def test_a_different_name_makes_a_different_build() -> None:
    """Name is part of the identity: two builds over one selection are two builds."""
    one = DatasetBuilder(code_commit="a").build(_episodes(), name="train")
    two = DatasetBuilder(code_commit="a").build(_episodes(), name="eval")
    assert one.hash != two.hash


def test_a_different_selection_makes_a_different_build() -> None:
    fewer = DatasetBuilder(code_commit="a").build(_episodes()[:2], name="x")
    more = DatasetBuilder(code_commit="a").build(_episodes(), name="x")
    assert fewer.hash != more.hash


def test_changing_an_episode_artifact_changes_the_hash() -> None:
    """Identity is content, not a label. A re-ingested episode is a new build."""
    before = DatasetBuilder(code_commit="a").build(_episodes(), name="x")
    mutated = _episodes()
    mutated[0]["artifact_hash"] = "art-CHANGED"
    after = DatasetBuilder(code_commit="a").build(mutated, name="x")
    assert before.hash != after.hash


# ------------------------------------------------------------------- identity


def test_the_hash_is_a_stable_content_address() -> None:
    """Recomputing from the stored manifest must reproduce the stored hash.

    This is what makes a build verifiable after the fact by anyone holding the
    manifest, and it is why the manifest is stored verbatim.
    """
    result = DatasetBuilder(code_commit="abc123").build(_episodes(), name="baseline")
    assert build_hash(result.manifest) == result.hash
    assert result.hash.startswith("bld_")


def test_the_manifest_cites_the_validation_policy_by_content_address() -> None:
    profile = {"hash": "vp_abc", "name": "strict", "version": "3"}
    result = DatasetBuilder(code_commit="a").build(_episodes(), name="x", profile=profile)
    cited = result.manifest["validation_profile"]
    assert cited["hash"] == "vp_abc"
    assert result.profile_hash == "vp_abc"
    # Changing the policy changes the build: same episodes, different policy,
    # is genuinely a different dataset claim.
    other = DatasetBuilder(code_commit="a").build(
        _episodes(), name="x", profile={**profile, "hash": "vp_xyz"}
    )
    assert result.hash != other.hash


# --------------------------------------------------------------------- errors


def test_an_empty_selection_is_refused() -> None:
    """A build of zero episodes is not a dataset.

    Producing one silently is how an empty training run gets shipped, so it is an
    error the caller has to see.
    """
    with pytest.raises(BuildError, match="at least one episode"):
        DatasetBuilder(code_commit="a").build([], name="empty")


def test_a_duplicate_episode_is_refused() -> None:
    with pytest.raises(BuildError, match="twice"):
        DatasetBuilder(code_commit="a").build([_episode("ep-a"), _episode("ep-a")], name="x")


def test_an_episode_with_no_id_is_refused() -> None:
    """An unciteable member would produce a manifest nobody can reproduce."""
    with pytest.raises(BuildError, match="no id"):
        DatasetBuilder(code_commit="a").build([{"source_hash": "s"}], name="x")


def test_an_unknown_commit_is_recorded_as_empty_not_invented() -> None:
    """A build from an installed wheel has no commit; that is an honest unknown."""
    result = DatasetBuilder().build(_episodes(), name="x")
    assert result.code_commit == ""
