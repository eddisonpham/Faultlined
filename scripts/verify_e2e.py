"""The end-to-end run, driven from an empty catalog to prove the fixes.

Every step asserts something. This is not a smoke test - it is the second half of
the observation that found the defects in the first place, and a fix that is not
re-observed against the running system is a fix that has only been compiled.

The run counts absolute state (five episodes, two builds, one quarantined), so
it refuses to start against a populated catalog instead of failing confusingly
halfway through: wipe first with `just reset --yes`, then re-run. That command
is destructive, which is why the driver never runs it on the caller's behalf,
and it is why a completed run is not re-runnable in place.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from typing import NoReturn

BASE = "http://127.0.0.1:8000"
PASS, FAIL = [], []


def call(method, path, *, data=None, form=None, raw=False):
    body = None
    headers = {}
    if form is not None:
        from urllib.parse import urlencode

        body = urlencode(form).encode()
        headers["content-type"] = "application/x-www-form-urlencoded"
    elif data is not None:
        body = json.dumps(data).encode()
        headers["content-type"] = "application/json"
    request = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            payload = response.read()
            return response.status, (payload if raw else json.loads(payload or b"null"))
    except urllib.error.HTTPError as error:
        return error.code, (error.read() if raw else json.loads(error.read() or b"null"))


def abort(reason, hint="") -> NoReturn:
    print(f"\n  [abort] {reason}")
    if hint:
        print(f"          {hint}")
    sys.exit(2)


def pick(items, predicate, what):
    """The one item a later assertion needs, or a clean stop - never a traceback."""
    for item in items:
        if predicate(item):
            return item
    abort(
        f"nothing matched while looking for {what}",
        "an earlier section likely failed; scroll up to the first FAIL.",
    )


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(f"  [{'ok ' if condition else 'FAIL'}] {name}{('  -> ' + str(detail)) if detail else ''}")


def settle(predicate, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    return False


def jobs():
    return call("GET", "/api/v1/jobs?limit=50")[1]["items"]


def wait_for_idle():
    if not settle(lambda: all(j["state"] in {"succeeded", "failed", "canceled"} for j in jobs())):
        stuck = [
            f"{j['type']}:{j['state']}"
            for j in jobs()
            if j["state"] not in {"succeeded", "failed", "canceled"}
        ]
        abort("jobs did not settle before the next assertion", f"still open: {', '.join(stuck)}")


def preflight():
    """Refuse to run against anything but the empty catalog the run assumes."""
    try:
        status, _body = call("GET", "/api/v1/health")
    except urllib.error.URLError as error:
        abort(f"no server answering on {BASE} ({error.reason})", "start one with `just run`.")
    if status != 200:
        abort(f"/api/v1/health returned HTTP {status}", "is this the faultlined server?")
    populated = [
        name
        for name, path in (
            ("jobs", "/api/v1/jobs?limit=1"),
            ("episodes", "/api/v1/episodes?limit=1"),
            ("builds", "/api/v1/builds?limit=1"),
            ("slices", "/api/v1/slices?limit=1"),
        )
        if call("GET", path)[1]["items"]
    ]
    if populated:
        abort(
            f"the catalog is not empty ({', '.join(populated)} present)",
            "this run counts absolute state; wipe first with `just reset --yes`, then re-run.",
        )


PROFILES = {
    "staged": {
        "name": "so101-staged",
        "version": "1",
        "required_channels": [],
        "forbidden_channels": ["debug"],
        "min_frames": 100,
        "max_frames": 100000,
        "min_duration_seconds": 1.0,
        "max_duration_seconds": 3600.0,
        "min_fps": 5.0,
        "max_fps": 200.0,
    },
    "loose": {
        "name": "so101-loose",
        "version": "1",
        "required_channels": [],
        "forbidden_channels": [],
        "min_frames": 5,
        "max_frames": 100000,
        "min_duration_seconds": 0.1,
        "max_duration_seconds": 3600.0,
        "min_fps": 1.0,
        "max_fps": 1000.0,
    },
}

preflight()
print("\n=== 1. ingest, through the browser form, all three formats ===")
for label, form in (
    ("mcap", {"kind": "path", "source": "var/real-data/so101_pick_place.mcap"}),
    ("lerobot v3", {"kind": "path", "source": "var/real-data/svla_so101_pickplace"}),
    ("lerobot v2", {"kind": "path", "source": "var/real-data/lerobot-v2-driving"}),
    ("synthetic", {"kind": "episode", "task": "wipe_table", "robot": "so101", "frames": "240"}),
):
    # urllib follows the 303, so the status alone proves nothing; the job is the
    # evidence that the submission was accepted.
    before = len(jobs())
    call("POST", "/ui/jobs", form=form, raw=True)
    check(f"form accepts {label}", len(jobs()) > before)
wait_for_idle()
episodes = call("GET", "/api/v1/episodes?limit=50")[1]["items"]
check("four episodes registered", len(episodes) == 4, [e["format"] for e in episodes])
check(
    "every ingest succeeded",
    all(j["state"] == "succeeded" for j in jobs() if j["type"].startswith("ingest")),
)

print("\n=== 2. the form can now reach a non-default episode ===")
before = len(jobs())
call(
    "POST",
    "/ui/jobs",
    form={
        "kind": "path",
        "source": "var/real-data/svla_so101_pickplace",
        "episode_key": "episode_index=7",
    },
    raw=True,
)
check("episode_key accepted by the form", len(jobs()) > before)
wait_for_idle()
episodes = call("GET", "/api/v1/episodes?limit=50")[1]["items"]
keys = sorted(e["episode_key"] for e in episodes)
check("a second LeRobot episode was ingested", "episode_index=7" in keys, keys)

print("\n=== 3. DEFECT 1: the episode's length is its own, not the quality sample ===")
mcap = pick(episodes, lambda e: e["format"] == "mcap", "the MCAP episode")
check(
    "MCAP episode reports 72600 frames, not the 816-frame sample",
    mcap["frame_count"] == 72600,
    f"listed as {mcap['frame_count']}",
)
check(
    "the analysed sample is exposed separately, not conflated",
    mcap.get("analysed_frames") == 816,
    mcap.get("analysed_frames"),
)
lerobot = pick(episodes, lambda e: e["format"] == "lerobot-v3", "the LeRobot v3 episode")
check(
    "LeRobot is unchanged where the two numbers always agreed",
    lerobot["frame_count"] == lerobot.get("analysed_frames"),
    f"{lerobot['frame_count']} vs {lerobot.get('analysed_frames')}",
)
detail = call("GET", f"/api/v1/episodes/{mcap['id']}")[1]
check(
    "the episode's own metadata still says 72600",
    detail["metadata"]["frame_count"] == 72600,
)
summary = call("GET", "/api/v1/quality/summary")[1]
check(
    "the length distribution is over episode lengths",
    summary["length"]["max"] == 72600,
    summary["length"],
)
longest = call("GET", "/api/v1/episodes?flag=long&limit=3")[1]["items"]
check(
    "`long` curation ranks the MCAP log first, not last",
    longest[0]["id"] == mcap["id"],
    [e["frame_count"] for e in longest],
)

print("\n=== 4. validate ===")
call(
    "POST",
    "/api/v1/jobs",
    data={"type": "validate", "payload": {"episode_ids": [], "profile": PROFILES["staged"]}},
)
wait_for_idle()
validate_job = pick(jobs(), lambda j: j["type"] == "validate", "the validate job")
result = call("GET", f"/api/v1/jobs/{validate_job['id']}")[1]["result"]
check(
    "one episode was quarantined, four passed",
    result["failed"] == 1 and result["passed"] == 4,
    f"passed={result['passed']} failed={result['failed']}",
)
quarantined = [
    e for e in call("GET", "/api/v1/episodes?limit=50")[1]["items"] if e["state"] == "quarantined"
]
check(
    "the synthetic one is the one quarantined",
    len(quarantined) == 1 and quarantined[0]["format"] == "synthetic-json",
    [e["format"] for e in quarantined],
)

print("\n=== 5. DEFECT 2 + 3: a build excludes quarantine and pins its policy ===")
call(
    "POST",
    "/api/v1/jobs",
    data={
        "type": "build",
        "payload": {"name": "so101-v1", "episode_ids": [], "profile": PROFILES["staged"]},
    },
)
wait_for_idle()
build_job = pick(
    jobs(),
    lambda j: j["type"] == "build" and j["state"] == "succeeded",
    "the first succeeded build job",
)
build = call("GET", f"/api/v1/jobs/{build_job['id']}")[1]["result"]
staged_hash = build["build_hash"]
check(
    "the build contains only validated episodes",
    build["episode_count"] == 4,
    build["episode_count"],
)
members = call("GET", f"/api/v1/builds/{staged_hash}")[1]["manifest"]["episodes"]
check(
    "the quarantined episode is NOT a member",
    all(m["format"] != "synthetic-json" for m in members),
    [m["format"] for m in members],
)
check(
    "profile_hash is a real content address, not null and not empty",
    isinstance(build["profile_hash"], str) and len(build["profile_hash"]) == 64,
    build["profile_hash"],
)
profile_hash = (
    call("GET", "/api/v1/profiles")[1] if False else None
)  # not a route; use the manifest
manifest_profile = call("GET", f"/api/v1/builds/{staged_hash}")[1]["manifest"]["validation_profile"]
check(
    "the manifest cites the same address",
    manifest_profile["hash"] == build["profile_hash"],
    manifest_profile,
)

print("\n=== 6. NFR-004: rebuilding the same selection reproduces the address ===")
call(
    "POST",
    "/api/v1/jobs",
    data={
        "type": "build",
        "payload": {"name": "so101-v1", "episode_ids": [], "profile": PROFILES["staged"]},
    },
)
wait_for_idle()
again = call("GET", "/api/v1/builds")[1]["items"]
check(
    "reproducing the build did not create a second one",
    len(again) == 1,
    [b["hash"][:16] for b in again],
)

print("\n=== 7. a different policy is a different build ===")
call(
    "POST",
    "/api/v1/jobs",
    data={
        "type": "build",
        "payload": {"name": "so101-v1", "episode_ids": [], "profile": PROFILES["loose"]},
    },
)
wait_for_idle()
builds = call("GET", "/api/v1/builds")[1]["items"]
check("two distinct builds now exist", len(builds) == 2, len(builds))
check("their addresses differ", len({b["hash"] for b in builds}) == 2)
check(
    "both cite a real policy address",
    all(b["profile_hash"] and len(b["profile_hash"]) == 64 for b in builds),
    [b["profile_hash"] for b in builds],
)

print("\n=== 8. DEFECT 4: reverse lineage carries every declared field ===")
lineage = call("GET", f"/api/v1/episodes/{mcap['id']}/builds")[1]["items"]
check("the MCAP episode is in both builds", len(lineage) == 2, len(lineage))
check(
    "profile_hash is populated",
    all(b["profile_hash"] for b in lineage),
    [b["profile_hash"] for b in lineage],
)
check(
    "code_commit is populated",
    all(b["code_commit"] for b in lineage),
    [b["code_commit"] for b in lineage],
)
check("job_id is populated", all(b["job_id"] for b in lineage))
check(
    "the quarantined episode is in no build",
    call("GET", f"/api/v1/episodes/{quarantined[0]['id']}/builds")[1]["items"] == [],
)

print("\n=== 9. DEFECT 5: a terminal payload failure costs one attempt ===")
call(
    "POST",
    "/api/v1/jobs",
    data={
        "type": "build",
        "payload": {
            "name": "collision",
            "episode_ids": [],
            "profile": {**PROFILES["staged"], "min_frames": 7},
        },
    },
)
wait_for_idle()
collision = pick(
    jobs(),
    lambda j: j["type"] == "build" and j["state"] == "failed",
    "the failed collision build job",
)
check(
    "a name/version collision is terminal: one attempt, not three",
    collision["attempts"] == 1,
    collision["attempts"],
)
check("it is still reported as a failure", bool(collision["error"]), collision["error"])

print("\n=== 10. the UI shows the truth ===")
page = call("GET", "/ui/episodes", raw=True)[1].decode()
check("the episodes page shows 72600 for the MCAP log", "72600" in page)
check(
    "it no longer shows the sample count as the length",
    ">816<" not in page and "816" not in page.split("so101_pick_place.mcap")[1][:200]
    if "so101_pick_place.mcap" in page
    else False,
    "episode row not found on the page",
)
for path in (
    "",
    "jobs",
    "episodes",
    "insights",
    "failures",
    "slices",
    "incidents",
    "metrics",
    "artifacts",
):
    status, _ = call("GET", f"/ui/{path}", raw=True)
    check(f"/ui/{path or 'status'} renders", status == 200, f"HTTP {status}")


print()
print("=== 11. ADR 0023: the metrics are honest about time ===")
# Every reader now supplies a clock, so `integrity` must be a real verdict for
# all five episodes, not the "unknown" a caller without timestamps gets.
qualities = {}
for episode in call("GET", "/api/v1/episodes?limit=50")[1]["items"]:
    qualities[episode["id"]] = call("GET", f"/api/v1/episodes/{episode['id']}/quality")[1]
check("every episode has a quality record", len(qualities) == 5, len(qualities))
for episode in call("GET", "/api/v1/episodes?limit=50")[1]["items"]:
    quality = qualities[episode["id"]]
    fmt = episode["format"]
    key = episode["episode_key"] or "default"
    label = f"{fmt} {key}"
    check(
        f"{label}: no non-finite value reached the catalog",
        quality["nonfinite"] == 0,
        quality["nonfinite"],
    )
    check(
        f"{label}: integrity is a real verdict, not unknown",
        quality["integrity"] in {"ok", "gapped"},
        quality["integrity"],
    )
    check(
        f"{label}: a maximum gap was measured from the clock",
        quality["max_gap_seconds"] is not None and quality["max_gap_seconds"] > 0,
        quality["max_gap_seconds"],
    )
    check(
        f"{label}: the gap ratio is a fraction",
        0.0 <= quality["gap_ratio"] <= 1.0,
        quality["gap_ratio"],
    )
    check(
        f"{label}: the scores are finite",
        all(
            quality[k] == quality[k] and abs(quality[k]) != float("inf")
            for k in ("movement_score", "jerk_score", "stall_ratio")
        ),
        {k: quality[k] for k in ("movement_score", "jerk_score", "stall_ratio")},
    )
    check(
        f"{label}: judged_dims does not exceed the dims present",
        quality["judged_dims"] <= len(quality["dims"]),
        f"{quality['judged_dims']} of {len(quality['dims'])}",
    )
    if quality["judged_dims"]:
        check(
            f"{label}: a judged episode names its worst dimension",
            quality["worst_dim"] in {d["name"] for d in quality["dims"]},
            quality["worst_dim"],
        )

mcap_id = pick(
    call("GET", "/api/v1/episodes?limit=50")[1]["items"],
    lambda e: e["format"] == "mcap",
    "the MCAP episode",
)["id"]
mcap_q = qualities[mcap_id]
check(
    "the 20-minute MCAP log reports a plausible inter-message gap",
    0.005 < mcap_q["max_gap_seconds"] < 5.0,
    mcap_q["max_gap_seconds"],
)
check(
    "a continuously generated log is not called gapped",
    mcap_q["integrity"] == "ok",
    mcap_q["integrity"],
)
check("the log actually got judged dimensions", mcap_q["judged_dims"] > 0, mcap_q["judged_dims"])
synth_id = pick(
    call("GET", "/api/v1/episodes?limit=50")[1]["items"],
    lambda e: e["format"] == "synthetic-json",
    "the synthetic episode",
)["id"]
check(
    "the synthetic episode carries judged dimensions",
    qualities[synth_id]["judged_dims"] > 0,
    qualities[synth_id]["judged_dims"],
)

print()
print("=== 12. the population aggregates are not poisoned ===")
summary = call("GET", "/api/v1/quality/summary")[1]
check("every episode has a verdict", sum(summary["verdicts"].values()) == 5, summary["verdicts"])
speed = summary["speed_distribution"]
check(
    "the speed distribution is a finite number, not NaN",
    bool(speed) and all(row["movement_score"] == row["movement_score"] for row in speed),
    [row["movement_score"] for row in speed],
)
check(
    "every row of it carries a verdict",
    all(row["verdict"] for row in speed),
    [row["verdict"] for row in speed],
)
check(
    "the length mean is a finite number, not NaN",
    summary["length"]["mean"] == summary["length"]["mean"],
    summary["length"]["mean"],
)


print()
print("=== 13. the visual layer renders against real data ===")
# The charts and graphs are server-rendered, so the honest check is what the
# server actually emits against a real catalog, not a screenshot.
insights = call("GET", "/ui/insights", raw=True)[1].decode()
check(
    "the motion chart is plotted, not a bare scatter",
    insights.count("de-plot-dots") == 1 and insights.count("<circle") >= 5,
    f"{insights.count('<circle')} dots",
)
check("the motion axis is labelled in the normalised unit", "0.0" in insights)
check("recording reliability is rolled up", "Recording reliability" in insights)
check(
    "every episode dot links to its episode",
    all(
        f"/ui/episodes/{e['id']}" in insights
        for e in call("GET", "/api/v1/episodes?limit=50")[1]["items"]
    ),
)
check("the raw score is still shown beside the normalised one", "raw /frame" in insights)

metrics = call("GET", "/ui/metrics", raw=True)[1].decode()
check(
    "the metrics series render as plots with axes",
    metrics.count("de-plot-line") >= 1 and metrics.count("de-tick") > 5,
    f"{metrics.count('de-tick')} ticks",
)
check("each plot carries a caption with its numbers", "de-chart-note" in metrics)
check(
    "each plot's drilldown is a real link, not escaped markup",
    '<a href="/ui/jobs">drill down</a>' in metrics and "&lt;a href" not in metrics,
)

schema = call("GET", "/ui/schema", raw=True)[1].decode()
check(
    "the entity graph is drawn from the live catalog",
    schema.count("de-node-link") == 14,
    f"{schema.count('de-node-link')} nodes",
)
check(
    "the graph is built from real foreign keys",
    schema.count("<path") == 9,
    f"{schema.count('<path')} edges",
)
check("row counts are live", ">5 rows<" in schema)
check("the data flow is described", "How data moves" in schema)
check(
    "a flow step naming a missing table is reported, not drawn",
    "does not" in schema and "have" in schema,
)
check("every table node links to its columns page", 'href="/ui/schema/episodes"' in schema)

quality_table = call("GET", "/ui/schema/episode_quality", raw=True)[1].decode()
check("a table page lists its columns", "episode_id" in quality_table)
check("a table page says who references it", "referenced by:" in quality_table)

build_hashes = [b["hash"] for b in call("GET", "/api/v1/builds")[1]["items"]]
if not build_hashes:
    abort("no builds exist by the visual section", "sections 5-7 should have created two.")
build_page = call("GET", f"/ui/builds/{build_hashes[0]}", raw=True)[1].decode()
check(
    "lineage is drawn as a graph",
    build_page.count("de-node-link") >= 5 and build_page.count("<path") >= 4,
    f"{build_page.count('de-node-link')} nodes",
)
check("lineage cites the commit it was built under", "commit" in build_page)
check(
    "a build that does not exist says so",
    "no such build" in call("GET", "/ui/builds/deadbeef", raw=True)[1].decode(),
)
index = call("GET", "/ui/builds", raw=True)[1].decode()
check(
    "the builds index links to each build",
    all(f'href="/ui/builds/{h}"' in index for h in build_hashes),
)

for path in ("schema", "builds", f"builds/{build_hashes[0]}"):
    status, _ = call("GET", f"/ui/{path}", raw=True)
    check(f"/ui/{path} renders", status == 200, f"HTTP {status}")


print("=== 14. the motion trace: bounded, honest about time, holes at dropouts ===")
TRACE_CAP = 240
for episode in call("GET", "/api/v1/episodes?limit=50")[1]["items"]:
    quality = call("GET", f"/api/v1/episodes/{episode['id']}/quality")[1]
    trace = quality["motion_trace"]
    points = [point for run in trace for point in run]
    label = f"{episode['format']} {episode['episode_key'] or 'default'}"
    check(f"{label}: a motion trace exists", len(points) > 0, f"{len(points)} points")
    check(f"{label}: the trace is bounded", len(points) <= TRACE_CAP, len(points))
    # The list row carries no duration; the detail's metadata does.
    meta = call("GET", f"/api/v1/episodes/{episode['id']}")[1].get("metadata") or {}
    span = float(meta.get("duration_seconds") or 0.0)
    check(
        f"{label}: trace time stays inside the episode",
        all(0.0 <= t <= span + 1.0 for t, _ in points),
        (span, [t for t, _ in points][-1]),
    )
    check(
        f"{label}: trace values are bounded normalised motion",
        all(0.0 <= v <= 1.0 + 1e-9 for _, v in points),
    )

mcap_page = call("GET", f"/ui/episodes/{mcap_id}", raw=True)[1].decode()
check(
    "the episode page draws the motion trace",
    "motion over time" in mcap_page and "de-trace" in mcap_page,
)
check("a continuous recording says so", "continuous recording" in mcap_page)

stamps = [round(i * 0.1, 6) for i in range(10)] + [round(30.0 + i * 0.1, 6) for i in range(10)]
gapped_payload = {
    "episode": {
        "task": "drop_out_test",
        "robot": "so101",
        "timestamps": stamps,
        "observations": [[round(i * 0.01, 6), round(i * 0.02, 6)] for i in range(20)],
        "actions": [[round(i * 0.03, 6), round(i * 0.04, 6)] for i in range(20)],
    }
}
call("POST", "/api/v1/jobs", data={"type": "ingest", "payload": gapped_payload})
wait_for_idle()
# Synthetic episodes carry no episode_key; the task lives in the metadata.
gapped = pick(
    call("GET", "/api/v1/episodes?limit=50")[1]["items"],
    lambda e: (
        (call("GET", f"/api/v1/episodes/{e['id']}")[1].get("metadata") or {}).get("task")
        == "drop_out_test"
    ),
    "the drop-out episode",
)
gq = call("GET", f"/api/v1/episodes/{gapped['id']}/quality")[1]
check("a dropped-out clock is reported gapped", gq["integrity"] == "gapped", gq["integrity"])
check(
    "the trace breaks into two runs at the dropout",
    len(gq["motion_trace"]) == 2,
    len(gq["motion_trace"]),
)
hole_width = gq["motion_trace"][1][0][0] - gq["motion_trace"][0][-1][0]
check("the hole has the dropout's width, not one blank frame", hole_width > 20.0, hole_width)
hole_page = call("GET", f"/ui/episodes/{gapped['id']}", raw=True)[1].decode()
check(
    "the hole renders as two separate lines",
    hole_page.count("de-trace") == 2,
    hole_page.count("de-trace"),
)
check("the caption names the holes as dropouts", "recording dropouts, not stillness" in hole_page)

print("=== 15. a curation slice says what it drops versus the dataset, and why ===")
created = call(
    "POST",
    "/api/v1/slices",
    data={"name": "e2e-survivors", "filter_config": {"state": "valid", "flag": ""}},
)
check("the slice was created", created[0] in (200, 201), f"HTTP {created[0]}")
impact_id = created[1]["id"]
impact = call("GET", f"/api/v1/slices/{impact_id}/impact")[1]
check(
    "kept and dropped partition the dataset",
    impact["kept"]["count"] + impact["dropped"]["count"] == impact["dataset"]["episodes"],
    (impact["kept"]["count"], impact["dropped"]["count"]),
)
check(
    "every drop names its reason",
    sum(r["count"] for r in impact["drop_reasons"]) == impact["dropped"]["count"],
    impact["drop_reasons"],
)
check(
    "reasons are the episodes' own values, not the filter's vocabulary",
    all(
        r["reason"].split("=")[0] in {"state", "verdict", "unscored"}
        for r in impact["drop_reasons"]
    ),
    [r["reason"] for r in impact["drop_reasons"]],
)
check(
    "both sides carry the quality signals they are compared on",
    all(
        k in impact["kept"] and k in impact["dropped"]
        for k in ("median_jerk_score", "median_stall_ratio", "median_frames", "gapped")
    ),
)
impact_page = call("GET", f"/ui/slices/{impact_id}", raw=True)[1].decode()
check(
    "the impact page shows the partition and the reasons",
    "What it drops" in impact_page
    and "state=" in impact_page
    and "median jerk score" in impact_page,
)
check("the keep rate is drawn, not just stated", "Keep rate" in impact_page)

call("POST", "/api/v1/slices", data={"name": "e2e-longest", "filter_config": {"flag": "long"}})
reorder_id = pick(
    call("GET", "/api/v1/slices")[1]["items"],
    lambda s: s["name"] == "e2e-longest",
    "the e2e-longest slice",
)["id"]
reorder = call("GET", f"/api/v1/slices/{reorder_id}/impact")[1]
check(
    "an ordering slice drops nothing and says so",
    reorder["reorders_only"] and reorder["dropped"]["count"] == 0,
    reorder["dropped"],
)
check(
    "the ordering page explains the zero",
    "it excludes nothing" in call("GET", f"/ui/slices/{reorder_id}", raw=True)[1].decode(),
)

print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
for name in FAIL:
    print(f"  FAILED: {name}")
sys.exit(1 if FAIL else 0)
