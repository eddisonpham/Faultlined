"""Repository boundary; PostgreSQL implementation is introduced in phase 05."""

from typing import Any, Protocol


class Catalog(Protocol):
    """Transactional persistence boundary for catalog records."""

    def get(self, table: str, key: str) -> dict[str, Any] | None: ...

    def insert(self, table: str, values: dict[str, Any]) -> dict[str, Any]: ...
