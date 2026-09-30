"""Text encoders, behind one interface, so the choice of model is a data point.

Every experiment in this package takes an `Encoder` and never imports a model
library itself. That keeps the factorial honest: the same clustering code runs
against every backend, so a difference in the results is a difference in the
embeddings rather than in how each backend was wired up.

Three backends are registered. Only the second is a real semantic encoder; the
others exist to answer different questions, and all three are cheap:

- `null`    - deterministic and dependency-free. Not semantic. It exists so the
              harness, the metrics and the tests can run with nothing installed,
              and so a bug in the plumbing shows up as a *bad score* rather than
              as an import error. It must never be reported as a method result.
- `m2v-8m`  - a static 8M-parameter distilled encoder via `model2vec`. No torch,
              no ONNX runtime, CPU only, which is what makes the experiments
              runnable on the captured hardware at all.
- `m2v-32m` - the same family at 4x the parameters. Included to measure whether
              the extra capacity buys anything for short English task strings,
              which is exactly the kind of assumption worth testing rather than
              assuming. A multilingual sibling was tried first and dropped: its
              hub snapshot ships incomplete, so it cannot be loaded reproducibly.

Embeddings are L2-normalised on the way out, so cosine similarity is a plain dot
product and every downstream distance is well-behaved.

`fingerprint` exists because NFR-004 treats build reproducibility as
release-blocking. A model fetched from a hub at run time is a moving target: the
same episodes would cluster differently if upstream re-published weights. Each
backend therefore exposes a content hash, and results record it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

#: Encoders return float32: half the memory of float64, and cosine similarity is
#: not precision-limited at this scale. Underscores the "no heavy stack" goal.
DTYPE = np.float32


@runtime_checkable
class Encoder(Protocol):
    """Maps task strings to unit-norm vectors.

    `name` and `dim` are properties rather than attributes so that a frozen
    dataclass satisfies the protocol. A mutable attribute would require
    implementations to support assignment, and the only attribute-carrying
    implementation here is deliberately frozen.
    """

    @property
    def name(self) -> str:
        """Short, stable identifier used in result filenames and report tables."""
        ...

    @property
    def dim(self) -> int:
        """Vector width, so callers can size buffers without encoding a probe."""
        ...

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        """Return an ``(len(texts), dim)`` array of unit-norm float32 rows."""
        ...

    def fingerprint(self) -> str:
        """Content hash of the weights, for reproducibility of a recorded result."""
        ...


def _l2_normalise(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    # A zero row is a legitimately uninformative input (empty string); leave it
    # zero rather than producing NaN and poisoning every distance downstream.
    safe = np.where(norms == 0.0, 1.0, norms)
    return (matrix / safe).astype(DTYPE, copy=False)


@dataclass(frozen=True, slots=True)
class NullEncoder:
    """Deterministic pseudo-embeddings. Not semantic, and never a result.

    Words are hashed into a fixed number of buckets and hashed again per bucket
    so that two texts sharing a word land in a partly predictable direction.
    That is enough to exercise the harness end to end, and its *lack* of
    semantic quality is itself a useful control: if a clustering method scores
    well here, it is scoring on something other than meaning.
    """

    name: str = "null"
    dim: int = 64
    seed: int = 0

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=DTYPE)
        for row, text in enumerate(texts):
            for word in text.lower().split():
                digest = hashlib.blake2b(word.encode("utf-8"), digest_size=8).digest()
                value = int.from_bytes(digest, "big")
                bucket = value % self.dim
                sign = 1.0 if (value >> 17) & 1 else -1.0
                out[row, bucket] += sign
        return _l2_normalise(out)

    def fingerprint(self) -> str:
        return f"null-v1-dim{self.dim}-seed{self.seed}"


class Model2VecEncoder:
    """A static distilled encoder loaded through `model2vec`.

    Deliberately not a sentence-transformer pipeline. A static model is a lookup
    table plus a weighted average, so it needs no forward pass, no torch, and no
    GPU, and it encodes a few thousand task strings in well under a second.
    """

    def __init__(self, model_id: str, name: str) -> None:
        from model2vec import StaticModel  # imported lazily: optional dependency

        self._name = name
        self._model_id = model_id
        self._model = StaticModel.from_pretrained(model_id)
        self._dim = int(self._model.dim)

    @property
    def name(self) -> str:
        return self._name

    @property
    def dim(self) -> int:
        return self._dim

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=DTYPE)
        return _l2_normalise(self._model.encode(list(texts)))

    def fingerprint(self) -> str:
        """Hash the actual vectors, not just the model id.

        A hub model can be re-published under the same name. Hashing the weights
        means a recorded result can be checked against the weights that produced
        it, which is the only way NFR-004's reproducibility claim survives a
        dependency that is fetched at run time.
        """
        vectors = self._model.embedding
        digest = hashlib.blake2b(digest_size=16)
        digest.update(vectors.dtype.str.encode("utf-8"))
        digest.update(str(vectors.shape).encode("utf-8"))
        digest.update(np.ascontiguousarray(vectors).tobytes())
        return f"{self._model_id}@{digest.hexdigest()}"


@dataclass(frozen=True, slots=True)
class Backend:
    """A named way to build an encoder, and whether it can be built here."""

    name: str
    build: Callable[[], Encoder]
    requires: str

    def available(self) -> bool:
        try:
            self.build()
        except Exception:
            return False
        return True


def _build_m2v_8m() -> Encoder:
    return Model2VecEncoder("minishlab/potion-base-8M", "m2v-8m")


def _build_m2v_32m() -> Encoder:
    return Model2VecEncoder("minishlab/potion-base-32M", "m2v-32m")


#: Ordered so the cheap, dependency-free backend is always first and results are
#: comparable before any heavier backend is installed.
BACKENDS: tuple[Backend, ...] = (
    Backend("null", lambda: NullEncoder(), "none"),
    Backend("m2v-8m", _build_m2v_8m, "model2vec"),
    Backend("m2v-32m", _build_m2v_32m, "model2vec"),
)


def get(name: str) -> Encoder:
    """Build a backend by name, with a message that says what to install."""
    for backend in BACKENDS:
        if backend.name == name:
            try:
                return backend.build()
            except Exception as error:
                raise RuntimeError(
                    f"embedding backend {name!r} needs {backend.requires!r}: {error}"
                ) from error
    known = ", ".join(b.name for b in BACKENDS)
    raise KeyError(f"unknown embedding backend {name!r}; known backends: {known}")


def available() -> tuple[str, ...]:
    """Names of backends that build successfully in this environment."""
    return tuple(backend.name for backend in BACKENDS if backend.available())


def encode_all(encoder: Encoder, texts: Sequence[str]) -> np.ndarray:
    """Encode, asserting the contract every downstream step relies on."""
    matrix = np.asarray(encoder.encode(texts), dtype=DTYPE)
    expected = (len(texts), encoder.dim)
    if matrix.shape != expected:
        raise ValueError(f"{encoder.name} returned {matrix.shape}, expected {expected}")
    if not np.isfinite(matrix).all():
        raise ValueError(f"{encoder.name} produced a non-finite embedding")
    return matrix


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two unit-norm rows, with a guard for zero vectors."""
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b / denominator) if denominator > 0.0 else 0.0


def is_semantic(encoder: Encoder) -> bool:
    """False for the null backend, so a report cannot present it as a result."""
    return not isinstance(encoder, NullEncoder)


def describe(encoder: Encoder) -> dict[str, object]:
    """Provenance for a result record."""
    return {
        "backend": encoder.name,
        "dim": encoder.dim,
        "fingerprint": encoder.fingerprint(),
        "semantic": is_semantic(encoder),
        "model": None if isinstance(encoder, NullEncoder) else encoder.fingerprint(),
    }
