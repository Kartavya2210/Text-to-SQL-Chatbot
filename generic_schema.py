"""
generic_schema.py — schema discovery for GENERIC_MODE.

The main app is tuned for one specific citizen database: it has hand-written
templates, a synonym map, a location guard and a vocabulary gate, all of which
assume that schema. Generic mode switches those off and instead learns the
schema from whatever SQLite file the app is pointed at.

Design notes:
- Value sampling is capped hard (few columns, few values) so a huge table does
  not turn startup into a full scan. Sampling uses LIMIT on distinct values.
- Everything here is read-only.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from sqlalchemy import create_engine, text as sa_text

logger = logging.getLogger("generic_schema")

# Hard caps so introspection stays cheap on large databases.
_MAX_SAMPLE_COLUMNS = 12     # at most this many text columns get value samples
_MAX_SAMPLE_VALUES = 8       # at most this many distinct values per column
_SAMPLE_CARDINALITY = 40     # only sample columns with <= this many distinct values
_SAMPLE_SCAN_LIMIT = 5000    # rows scanned when estimating distinct values


@dataclass
class ColumnInfo:
    name: str
    type: str
    samples: list[str] = field(default_factory=list)


@dataclass
class TableInfo:
    name: str
    columns: list[ColumnInfo]
    row_count: int = 0


@dataclass
class DBSchema:
    tables: list[TableInfo]
    primary_table: str

    def column_names(self, table: str | None = None) -> list[str]:
        t = table or self.primary_table
        for tab in self.tables:
            if tab.name == t:
                return [c.name for c in tab.columns]
        return []


def _quote_ident(name: str) -> str:
    """Safely quote a SQLite identifier for use in a query."""
    return '"' + name.replace('"', '""') + '"'


def introspect(db_uri: str, preferred_table: str | None = None) -> DBSchema:
    """Inspect a SQLite database and return its schema with sampled values."""
    if not db_uri.startswith("sqlite"):
        raise ValueError("generic_schema supports SQLite databases only.")

    engine = create_engine(db_uri)
    tables: list[TableInfo] = []

    with engine.connect() as conn:
        names = [r[0] for r in conn.execute(sa_text(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ))]
        if not names:
            raise ValueError("No tables found in the database.")

        for tname in names:
            cols = []
            for row in conn.execute(sa_text(f"PRAGMA table_info({_quote_ident(tname)})")):
                # PRAGMA table_info → (cid, name, type, notnull, dflt_value, pk)
                cols.append(ColumnInfo(name=row[1], type=(row[2] or "").upper()))
            try:
                n = conn.execute(sa_text(
                    f"SELECT COUNT(*) FROM {_quote_ident(tname)}"
                )).scalar() or 0
            except Exception:
                n = 0
            tables.append(TableInfo(name=tname, columns=cols, row_count=n))

        # Pick the primary table: explicit choice, else the only one, else biggest.
        if preferred_table and any(t.name == preferred_table for t in tables):
            primary = preferred_table
        elif len(tables) == 1:
            primary = tables[0].name
        else:
            primary = max(tables, key=lambda t: t.row_count).name

        ptab = next(t for t in tables if t.name == primary)

        # Sample distinct values from low-cardinality text columns, so the model
        # is told e.g. "region can be North/South/East/West" with no hand-tuning.
        sampled = 0
        for col in ptab.columns:
            if sampled >= _MAX_SAMPLE_COLUMNS:
                break
            # only sample text-like columns; skip numeric/date types whose distinct
            # values are data points, not categories worth listing for the model
            t = col.type.upper()
            is_numeric = any(k in t for k in ("INT", "REAL", "FLOA", "DOUB", "NUM", "DEC"))
            is_textlike = (t == "" or t == "TEXT" or any(k in t for k in ("CHAR", "CLOB")))
            if is_numeric or not is_textlike:
                continue
            try:
                vals = [r[0] for r in conn.execute(sa_text(
                    f"SELECT DISTINCT {_quote_ident(col.name)} "
                    f"FROM (SELECT {_quote_ident(col.name)} FROM {_quote_ident(primary)} "
                    f"LIMIT {_SAMPLE_SCAN_LIMIT}) "
                    f"WHERE {_quote_ident(col.name)} IS NOT NULL "
                    f"LIMIT {_SAMPLE_CARDINALITY + 1}"
                ))]
            except Exception as e:
                logger.debug("Sampling failed for %s.%s: %s", primary, col.name, e)
                continue
            if 0 < len(vals) <= _SAMPLE_CARDINALITY:
                col.samples = [str(v)[:40] for v in vals[:_MAX_SAMPLE_VALUES]]
                sampled += 1

    return DBSchema(tables=tables, primary_table=primary)


def build_schema_hint(schema: DBSchema) -> str:
    """Render the discovered schema as the prompt block the SQL model receives."""
    lines: list[str] = []
    ptab = next(t for t in schema.tables if t.name == schema.primary_table)

    lines.append(f"The database has a table named {ptab.name} with these columns:")
    for c in ptab.columns:
        desc = f"  - {c.name} ({c.type or 'TEXT'})"
        if c.samples:
            desc += "  e.g. " + ", ".join(repr(s) for s in c.samples)
        lines.append(desc)

    others = [t for t in schema.tables if t.name != schema.primary_table]
    if others:
        lines.append("")
        lines.append("Other tables available for joins:")
        for t in others:
            lines.append(f"  - {t.name}({', '.join(c.name for c in t.columns)})")

    lines.append("")
    lines.append(f"Query the {ptab.name} table. Use ONLY the column names listed above — "
                 f"never invent a column. When filtering a text column, use one of the "
                 f"example values shown for it, matching its exact spelling and case.")
    return "\n".join(lines)
