"""Live schema introspection for the `/ui/schema` page (ADR 0024)."""

from __future__ import annotations

from typing import Any, cast

from data_engine.catalog.database import connect
from data_engine.config import Settings

__all__ = ["ColumnInfo", "ForeignKey", "TableInfo", "describe"]

_COLUMNS_SQL = """
SELECT c.table_name,
       c.column_name,
       c.ordinal_position,
       c.data_type,
       c.is_nullable,
       c.column_default,
       COALESCE(pk.is_primary, false) AS is_primary
FROM information_schema.columns c
JOIN information_schema.tables t
  ON t.table_schema = c.table_schema AND t.table_name = c.table_name
LEFT JOIN (
    SELECT kcu.table_name, kcu.column_name, true AS is_primary
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON kcu.constraint_name = tc.constraint_name
     AND kcu.table_schema = tc.table_schema
    WHERE tc.constraint_type = 'PRIMARY KEY'
      AND tc.table_schema = 'public'
) pk ON pk.table_name = c.table_name AND pk.column_name = c.column_name
WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
ORDER BY c.table_name, c.ordinal_position
"""

_FOREIGN_KEYS_SQL = """
SELECT tc.table_name AS source,
       kcu.column_name AS source_column,
       ccu.table_name AS target,
       ccu.column_name AS target_column
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
JOIN information_schema.constraint_column_usage ccu
  ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
ORDER BY tc.table_name, kcu.column_name
"""


class ColumnInfo:
    """One column."""

    __slots__ = ("data_type", "default", "name", "nullable", "position", "primary")

    def __init__(self, row: dict[str, Any]) -> None:
        self.name = str(row["column_name"])
        self.position = int(row["ordinal_position"])
        self.data_type = str(row["data_type"])
        self.nullable = str(row["is_nullable"]).upper() == "YES"
        self.default = row["column_default"]
        self.primary = bool(row["is_primary"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "position": self.position,
            "data_type": self.data_type,
            "nullable": self.nullable,
            "default": self.default,
            "primary": self.primary,
        }


class ForeignKey:
    __slots__ = ("source", "source_column", "target", "target_column")

    def __init__(self, row: dict[str, Any]) -> None:
        self.source = str(row["source"])
        self.source_column = str(row["source_column"])
        self.target = str(row["target"])
        self.target_column = str(row["target_column"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_column": self.source_column,
            "target": self.target,
            "target_column": self.target_column,
        }


class TableInfo:
    __slots__ = ("columns", "name", "referenced_by", "row_count")

    def __init__(self, name: str) -> None:
        self.name = name
        self.columns: list[ColumnInfo] = []
        self.row_count = 0
        self.referenced_by: list[str] = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "row_count": self.row_count,
            "columns": [column.to_dict() for column in self.columns],
            "referenced_by": sorted(self.referenced_by),
        }


def describe(settings: Settings | None = None) -> dict[str, Any]:
    """The live catalog shape: tables, columns, keys, row counts."""
    try:
        with connect(settings) as connection:
            tables: dict[str, TableInfo] = {}
            for row in connection.execute(_COLUMNS_SQL).fetchall():
                table = tables.setdefault(str(row["table_name"]), TableInfo(str(row["table_name"])))
                table.columns.append(ColumnInfo(row))

            foreign_keys: list[ForeignKey] = []
            for row in connection.execute(_FOREIGN_KEYS_SQL).fetchall():
                key = ForeignKey(row)
                foreign_keys.append(key)
                if key.target in tables and key.source not in tables[key.target].referenced_by:
                    tables[key.target].referenced_by.append(key.source)

            for name, table in tables.items():
                counted = connection.execute(f'SELECT count(*) AS n FROM "{name}"').fetchone()
                table.row_count = cast(int, counted["n"]) if counted else 0
    except Exception as error:
        return {
            "reachable": False,
            "error": f"{type(error).__name__}: {error}",
            "tables": [],
            "foreign_keys": [],
            "table_count": 0,
            "column_count": 0,
        }

    ordered = [tables[name] for name in sorted(tables)]
    return {
        "reachable": True,
        "error": "",
        "tables": [table.to_dict() for table in ordered],
        "foreign_keys": [key.to_dict() for key in foreign_keys],
        "table_count": len(ordered),
        "column_count": sum(len(table.columns) for table in ordered),
    }
