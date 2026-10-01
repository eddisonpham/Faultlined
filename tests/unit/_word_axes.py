"""A deterministic stand-in for a sentence encoder, shared by the clustering tests.

Every distinct word gets its own axis with weight 1, so cosine similarity between two
sentences is the fraction of words they share. Nothing about a model's training run can
change an outcome here: what is under test is the method, not the encoder, and these
tests must keep passing on a machine with no model weights and no network.
"""

from __future__ import annotations

import zlib

import numpy as np

#: Wide enough that the clustering test vocabulary never collides on an axis.
DIM = 256


class WordAxes:
    """Word-per-axis encoder. Deterministic, dependency-free, and deliberately dull."""

    name = "word_axes"
    dim = DIM

    def encode(self, texts: object) -> np.ndarray:
        rows = []
        for text in texts:  # type: ignore[union-attr]
            vector = np.zeros(DIM, dtype=np.float32)
            for word in str(text).lower().split():
                vector[zlib.crc32(word.encode()) % DIM] += 1.0
            norm = float(np.linalg.norm(vector))
            rows.append(vector / norm if norm else vector)
        return np.stack(rows)

    def fingerprint(self) -> str:
        return "word-axes"
