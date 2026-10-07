"""Watch a clustering form in real time, and freeze clusters by hand."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import numpy as np

from experiments.clustering import attributes, centroid_experiment, embeddings, figures
from experiments.clustering.methods.online_centroids import OnlineCentroids

LOG_LIMIT = 200


@dataclass(slots=True)
class Session:
    """One streaming run of the frozen configuration."""

    encoder: embeddings.Encoder
    radius: float
    colour_weight: float
    mask_verbs: bool
    rule: str
    max_clusters: int = 64

    texts: list[str] = field(default_factory=list)
    rows: list[np.ndarray] = field(default_factory=list)
    model: OnlineCentroids = field(init=False)
    arrivals: list[dict[str, Any]] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.model = OnlineCentroids(
            radius=self.radius,
            max_clusters=self.max_clusters,
            rule_factory=centroid_experiment.RULES[self.rule],
        )

    def vector_for(self, text: str) -> np.ndarray:
        """Encode one string through the same pipeline the experiments used."""
        encoded = attributes.encode_for_clustering(
            self.encoder, [text], self.colour_weight, mask_verb=self.mask_verbs
        )[text]
        return np.asarray(encoded, dtype=np.float32)

    def feed(self, text: str) -> dict[str, Any]:
        """Admit one string and report what happened to it."""
        text = text.strip()
        if not text:
            return {"text": text, "outcome": "skipped", "reason": "empty"}
        vector = self.vector_for(text)
        with self.lock:
            before = len(self.model.clusters)
            nearest = _nearest(self.model, vector)
            was_frozen = nearest is not None and self.model.clusters[nearest].frozen
            self.model.observe(vector, text)
            self.texts.append(text)
            self.rows.append(vector)
            after = len(self.model.clusters)
            joined = after == before
            event = {
                "text": text,
                "outcome": "joined" if joined else "new cluster",
                "cluster": nearest if joined else after - 1,
                "into_frozen": bool(joined and was_frozen),
                "masked_verb": attributes.verb_of(text) if self.mask_verbs else "",
                "colour": attributes.colour_of(text),
                "at": round(time.monotonic() - self.started, 2),
            }
            self.arrivals.append(event)
            del self.arrivals[:-LOG_LIMIT]
            return event

    def feed_all(self, texts: Iterable[str]) -> None:
        for text in texts:
            self.feed(text)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            counts = self._cluster_sizes()
            return {
                "clusters": len(counts),
                "max_clusters": self.max_clusters,
                "arrived": len(self.texts),
                "frozen": sum(1 for index in counts if self.model.clusters[index].frozen),
                "sizes": counts,
                "labels": {
                    str(index): self.model.clusters[index].label
                    for index in counts
                    if self.model.clusters[index].label
                },
                "config": {
                    "encoder": self.encoder.name,
                    "radius": self.radius,
                    "colour_weight": self.colour_weight,
                    "mask_verbs": self.mask_verbs,
                    "rule": self.rule,
                },
                "log": self.arrivals[-40:][::-1],
                "elapsed": round(time.monotonic() - self.started, 1),
            }

    def _cluster_sizes(self) -> dict[int, int]:
        labels = self.labels()
        sizes: dict[int, int] = {}
        for label in labels:
            sizes[label] = sizes.get(label, 0) + 1
        return sizes

    def labels(self) -> list[int]:
        if not self.texts:
            return []
        matrix = np.stack(self.rows)
        stacked = np.stack([c.centroid for c in self.model.clusters])
        return [int(i) for i in np.argmax(matrix @ stacked.T, axis=1)]

    def confirm(self, index: int, label: str = "") -> bool:
        """Freeze a cluster."""
        with self.lock:
            if not 0 <= index < len(self.model.clusters):
                return False
            self.model.confirm(index, label or f"task {index}")
            return True

    def figures(self) -> dict[str, str]:
        """Treemap, map and heatmap for the state right now."""
        with self.lock:
            return self._render()

    def _render(self) -> dict[str, str]:
        if not self.texts:
            empty = (
                '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="120">'
                '<text x="16" y="60" font-family="ui-monospace, monospace" font-size="13">'
                "waiting for the first task string</text></svg>"
            )
            return {"treemap": empty, "map": empty, "heatmap": empty}

        labels = self.labels()
        sizes_dict = self._cluster_sizes()
        ordered = sorted(sizes_dict)
        sizes = [sizes_dict[index] for index in ordered]
        row_labels = [f"c{index}" for index in ordered]

        tags = {text: [f"colour:{attributes.colour_of(text)}"] for text in self.texts}
        colours = sorted({tag for values in tags.values() for tag in values})
        members: dict[int, list[str]] = {}
        for label, text in zip(labels, self.texts, strict=True):
            members.setdefault(label, []).append(text)
        matrix = figures.cluster_attribute_matrix(members, tags, colours)

        title = f"{self.encoder.name} - {len(self.texts)} strings, {len(ordered)} clusters"
        return {
            "treemap": figures.render_treemap(sizes, row_labels, title),
            "map": figures.render_map(np.stack(self.rows), labels, title),
            "heatmap": figures.render_heatmap(matrix, row_labels, colours, f"colour mix - {title}"),
        }


def _nearest(model: OnlineCentroids, vector: np.ndarray) -> int | None:
    distances = model.distances(vector)
    if distances.size == 0:
        return None
    return int(np.argmin(distances))


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Faultlined clustering - live</title>
<style>
 body { font-family: ui-monospace, monospace; margin: 16px; color: #1f2933; }
 h1 { font-size: 15px; margin: 0 0 4px; }
 .sub { color: #52606d; font-size: 12px; margin-bottom: 10px; }
 .wrap { display: flex; gap: 18px; align-items: flex-start; flex-wrap: wrap; }
 .col { min-width: 380px; }
 img { border: 1px solid #e4e7eb; background: #fff; }
 button { font: inherit; font-size: 11px; padding: 1px 6px; margin-left: 4px;
          cursor: pointer; border: 1px solid #cbd2d9; border-radius: 3px; background: #fff; }
 button:hover { background: #f5f7fa; }
 .chip { display: inline-block; font-size: 11px; padding: 2px 6px; margin: 0 4px 4px 0;
         border: 1px solid #cbd2d9; border-radius: 3px; background: #f5f7fa; }
 .chip.frozen { background: #e3f2fd; border-color: #90caf9; font-weight: 600; }
 table { border-collapse: collapse; font-size: 11px; width: 100%; }
 td, th { padding: 2px 6px; text-align: left; border-bottom: 1px solid #f0f2f4; }
 .new { color: #0b6b3a; font-weight: 600; }
 .froz { color: #0d47a1; font-weight: 600; }
</style></head><body>
<h1>Faultlined object-view clustering - live</h1>
<div class="sub" id="sub">connecting...</div>
<div class="wrap">
  <div class="col">
    <div id="clusters"></div>
    <div id="log"></div>
  </div>
  <div class="col"><img id="treemap" width="560"><img id="map" width="560"></div>
  <div class="col"><img id="heatmap" width="420"></div>
</div>
<script>
let stamp = 0;
async function tick() {
  const state = await (await fetch('/state')).json();
  const cfg = state.config;
  const verbs = cfg.mask_verbs ? 'masked' : 'kept';
  document.getElementById('sub').textContent =
    `${cfg.encoder} | view=object | rule=${cfg.rule} | radius=${cfg.radius}`
    + ` | colour weight=${cfg.colour_weight} | verbs ${verbs}`
    + `  -  ${state.arrived} strings, ${state.clusters} clusters,`
    + ` ${state.frozen} frozen, ${state.elapsed}s`;
  const sizes = state.sizes;
  const html = Object.entries(sizes).sort((a,b) => b[1]-a[1]).map(([i, n]) => {
    const frozen = state.frozen_list.includes(Number(i));
    const name = state.labels[i];
    const cls = frozen ? 'frozen' : '';
    const button = frozen ? '' : ` <button onclick="confirm(${i})">confirm</button>`;
    return `<span class="chip ${cls}">${name ? name + ' ' : ''}c${i}:${n}`
      + (frozen ? ' &#10052;' : '') + button + '</span>';
  }).join('');
  document.getElementById('clusters').innerHTML = html || '<em>nothing yet</em>';
  const head = '<table><tr><th>arrived</th><th>task</th><th>outcome</th></tr>';
  const row = e => {
    const cls = e.into_frozen ? 'froz' : (e.outcome === 'new cluster' ? 'new' : '');
    const tail = e.into_frozen ? ' (frozen cluster)' : '';
    return `<tr><td>${e.at}s</td><td>${e.text}</td>`
      + `<td class="${cls}">${e.outcome}${tail}</td></tr>`;
  };
  document.getElementById('log').innerHTML = head
    + state.log.map(row).join('') + '</table>';
  stamp += 1;
  for (const name of ['treemap', 'map', 'heatmap']) {
    document.getElementById(name).src = `/${name}.svg?t=${stamp}`;
  }
}
async function confirm(i) {
  await fetch('/confirm/' + i, { method: 'POST' });
  tick();
}
tick();
setInterval(tick, 700);
</script></body></html>
"""


def serve(session: Session, port: int) -> None:
    """Serve the live view."""
    counter = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/":
                self._send(_PAGE.encode("utf-8"), "text/html; charset=utf-8")
                return
            if self.path == "/state":
                state = session.snapshot()
                with session.lock:
                    state["frozen_list"] = [
                        index
                        for index, cluster in enumerate(session.model.clusters)
                        if cluster.frozen
                    ]
                self._send(json.dumps(state).encode("utf-8"), "application/json")
                return
            name = self.path.split("?")[0].lstrip("/")
            if name.endswith(".svg"):
                counter["n"] += 1
                self._send(
                    session.figures()[name.removesuffix(".svg")].encode("utf-8"), "image/svg+xml"
                )
                return
            self.send_error(404)

        def do_POST(self) -> None:
            if self.path.startswith("/confirm/"):
                index = int(self.path.rsplit("/", 1)[-1])
                ok = session.confirm(index)
                self._send(b"ok" if ok else b"no such cluster", "text/plain")
                return
            self.send_error(404)

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"live view on http://127.0.0.1:{port} - Ctrl-C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        print("\nstopped")


SEED = (
    "pick up the red cube",
    "grab the red cube",
    "take the red cube",
    "pick up the blue cube",
    "pick up the red block",
    "push the red cube",
    "push the blue cube",
    "open the drawer",
    "close the drawer",
    "open the cabinet",
    "wipe the table",
    "wipe the counter",
    "press the red button",
    "press the blue button",
    "pour the milk",
    "pour the oil",
    "place the bowl on the plate",
    "put down the bowl",
    "sort the red blocks into the bin",
    "sort the blue blocks into the bin",
)


def run(source: str | None, port: int, **options: Any) -> int:
    """Start a session, feed it, and serve."""
    import sys

    session = Session(**options)
    if source and source != "-":
        with open(source, encoding="utf-8") as handle:
            session.feed_all(line for line in handle)
    else:
        session.feed_all(SEED)

    if source == "-" or not source:
        print(f"seeded with {len(session.texts)} strings")
        print("type a task string and press enter to watch it join; Ctrl-C to stop")

        def pump() -> None:
            for line in sys.stdin:
                session.feed(line)

        threading.Thread(target=pump, daemon=True).start()

    serve(session, port)
    return 0
