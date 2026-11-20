"""Metadata catalog model and loaders (CSV, Snowflake, Databricks Unity Catalog).

Both organizations supply one row per column using the same contract:

    PLATFORM, TABLE_PHYSICAL_NAME, TABLE_LOGICAL_NAME, TABLE_DESCRIPTION,
    COLUMN_PHYSICAL_NAME, COLUMN_LOGICAL_NAME, COLUMN_DESCRIPTION,
    COLUMN_DATATYPE, COLUMN_LENGTH

Table-level fields repeat on every column row. A table with no column metadata
uses one row with an empty COLUMN_PHYSICAL_NAME. PLATFORM defaults to SNOWFLAKE.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .text import short_name

FIELDS = ("PLATFORM", "TABLE_PHYSICAL_NAME", "TABLE_LOGICAL_NAME", "TABLE_DESCRIPTION",
          "COLUMN_PHYSICAL_NAME", "COLUMN_LOGICAL_NAME", "COLUMN_DESCRIPTION",
          "COLUMN_DATATYPE", "COLUMN_LENGTH")
PLATFORMS = {"SNOWFLAKE", "DATABRICKS"}


@dataclass
class Column:
    name: str
    logical_name: str | None = None
    description: str | None = None
    data_type: str | None = None
    length: int | None = None
    table: "Table | None" = field(default=None, repr=False, compare=False)

    @property
    def ref(self) -> str:
        return f"{self.table.name}.{self.name}" if self.table else self.name

    def to_dict(self) -> dict:
        return dict(name=self.name, logical_name=self.logical_name, description=self.description,
                    data_type=self.data_type, length=self.length)


@dataclass
class Table:
    name: str
    platform: str = "SNOWFLAKE"
    logical_name: str | None = None
    description: str | None = None
    columns: list[Column] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.platform}:{self.name}"

    @property
    def short_name(self) -> str:
        return short_name(self.name)

    def column(self, name: str) -> Column | None:
        key = name.strip().casefold()
        exact = [c for c in self.columns if c.name.casefold() == key]
        return (exact or [c for c in self.columns if (c.logical_name or "").casefold() == key]
                or [None])[0]

    def to_dict(self, columns: bool = False) -> dict:
        data = dict(id=self.id, name=self.name, platform=self.platform,
                    logical_name=self.logical_name, description=self.description,
                    column_count=len(self.columns))
        if columns:
            data["columns"] = [c.to_dict() for c in self.columns]
        return data


class Catalog:
    def __init__(self, tables: Iterable[Table], label: str = "catalog"):
        self.label = label
        self.tables = list(tables)
        seen = set()
        for table in self.tables:
            if table.id in seen:
                raise ValueError(f"Duplicate table {table.id} in {label}")
            seen.add(table.id)
            names = [c.name.casefold() for c in table.columns]
            if len(names) != len(set(names)):
                raise ValueError(f"Duplicate column name in {table.id}")
            for column in table.columns:
                column.table = table

    def __len__(self) -> int:
        return len(self.tables)

    @property
    def columns(self) -> list[Column]:
        return [c for t in self.tables for c in t.columns]

    def find_table(self, text: str, platform: str | None = None) -> Table:
        """Resolve a table by full physical name, short name, or logical name."""
        term = text.strip().casefold()
        if not term:
            raise ValueError("Enter a table name.")
        pool = [t for t in self.tables if not platform or t.platform == platform.upper()]
        for test in (lambda t: t.name.casefold() == term or t.id.casefold() == term,
                     lambda t: t.short_name.casefold() == term,
                     lambda t: (t.logical_name or "").casefold() == term):
            hits = [t for t in pool if test(t)]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise ValueError(f"'{text}' is ambiguous: "
                                 + ", ".join(sorted(h.id for h in hits)))
        raise ValueError(f"Table '{text}' was not found in the {self.label} catalog.")

    # ---------------------------------------------------------------- loaders
    @classmethod
    def from_rows(cls, rows: Iterable[dict], label: str = "catalog") -> "Catalog":
        tables: dict[tuple[str, str], Table] = {}
        for raw in rows:
            row = {k.strip().upper(): (v.strip() if isinstance(v, str) else v)
                   for k, v in raw.items() if k}
            physical = row.get("TABLE_PHYSICAL_NAME")
            if not physical:
                raise ValueError(f"Row without TABLE_PHYSICAL_NAME in {label}")
            platform = (row.get("PLATFORM") or "SNOWFLAKE").upper()
            if platform not in PLATFORMS:
                raise ValueError(f"Unsupported platform {platform!r} for {physical}")
            table = tables.setdefault((platform, physical), Table(
                name=physical, platform=platform, logical_name=row.get("TABLE_LOGICAL_NAME") or None,
                description=row.get("TABLE_DESCRIPTION") or None))
            column = row.get("COLUMN_PHYSICAL_NAME")
            if column:
                length = row.get("COLUMN_LENGTH")
                table.columns.append(Column(
                    name=column, logical_name=row.get("COLUMN_LOGICAL_NAME") or None,
                    description=row.get("COLUMN_DESCRIPTION") or None,
                    data_type=row.get("COLUMN_DATATYPE") or None,
                    length=int(float(length)) if length not in (None, "") else None))
        return cls(tables.values(), label)

    @classmethod
    def from_csv(cls, path: str | Path, label: str | None = None) -> "Catalog":
        with open(path, newline="", encoding="utf-8") as handle:
            return cls.from_rows(csv.DictReader(handle), label or Path(path).stem)

    @classmethod
    def from_snowflake(cls, connection: str, table: str, label: str) -> "Catalog":
        """Read a metadata export table through a named Snowflake connection."""
        import snowflake.connector  # optional dependency

        with snowflake.connector.connect(connection_name=connection, paramstyle="qmark") as conn:
            with conn.cursor(snowflake.connector.DictCursor) as cursor:
                cursor.execute("SELECT * FROM IDENTIFIER(?)", (table,))
                return cls.from_rows(cursor.fetchall(), label)

    @classmethod
    def from_databricks(cls, catalogs: list[str], label: str, *, profile: str | None = None,
                        warehouse_id: str | None = None, schemas: list[str] | None = None) -> "Catalog":
        """Harvest metadata straight from Unity Catalog information_schema via a SQL warehouse."""
        from .connectors import databricks_rows

        return cls.from_rows(databricks_rows(catalogs, profile=profile, warehouse_id=warehouse_id,
                                             schemas=schemas), label)
