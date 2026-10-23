"""Core API for writing and reading facts in the universal schema."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path: str | Path = ":memory:") -> duckdb.DuckDBPyConnection:
    """Open a database and make sure the core schema exists."""
    con = duckdb.connect(str(path))
    con.execute(SCHEMA_PATH.read_text())
    return con


def register_source(con, source_id: str, name: str, license: str, commercial_use: bool,
                    homepage_url: str | None = None, license_url: str | None = None,
                    attribution: str | None = None) -> None:
    con.execute(
        """INSERT OR REPLACE INTO sources VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [source_id, name, homepage_url, license, license_url, commercial_use, attribution],
    )


def define_metric(con, metric_id: str, domain: str, name: str, description: str,
                  unit: str | None = None, higher_is_better: bool | None = None,
                  definition: str | None = None) -> None:
    con.execute(
        """INSERT OR REPLACE INTO metrics VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [metric_id, domain, name, description, unit, higher_is_better, definition],
    )


def upsert_entity(con, entity_id: str, domain: str, entity_type: str, canonical_name: str,
                  attributes: dict | None = None) -> None:
    con.execute(
        """INSERT INTO entities (entity_id, domain, entity_type, canonical_name, attributes)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (entity_id) DO UPDATE SET
               canonical_name = excluded.canonical_name,
               attributes = excluded.attributes""",
        [entity_id, domain, entity_type, canonical_name, json.dumps(attributes or {})],
    )


def add_identifier(con, entity_id: str, scheme: str, identifier: str,
                   source_id: str | None = None) -> None:
    con.execute(
        """INSERT OR REPLACE INTO entity_identifiers (entity_id, scheme, identifier, source_id)
           VALUES (?, ?, ?, ?)""",
        [entity_id, scheme, identifier, source_id],
    )


def resolve(con, scheme: str, identifier: str) -> str | None:
    """Map an outside ID (e.g. a Cricsheet ID) to our canonical entity ID."""
    row = con.execute(
        "SELECT entity_id FROM entity_identifiers WHERE scheme = ? AND identifier = ?",
        [scheme, identifier],
    ).fetchone()
    return row[0] if row else None


def start_run(con, source_id: str, pipeline: str, pipeline_version: str) -> int:
    return con.execute(
        """INSERT INTO ingestion_runs (source_id, pipeline, pipeline_version)
           VALUES (?, ?, ?) RETURNING run_id""",
        [source_id, pipeline, pipeline_version],
    ).fetchone()[0]


def finish_run(con, run_id: int, rows_written: int, status: str = "success") -> None:
    con.execute(
        """UPDATE ingestion_runs SET finished_at = current_timestamp, status = ?, rows_written = ?
           WHERE run_id = ?""",
        [status, rows_written, run_id],
    )


def record_observation(con, entity_id: str, metric_id: str, value: float | None, *,
                       source_id: str, period_label: str | None = None,
                       period_start: date | None = None, period_end: date | None = None,
                       context: dict | None = None, value_text: str | None = None,
                       unit: str | None = None, source_url: str | None = None,
                       fetched_at: datetime | None = None, run_id: int | None = None) -> int:
    """Write one fact. If a current value already exists for the same entity, metric,
    period, context and source and it differs, the old one is superseded, not deleted."""
    license_ = con.execute(
        "SELECT license FROM sources WHERE source_id = ?", [source_id]
    ).fetchone()
    if license_ is None:
        raise ValueError(f"Unknown source {source_id!r}; register it first")
    context_json = json.dumps(context or {}, sort_keys=True)

    existing = con.execute(
        """SELECT obs_id, value, value_text FROM current_observations
           WHERE entity_id = ? AND metric_id = ? AND source_id = ?
             AND period_label IS NOT DISTINCT FROM ?
             AND context::VARCHAR = ?""",
        [entity_id, metric_id, source_id, period_label, context_json],
    ).fetchone()
    if existing and existing[1] == value and existing[2] == value_text:
        return existing[0]
    if existing:
        con.execute(
            "UPDATE observations SET superseded_at = current_timestamp WHERE obs_id = ?",
            [existing[0]],
        )

    return con.execute(
        """INSERT INTO observations
             (entity_id, metric_id, period_start, period_end, period_label, context,
              value, value_text, unit, source_id, source_url, fetched_at, run_id, license)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING obs_id""",
        [entity_id, metric_id, period_start, period_end, period_label, context_json,
         value, value_text, unit, source_id, source_url,
         fetched_at or datetime.now(timezone.utc), run_id, license_[0]],
    ).fetchone()[0]


def get_facts(con, entity_id: str, metric_id: str | None = None,
              include_history: bool = False) -> list[dict]:
    """Read facts with their full provenance attached."""
    table = "observations" if include_history else "current_observations"
    sql = f"""
        SELECT o.entity_id, e.canonical_name, o.metric_id, o.period_label, o.context,
               o.value, o.value_text, o.unit, o.source_id, s.name AS source_name,
               o.source_url, o.fetched_at, o.license, s.commercial_use, s.attribution,
               o.confidence, o.recorded_at, o.superseded_at, o.run_id
        FROM {table} o
        JOIN entities e USING (entity_id)
        JOIN sources s USING (source_id)
        WHERE o.entity_id = ?"""
    params: list = [entity_id]
    if metric_id:
        sql += " AND o.metric_id = ?"
        params.append(metric_id)
    sql += " ORDER BY o.metric_id, o.period_label, o.recorded_at"
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
