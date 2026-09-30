"""Live schema introspection for the `/ui/schema` page (ADR 0024).

Reads the catalog that is actually running rather than a diagram somebody
maintained. That is the whole point: a hand-written schema picture is a
snapshot of what was true the day it was drawn, it never shows the column that
was added last month without a migration, and nobody notices it has drifted
until it is load-bearing. These queries cannot drift, because they are the
database asking about itself.

Three things are read, all from `pg_catalog`:

- **columns** — name, type, nullability, default, and whether the column is a
  primary key. Enough to answer "what is in here" without a second query per
  table.
- **foreign keys** — the real constraints, which is what turns a list of
  fourteen tables into a graph an operator can navigate.
- **row counts** — live, from a per-table count. The catalog is local-scale by
  design (ADR 0005), so an exact count is cheap and far more useful than the
  `reltuples` estimate PostgreSQL keeps, which is deliberately approximate and
  would put `0` or `-1` on a page whose entire purpose is to say what is in
  here.

`describe()` returns a plain dict and never raises for a missing catalog: an
operator opening this page with the database down gets an explicit "catalog
unreachable" state, because a schema view that 500s is worse than no schema
view.
"""

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
    """One column. A plain class rather than a dataclass: it never leaves the
    process and a frozen dataclass here would be ceremony."""

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
    """The live catalog shape: tables, columns, keys, row counts.

    Returns `{"reachable": False, ...}` rather than raising when the catalog
    cannot be reached, so the page can say so in its own words.
    """
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
                # `source` holds the foreign key, `target` the table it points
                # at. "Referenced by" belongs to the *target*: `episodes` is
                # referenced by `episode_quality`, not the other way round, and
                # reading that backwards tells an operator which side owns the
                # relationship - which is the only reason the column exists.
                if key.target in tables and key.source not in tables[key.target].referenced_by:
                    tables[key.target].referenced_by.append(key.source)

            for name, table in tables.items():
                # Identifiers come from information_schema, not from a request, so
                # they cannot carry an injection. The quoting is belt and braces
                # against a table named `order` or `user`.
                counted = connection.execute(f'SELECT count(*) AS n FROM "{name}"').fetchone()
                # `count(*)` is bigint, which psycopg hands back as a Python int;
                # the row type is `object` because dict rows are untyped.
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
