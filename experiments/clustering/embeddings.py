"""Text encoders, behind one interface, so the choice of model is a data point."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

DTYPE = np.float32


@runtime_checkable
class Encoder(Protocol):
    """Maps task strings to unit-norm vectors."""

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
    safe = np.where(norms == 0.0, 1.0, norms)
    return (matrix / safe).astype(DTYPE, copy=False)


@dataclass(frozen=True, slots=True)
class NullEncoder:
    """Deterministic pseudo-embeddings."""

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


class SentenceTransformerEncoder:
    """A contextual encoder: MiniLM, run on CPU through `sentence-transformers`."""

    def __init__(self, model_id: str, name: str) -> None:
        from sentence_transformers import SentenceTransformer

        self._name = name
        self._model_id = model_id
        self._model = SentenceTransformer(model_id, device="cpu")
        dimension = self._model.get_embedding_dimension()
        if dimension is None:
            raise ValueError(f"{model_id} reported no embedding dimension")
        self._dim = int(dimension)

    @property
    def name(self) -> str:
        return self._name

    @property
    def dim(self) -> int:
        return self._dim

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self._dim), dtype=DTYPE)
        vectors = self._model.encode(
            list(texts), convert_to_numpy=True, normalize_embeddings=False, show_progress_bar=False
        )
        return _l2_normalise(np.asarray(vectors, dtype=np.float32))

    def fingerprint(self) -> str:
        """Hash the actual weight tensors, not the model name."""
        digest = hashlib.blake2b(digest_size=16)
        digest.update(self._model_id.encode("utf-8"))
        state = self._model.state_dict()
        for key in sorted(state):
            tensor = state[key]
            digest.update(key.encode("utf-8"))
            digest.update(str(tuple(tensor.shape)).encode("utf-8"))
            digest.update(np.ascontiguousarray(tensor.detach().cpu().numpy()).tobytes())
        return f"{self._model_id}@{digest.hexdigest()}"


class Model2VecEncoder:
    """A static distilled encoder loaded through `model2vec`."""

    def __init__(self, model_id: str, name: str) -> None:
        from model2vec import StaticModel

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
        """Hash the actual vectors, not just the model id."""
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


def _build_minilm() -> Encoder:
    return SentenceTransformerEncoder("sentence-transformers/all-MiniLM-L6-v2", "minilm")


BACKENDS: tuple[Backend, ...] = (
    Backend("null", lambda: NullEncoder(), "none"),
    Backend("m2v-8m", _build_m2v_8m, "model2vec"),
    Backend("m2v-32m", _build_m2v_32m, "model2vec"),
    Backend("minilm", _build_minilm, "sentence-transformers"),
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


def is_contextual(encoder: Encoder) -> bool:
    """True for a neural forward pass, False for a static or null encoder."""
    return isinstance(encoder, SentenceTransformerEncoder)


_PROBE_SAME = ("pick up the red cube", "pick up the red cube")
_PROBE_DIFFERENT = ("pick up the red cube", "database index rebuild")


@dataclass(frozen=True, slots=True)
class Verification:
    """Whether an encoder can be trusted with a benchmark at all."""

    ok: bool
    checks: tuple[str, ...]
    failures: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "checks": list(self.checks), "failures": list(self.failures)}


def verify(encoder: Encoder) -> Verification:
    """Cheap sanity checks, run before an encoder's result is recorded."""
    failures: list[str] = []
    checks: list[str] = []

    probe = list(_PROBE_SAME) + list(_PROBE_DIFFERENT)
    matrix = encode_all(encoder, probe)
    checks.append("shape and finiteness")

    if np.linalg.norm(matrix[0] - matrix[1]) > 1e-5:
        failures.append("identical strings produced different vectors")
    if np.linalg.norm(matrix[2] - matrix[3]) < 1e-5:
        failures.append("unrelated strings produced identical vectors")

    repeat = encode_all(encoder, probe)
    checks.append("determinism")
    if not np.allclose(matrix, repeat, atol=1e-6):
        failures.append("encoding is not deterministic across calls")

    same = cosine(matrix[0], matrix[1])
    different = cosine(matrix[2], matrix[3])
    checks.append("orders identical above unrelated")
    if same < 0.999:
        failures.append(f"identical strings scored {same:.4f}, expected ~1.0")
    if different >= same:
        failures.append(
            f"unrelated strings scored {different:.4f}, at or above the "
            f"identical-pair score {same:.4f}"
        )

    return Verification(ok=not failures, checks=tuple(checks), failures=tuple(failures))


def describe(encoder: Encoder) -> dict[str, object]:
    """Provenance for a result record."""
    return {
        "backend": encoder.name,
        "dim": encoder.dim,
        "fingerprint": encoder.fingerprint(),
        "semantic": is_semantic(encoder),
        "contextual": is_contextual(encoder),
        "model": None if isinstance(encoder, NullEncoder) else encoder.fingerprint(),
    }
