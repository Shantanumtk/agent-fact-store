"""Core API for writing and reading facts in the universal schema."""

from __future__ import annotations

import json
import subprocess
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
                    attribution: str | None = None, upstream: str | None = None) -> None:
    con.execute(
        """INSERT OR REPLACE INTO sources
             (source_id, name, homepage_url, license, license_url, commercial_use,
              attribution, upstream)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [source_id, name, homepage_url, license, license_url, commercial_use, attribution,
         upstream],
    )


def define_metric(con, metric_id: str, domain: str, name: str, description: str,
                  unit: str | None = None, higher_is_better: bool | None = None,
                  definition: str | None = None) -> None:
    con.execute(
        """INSERT OR REPLACE INTO metrics VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [metric_id, domain, name, description, unit, higher_is_better, definition],
    )


class IdentifierConflict(ValueError):
    """An outside ID was asserted for a different entity than the one it already maps to."""

    def __init__(self, conflicts: list[tuple]):
        self.conflicts = conflicts
        shown = ", ".join(f"{s}:{i} ({a} vs {b})" for s, i, a, b in conflicts[:5])
        super().__init__(f"{len(conflicts)} identifier conflict(s): {shown}")


def upsert_entity(con, entity_id: str, domain: str, entity_type: str, canonical_name: str,
                  attributes: dict | None = None) -> None:
    upsert_entities(con, [{"entity_id": entity_id, "domain": domain, "entity_type": entity_type,
                           "canonical_name": canonical_name, "attributes": attributes}])


def upsert_entities(con, rows: list[dict], update_names: bool = True) -> None:
    """Create entities, or update existing ones. New attributes are merged into existing
    ones, so two sources can each contribute attributes without erasing the other's.
    Pass update_names=False from sources that shouldn't override the canonical name."""
    _stage(con, "_entities", [{**r, "attributes": json.dumps(r.get("attributes") or {})}
                              for r in rows],
           {"entity_id": "VARCHAR", "domain": "VARCHAR", "entity_type": "VARCHAR",
            "canonical_name": "VARCHAR", "attributes": "VARCHAR"})
    name = "excluded.canonical_name" if update_names else "entities.canonical_name"
    con.execute(
        f"""INSERT INTO entities (entity_id, domain, entity_type, canonical_name, attributes)
            SELECT entity_id, domain, entity_type, canonical_name, attributes FROM _entities
            ON CONFLICT (entity_id) DO UPDATE SET
                canonical_name = {name},
                attributes = json_merge_patch(entities.attributes, excluded.attributes)""")
    con.execute("DROP TABLE _entities")


def add_identifier(con, entity_id: str, scheme: str, identifier: str,
                   source_id: str | None = None, run_id: int | None = None) -> None:
    add_identifiers(con, [{"entity_id": entity_id, "scheme": scheme, "identifier": identifier,
                           "source_id": source_id}], run_id=run_id)


def add_identifiers(con, rows: list[dict], run_id: int | None = None) -> int:
    """Map outside IDs to entities. Existing mappings are kept as they are (with the run that
    first asserted them). Raises IdentifierConflict, writing nothing, if any ID would end
    up pointing at two entities. Returns how many new mappings were added."""
    _stage(con, "_ids", rows, {"entity_id": "VARCHAR", "scheme": "VARCHAR",
                               "identifier": "VARCHAR", "source_id": "VARCHAR"})
    conflicts = con.execute(
        """SELECT s.scheme, s.identifier, i.entity_id, s.entity_id
           FROM _ids s JOIN entity_identifiers i USING (scheme, identifier)
           WHERE i.entity_id <> s.entity_id
           UNION ALL
           SELECT scheme, identifier, min(entity_id), max(entity_id)
           FROM _ids GROUP BY scheme, identifier HAVING count(DISTINCT entity_id) > 1""",
    ).fetchall()
    if conflicts:
        con.execute("DROP TABLE _ids")
        raise IdentifierConflict(conflicts)
    added = con.execute(
        """INSERT OR IGNORE INTO entity_identifiers (entity_id, scheme, identifier, source_id, run_id)
           SELECT DISTINCT entity_id, scheme, identifier, source_id, ? FROM _ids""", [run_id],
    ).fetchone()[0]
    con.execute("DROP TABLE _ids")
    return added


def add_names(con, rows: list[dict], run_id: int | None = None) -> int:
    """Record names entities are known by. Returns how many new names were added."""
    _stage(con, "_names", rows,
           {"entity_id": "VARCHAR", "name": "VARCHAR", "source_id": "VARCHAR"})
    added = con.execute(
        """INSERT OR IGNORE INTO entity_names (entity_id, name, source_id, run_id)
           SELECT DISTINCT entity_id, name, source_id, ? FROM _names
           WHERE name IS NOT NULL AND name <> ''""", [run_id],
    ).fetchone()[0]
    con.execute("DROP TABLE _names")
    return added


def resolve(con, scheme: str, identifier: str) -> str | None:
    """Map an outside ID (e.g. a Cricsheet ID) to our canonical entity ID."""
    row = con.execute(
        "SELECT entity_id FROM entity_identifiers WHERE scheme = ? AND identifier = ?",
        [scheme, identifier],
    ).fetchone()
    return row[0] if row else None


def identifiers(con, entity_id: str) -> dict[str, list[str]]:
    """All outside IDs for an entity, grouped by scheme."""
    out: dict[str, list[str]] = {}
    for scheme, identifier in con.execute(
        """SELECT scheme, identifier FROM entity_identifiers WHERE entity_id = ?
           ORDER BY scheme, identifier""", [entity_id],
    ).fetchall():
        out.setdefault(scheme, []).append(identifier)
    return out


def find_entities(con, query: str, domain: str | None = None, limit: int = 10,
                  min_score: float = 0.85, entity_type: str | None = None) -> list[dict]:
    """Find entities by any of their names, best first: exact name, then the query as whole
    words inside a name ("Anderson" in "James Anderson"), then a full name matching an
    initials form ("Mithali Raj" -> "M Raj", "Mahendra Singh Dhoni" -> "MS Dhoni"), then
    part of a word, then close spellings. Ties go to the entity with more facts, so "Kohli"
    finds the Kohli we hold data on before namesakes we only have a register entry for."""
    parts = query.lower().split()
    initials = {"q_first": None, "q_rest": None, "q_last": None, "q_initials": None}
    if len(parts) >= 2:
        initials = {"q_first": parts[0][0], "q_rest": " ".join(parts[1:]), "q_last": parts[-1],
                    "q_initials": "".join(p[0] for p in parts[:-1])}
    sql = """
        WITH named AS (
            SELECT entity_id, name, string_split(name, ' ') AS words FROM entity_names),
        scored AS (
            SELECT entity_id, name,
                   CASE WHEN lower(name) = lower($q) THEN 5
                        WHEN contains(' ' || lower(name) || ' ', ' ' || lower($q) || ' ') THEN 4
                        WHEN $q_first IS NOT NULL AND regexp_matches(words[1], '^[A-Z]+$') AND (
                             (lower(words[1][1]) = $q_first
                              AND lower(array_to_string(words[2:], ' ')) = $q_rest)
                             OR (len(words) = 2 AND lower(words[2]) = $q_last
                                 AND starts_with(lower(words[1]), $q_initials))) THEN 3
                        WHEN contains(lower(name), lower($q)) THEN 2
                        ELSE 1 END AS tier,
                   jaro_winkler_similarity(lower(name), lower($q)) AS similarity
            FROM named),
        matches AS (
            SELECT entity_id, max(tier) AS tier, max(similarity) AS similarity,
                   list(DISTINCT name ORDER BY name) AS matched_names
            FROM scored WHERE tier > 1 OR similarity >= $min_score GROUP BY entity_id)
        SELECT e.entity_id, e.canonical_name, e.domain, e.entity_type,
               ['similar spelling', 'part of a name', 'initials', 'word in a name',
                'exact'][m.tier] AS match, round(m.similarity, 3) AS similarity, m.matched_names,
               (SELECT count(*) FROM current_observations o
                WHERE o.entity_id = e.entity_id) AS fact_count
        FROM matches m JOIN entities e USING (entity_id)
        WHERE ($domain IS NULL OR e.domain = $domain)
          AND ($entity_type IS NULL OR e.entity_type = $entity_type)
        ORDER BY m.tier DESC,
                 CASE WHEN m.tier = 1 THEN m.similarity END DESC NULLS LAST,
                 fact_count DESC, m.similarity DESC, e.canonical_name
        LIMIT $limit"""
    cur = con.execute(sql, {"q": query, "domain": domain, "entity_type": entity_type,
                            "limit": limit, "min_score": min_score, **initials})
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def code_version() -> str:
    """The git commit of this code, with '-dirty' if there are uncommitted changes."""
    here = Path(__file__).parent
    try:
        commit = subprocess.run(["git", "-C", str(here), "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(here), "status", "--porcelain", "--", "."],
                               capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{commit}-dirty" if dirty else commit


def start_run(con, source_id: str, pipeline: str, pipeline_version: str, *,
              source_url: str | None = None, raw_path: str | None = None,
              raw_sha256: str | None = None, params: dict | None = None) -> int:
    return con.execute(
        """INSERT INTO ingestion_runs
             (source_id, pipeline, pipeline_version, code_version, source_url, raw_path,
              raw_sha256, params)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING run_id""",
        [source_id, pipeline, pipeline_version, code_version(), source_url, raw_path,
         raw_sha256, json.dumps(params, sort_keys=True) if params is not None else None],
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
            """UPDATE observations SET superseded_at = current_timestamp, superseded_by_run = ?
               WHERE obs_id = ?""",
            [run_id, existing[0]],
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
    period_start, period_end, context and unit are optional. Derived facts also pass
    license (combined from their inputs) and inputs_hash. A row's own source_url, if
    given, overrides the batch one. Keys must be unique.
    Returns counts of inserted, superseded and unchanged facts."""
    license_ = con.execute(
        "SELECT license FROM sources WHERE source_id = ?", [source_id]
    ).fetchone()
    if license_ is None:
        raise ValueError(f"Unknown source {source_id!r}; register it first")

    _stage(con, "_staging", [{
        **r, "period_start": _iso(r.get("period_start")), "period_end": _iso(r.get("period_end")),
        "context": json.dumps(r.get("context") or {}, sort_keys=True),
    } for r in rows], {
        "entity_id": "VARCHAR", "metric_id": "VARCHAR", "period_label": "VARCHAR",
        "period_start": "DATE", "period_end": "DATE", "context": "VARCHAR",
        "value": "DOUBLE", "value_text": "VARCHAR", "unit": "VARCHAR",
        "license": "VARCHAR", "inputs_hash": "VARCHAR", "source_url": "VARCHAR",
    })

    params = {"source_id": source_id}
    same_value = """o.value IS NOT DISTINCT FROM s.value
        AND o.value_text IS NOT DISTINCT FROM s.value_text
        AND o.inputs_hash IS NOT DISTINCT FROM s.inputs_hash"""
    unchanged = con.execute(
        f"""SELECT count(*) FROM _staging s WHERE EXISTS (
              SELECT 1 FROM observations o WHERE {_SAME_KEY} AND {same_value})""", params,
    ).fetchone()[0]
    superseded = con.execute(
        f"""UPDATE observations o
            SET superseded_at = current_timestamp, superseded_by_run = $run_id
            FROM _staging s
            WHERE {_SAME_KEY} AND NOT ({same_value})
            RETURNING o.obs_id""", {**params, "run_id": run_id},
    ).fetchall()
    con.execute(
        f"""INSERT INTO observations
              (entity_id, metric_id, period_start, period_end, period_label, context,
               value, value_text, unit, source_id, source_url, fetched_at, run_id, license,
               inputs_hash)
            SELECT s.entity_id, s.metric_id, s.period_start, s.period_end, s.period_label,
                   s.context, s.value, s.value_text, s.unit, $source_id,
                   coalesce(s.source_url, $source_url),
                   $fetched_at, $run_id, coalesce(s.license, $license), s.inputs_hash
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


def _stage(con, table: str, rows: list[dict], columns: dict[str, str]) -> None:
    """Load rows into a temp table in one pass. Much faster than executemany for
    thousands of rows. Only the listed columns are kept; missing keys become NULL."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rows.ndjson"
        with path.open("w") as f:
            for r in rows:
                f.write(json.dumps({c: r.get(c) for c in columns}) + "\n")
        spec = ", ".join(f"{c}: '{t}'" for c, t in columns.items())
        con.execute(
            f"""CREATE OR REPLACE TEMP TABLE {table} AS
                SELECT * FROM read_json(?, format = 'newline_delimited', columns = {{{spec}}})""",
            [str(path)],
        )


def get_facts(con, entity_id: str, metric_id: str | None = None,
              include_history: bool = False) -> list[dict]:
    """Read facts with their full provenance attached. For derived facts, commercial_use
    and attribution are worked out from the raw sources they were computed from."""
    table = "observations" if include_history else "current_observations"
    sql = f"""
        SELECT o.obs_id, o.entity_id, e.canonical_name, o.metric_id, o.period_label,
               o.context, o.value, o.value_text, o.unit, o.source_id, s.name AS source_name,
               o.source_url, o.fetched_at, o.license, s.commercial_use, s.attribution,
               o.confidence, o.recorded_at, o.superseded_at, o.superseded_by_run, o.run_id,
               o.inputs_hash IS NOT NULL AS derived
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
    facts = [dict(zip(cols, row)) for row in cur.fetchall()]
    for f in facts:
        if f["derived"]:
            sources = raw_sources(con, f["obs_id"])
            f["commercial_use"] = all(s["commercial_use"] for s in sources)
            f["attribution"] = "; ".join(s["attribution"] for s in sources if s["attribution"])
    return facts


def lineage(con, obs_id: int) -> list[dict]:
    """Every fact a fact was computed from, all the way down to raw facts, with the run
    (method or pipeline, code version, raw file) behind each. Depth 0 is the fact itself."""
    cur = con.execute(
        """WITH RECURSIVE tree(obs_id, parent_obs_id, role, depth) AS (
               SELECT ?::BIGINT, NULL::BIGINT, NULL::VARCHAR, 0
               UNION ALL
               SELECT fi.input_obs_id, fi.obs_id, fi.role, t.depth + 1
               FROM tree t JOIN fact_inputs fi ON fi.obs_id = t.obs_id)
           SELECT t.depth, t.role, t.parent_obs_id, o.obs_id, o.entity_id, o.metric_id,
                  o.period_label, o.context, o.value, o.source_id, o.license,
                  o.inputs_hash IS NOT NULL AS derived, o.superseded_at,
                  r.run_id, r.pipeline, r.pipeline_version, r.code_version, r.params,
                  r.source_url, r.raw_sha256
           FROM tree t
           JOIN observations o ON o.obs_id = t.obs_id
           LEFT JOIN ingestion_runs r ON r.run_id = o.run_id
           ORDER BY t.depth, o.entity_id, o.metric_id, o.period_label""",
        [obs_id],
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def raw_sources(con, obs_id: int) -> list[dict]:
    """The original sources behind a fact (itself, if it is raw), with their licenses."""
    cur = con.execute(
        """WITH RECURSIVE tree(obs_id) AS (
               SELECT ?::BIGINT
               UNION
               SELECT fi.input_obs_id FROM tree t JOIN fact_inputs fi ON fi.obs_id = t.obs_id)
           SELECT DISTINCT s.source_id, s.name, s.license, s.commercial_use, s.attribution
           FROM tree t JOIN observations o USING (obs_id) JOIN sources s USING (source_id)
           WHERE o.inputs_hash IS NULL
           ORDER BY s.source_id""",
        [obs_id],
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
