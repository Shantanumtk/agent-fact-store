"""Core API for writing and reading facts in the universal schema."""

from __future__ import annotations

import json
import tempfile
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


def upsert_entities(con, rows: list[dict]) -> None:
    """Bulk version of upsert_entity. Each row has the same keys as its arguments."""
    con.executemany(
        """INSERT INTO entities (entity_id, domain, entity_type, canonical_name, attributes)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (entity_id) DO UPDATE SET
               canonical_name = excluded.canonical_name,
               attributes = excluded.attributes""",
        [[r["entity_id"], r["domain"], r["entity_type"], r["canonical_name"],
          json.dumps(r.get("attributes") or {})] for r in rows],
    )


def add_identifiers(con, rows: list[dict]) -> None:
    """Bulk version of add_identifier."""
    con.executemany(
        """INSERT OR REPLACE INTO entity_identifiers (entity_id, scheme, identifier, source_id)
           VALUES (?, ?, ?, ?)""",
        [[r["entity_id"], r["scheme"], r["identifier"], r.get("source_id")] for r in rows],
    )


def start_run(con, source_id: str, pipeline: str, pipeline_version: str, *,
              source_url: str | None = None, raw_path: str | None = None,
              raw_sha256: str | None = None) -> int:
    return con.execute(
        """INSERT INTO ingestion_runs
             (source_id, pipeline, pipeline_version, source_url, raw_path, raw_sha256)
           VALUES (?, ?, ?, ?, ?, ?) RETURNING run_id""",
        [source_id, pipeline, pipeline_version, source_url, raw_path, raw_sha256],
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


_SAME_KEY = """o.superseded_at IS NULL AND o.source_id = $source_id
    AND o.entity_id = s.entity_id AND o.metric_id = s.metric_id
    AND o.period_label IS NOT DISTINCT FROM s.period_label
    AND o.context::VARCHAR = s.context"""


def record_observations(con, rows: list[dict], *, source_id: str, run_id: int | None = None,
                        source_url: str | None = None,
                        fetched_at: datetime | None = None) -> dict:
    """Bulk version of record_observation, for pipelines writing thousands of facts.

    Same rules: unchanged values are skipped, changed values supersede the old version.
    Each row needs entity_id, metric_id and value (or value_text); period_label,
    period_start, period_end, context and unit are optional. Keys must be unique.
    Returns counts of inserted, superseded and unchanged facts."""
    license_ = con.execute(
        "SELECT license FROM sources WHERE source_id = ?", [source_id]
    ).fetchone()
    if license_ is None:
        raise ValueError(f"Unknown source {source_id!r}; register it first")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rows.ndjson"
        with path.open("w") as f:
            for r in rows:
                f.write(json.dumps({
                    "entity_id": r["entity_id"], "metric_id": r["metric_id"],
                    "period_label": r.get("period_label"),
                    "period_start": _iso(r.get("period_start")),
                    "period_end": _iso(r.get("period_end")),
                    "context": json.dumps(r.get("context") or {}, sort_keys=True),
                    "value": r.get("value"), "value_text": r.get("value_text"),
                    "unit": r.get("unit"),
                }) + "\n")
        con.execute(
            """CREATE OR REPLACE TEMP TABLE _staging AS
               SELECT * FROM read_json(?, format = 'newline_delimited', columns = {
                   entity_id: 'VARCHAR', metric_id: 'VARCHAR', period_label: 'VARCHAR',
                   period_start: 'DATE', period_end: 'DATE', context: 'VARCHAR',
                   value: 'DOUBLE', value_text: 'VARCHAR', unit: 'VARCHAR'})""",
            [str(path)],
        )

    params = {"source_id": source_id}
    unchanged = con.execute(
        f"""SELECT count(*) FROM _staging s WHERE EXISTS (
              SELECT 1 FROM observations o WHERE {_SAME_KEY}
                AND o.value IS NOT DISTINCT FROM s.value
                AND o.value_text IS NOT DISTINCT FROM s.value_text)""", params,
    ).fetchone()[0]
    superseded = con.execute(
        f"""UPDATE observations o SET superseded_at = current_timestamp
            FROM _staging s
            WHERE {_SAME_KEY}
              AND (o.value IS DISTINCT FROM s.value OR o.value_text IS DISTINCT FROM s.value_text)
            RETURNING o.obs_id""", params,
    ).fetchall()
    con.execute(
        f"""INSERT INTO observations
              (entity_id, metric_id, period_start, period_end, period_label, context,
               value, value_text, unit, source_id, source_url, fetched_at, run_id, license)
            SELECT s.entity_id, s.metric_id, s.period_start, s.period_end, s.period_label,
                   s.context, s.value, s.value_text, s.unit, $source_id, $source_url,
                   $fetched_at, $run_id, $license
            FROM _staging s
            WHERE NOT EXISTS (SELECT 1 FROM observations o WHERE {_SAME_KEY})""",
        {**params, "source_url": source_url, "run_id": run_id, "license": license_[0],
         "fetched_at": fetched_at or datetime.now(timezone.utc)},
    )
    con.execute("DROP TABLE _staging")
    return {"inserted": len(rows) - unchanged, "superseded": len(superseded),
            "unchanged": unchanged}


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


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
