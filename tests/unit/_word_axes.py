"""A deterministic stand-in for a sentence encoder, shared by the clustering tests."""

from __future__ import annotations

import zlib

import numpy as np

DIM = 256


class WordAxes:
    """Word-per-axis encoder."""

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
