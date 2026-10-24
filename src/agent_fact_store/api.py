"""Read-only query layer behind the MCP server. Field-agnostic: every function works on
the universal template (entities, metrics, periods, contexts, facts, lineage) and never
on any one domain's terms.

Every fact returned carries its receipts: source, license, whether commercial use is
allowed, and a fact_id that explain_fact can trace back to the raw files.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import date, datetime
from pathlib import Path

import duckdb

from agent_fact_store import core, verify

MAX_LIMIT = 500
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class QueryError(ValueError):
    """A request that can't be answered as asked. The message says what to do instead."""


def open_readonly(path: str | Path) -> duckdb.DuckDBPyConnection:
    """Open the store for reading only, with file and network access switched off so
    run_sql can't reach anything outside the database."""
    if not Path(path).exists():
        raise FileNotFoundError(f"No fact store at {path}; run a pipeline to create it")
    con = duckdb.connect(str(path), read_only=True)
    try:
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")
    except duckdb.InvalidInputException:
        # DuckDB shares one instance per file within a process; a previous open may
        # already have locked it. That's fine as long as access is still switched off.
        if con.execute("SELECT current_setting('enable_external_access')").fetchone()[0]:
            raise
    return con


# ---------------------------------------------------------------- discovery

def list_domains(con) -> list[dict]:
    return _rows(con, """
        SELECT e.domain,
               count(DISTINCT e.entity_id) AS entities,
               list(DISTINCT e.entity_type ORDER BY e.entity_type) AS entity_types,
               (SELECT count(*) FROM metrics m WHERE m.domain = e.domain) AS metrics,
               count(o.obs_id) AS facts,
               min(o.period_start) AS first_period_start, max(o.period_end) AS last_period_end,
               list(DISTINCT o.source_id ORDER BY o.source_id)
                   FILTER (WHERE o.source_id IS NOT NULL) AS sources
        FROM entities e LEFT JOIN current_observations o USING (entity_id)
        GROUP BY e.domain ORDER BY e.domain""")


def search_entities(con, query: str, domain: str | None = None,
                    entity_type: str | None = None, limit: int = 10) -> list[dict]:
    return [_clean(r) for r in core.find_entities(con, query, domain=domain,
                                                  entity_type=entity_type,
                                                  limit=_limit(limit, 50))]


def get_entity(con, entity_id: str | None = None, scheme: str | None = None,
               identifier: str | None = None) -> dict:
    if entity_id is None:
        if not (scheme and identifier):
            raise QueryError("Pass entity_id, or scheme and identifier (an outside ID)")
        entity_id = core.resolve(con, scheme, identifier)
        if entity_id is None:
            raise QueryError(f"No entity has {scheme} ID {identifier!r}. "
                             "Try search_entities with a name instead.")
    entity = _one(con, """SELECT entity_id, canonical_name, domain, entity_type, attributes
                          FROM entities WHERE entity_id = ?""", [entity_id])
    if entity is None:
        raise QueryError(f"Unknown entity {entity_id!r}. Use search_entities to find its ID.")
    entity["attributes"] = json.loads(entity["attributes"] or "{}")
    entity["names"] = [r["name"] for r in _rows(
        con, "SELECT name FROM entity_names WHERE entity_id = ? ORDER BY name", [entity_id])]
    entity["identifiers"] = core.identifiers(con, entity_id)
    entity["coverage"] = _rows(con, """
        SELECT o.metric_id, m.name AS metric_name, o.context, count(*) AS facts,
               min(o.period_start) AS first_period_start, max(o.period_end) AS last_period_end,
               list(DISTINCT o.period_label ORDER BY o.period_label) AS periods
        FROM current_observations o JOIN metrics m USING (metric_id)
        WHERE o.entity_id = ? GROUP BY ALL ORDER BY o.metric_id, o.context""", [entity_id])
    for c in entity["coverage"]:
        c["context"] = json.loads(c["context"])
    return entity


def list_metrics(con, domain: str | None = None, query: str | None = None,
                 limit: int = 50, offset: int = 0) -> dict:
    where, params = ["TRUE"], []
    if domain:
        where.append("m.domain = ?")
        params.append(domain)
    if query:
        where.append("(m.metric_id ILIKE ? OR m.name ILIKE ? OR m.description ILIKE ?)")
        params += [f"%{query}%"] * 3
    limit = _limit(limit)
    rows = _rows(con, f"""
        SELECT m.metric_id, m.domain, m.name, m.description, m.unit, m.higher_is_better,
               m.definition, count(o.obs_id) AS facts,
               count(DISTINCT o.entity_id) AS entities,
               bool_or(o.inputs_hash IS NOT NULL) AS derived
        FROM metrics m LEFT JOIN current_observations o USING (metric_id)
        WHERE {' AND '.join(where)}
        GROUP BY ALL ORDER BY m.domain, m.metric_id
        LIMIT ? OFFSET ?""", params + [limit + 1, offset])
    return _page(rows, limit, offset)


def describe_metric(con, metric_id: str) -> dict:
    metric = _one(con, "SELECT * FROM metrics WHERE metric_id = ?", [metric_id])
    if metric is None:
        raise QueryError(f"Unknown metric {metric_id!r}. Use list_metrics to browse the catalog.")
    metric["contexts"] = [{"context": json.loads(r["context"]), "facts": r["facts"]} for r in _rows(
        con, """SELECT context, count(*) AS facts FROM current_observations
                WHERE metric_id = ? GROUP BY context ORDER BY facts DESC""", [metric_id])]
    metric["periods"] = _rows(con, """
        SELECT period_label, min(period_start) AS period_start, max(period_end) AS period_end,
               count(*) AS facts
        FROM current_observations WHERE metric_id = ?
        GROUP BY period_label ORDER BY min(period_start) NULLS LAST, period_label""", [metric_id])
    metric["entity_types"] = _rows(con, """
        SELECT e.domain, e.entity_type, count(DISTINCT e.entity_id) AS entities
        FROM current_observations o JOIN entities e USING (entity_id)
        WHERE o.metric_id = ? GROUP BY ALL ORDER BY entities DESC""", [metric_id])
    # how this metric is produced, and which metrics are produced from it
    runs = _rows(con, """
        SELECT DISTINCT r.pipeline, r.params FROM ingestion_runs r
        WHERE r.source_id = 'derived' AND r.status = 'success'""")
    produced_by, used_by = [], set()
    for r in runs:
        p = json.loads(r["params"] or "{}")
        inputs = {p.get(k) for k in ("metric", "numerator", "denominator", "reference_metric")} - {None}
        if p.get("output_metric") == metric_id:
            entry = {"operation": r["pipeline"], "inputs": sorted(inputs)}
            if entry not in produced_by:
                produced_by.append(entry)
        elif metric_id in inputs and p.get("output_metric"):
            used_by.add(p["output_metric"])
    metric["derived_by"] = produced_by
    metric["used_to_derive"] = sorted(used_by)
    return metric


# ---------------------------------------------------------------- facts

def get_facts(con, entity_ids: list[str], metric_ids: list[str] | None = None,
              periods: list[str] | None = None, start: str | None = None,
              end: str | None = None, context: dict | None = None, as_of: str | None = None,
              include_history: bool = False, detail: str = "concise",
              limit: int = 100, offset: int = 0) -> dict:
    if not entity_ids:
        raise QueryError("Pass at least one entity_id. Use search_entities to find IDs.")
    where = ["list_contains(?, o.entity_id)"]
    params: list = [list(entity_ids)]
    if metric_ids:
        where.append("list_contains(?, o.metric_id)")
        params.append(list(metric_ids))
    _period_filters(where, params, periods, start, end)
    _context_filter(where, params, context)
    if as_of:
        where.append("o.recorded_at <= ?::TIMESTAMP "
                     "AND (o.superseded_at IS NULL OR o.superseded_at > ?::TIMESTAMP)")
        params += [as_of, as_of]
    elif not include_history:
        where.append("o.superseded_at IS NULL")
    limit = _limit(limit)
    rows = _fact_rows(con, " AND ".join(where), params, detail, limit + 1, offset,
                      order="o.entity_id, o.metric_id, o.period_start NULLS LAST, o.period_label, "
                            "o.recorded_at")
    result = _page(rows, limit, offset)
    result["coverage_notes"] = _coverage_notes(con, result["rows"])
    if not rows and offset == 0:
        result["hint"] = ("No facts matched. get_entity shows which metrics, contexts and "
                          "periods exist for an entity.")
    return result


def compare(con, entity_ids: list[str], metric_ids: list[str], period: str,
            context: dict | None = None) -> dict:
    if len(entity_ids) < 2 or not metric_ids:
        raise QueryError("Pass two or more entity_ids and one or more metric_ids")
    where = ["list_contains(?, o.entity_id)", "list_contains(?, o.metric_id)",
             "o.period_label = ?", "o.superseded_at IS NULL"]
    params: list = [list(entity_ids), list(metric_ids), period]
    _context_filter(where, params, context)
    facts = _fact_rows(con, " AND ".join(where), params, "concise", MAX_LIMIT, 0,
                       order="o.entity_id, o.metric_id")
    _require_single_context(facts, context)
    names = {r["entity_id"]: r["canonical_name"] for r in _rows(
        con, "SELECT entity_id, canonical_name FROM entities WHERE list_contains(?, entity_id)",
        [list(entity_ids)])}
    unknown = [e for e in entity_ids if e not in names]
    if unknown:
        raise QueryError(f"Unknown entities {unknown}. Use search_entities to find IDs.")
    table = []
    for eid in entity_ids:
        values = {}
        for mid in metric_ids:
            f = next((f for f in facts if f["entity_id"] == eid and f["metric_id"] == mid), None)
            values[mid] = None if f is None else {k: f[k] for k in (
                "value", "unit", "fact_id", "source_id", "license", "commercial_use", "derived")}
        table.append({"entity_id": eid, "entity_name": names[eid], "values": values})
    return {"period": period, "context": context or (facts and facts[0]["context"]) or {},
            "rows": table,
            "missing": [f"{r['entity_id']}: {m}" for r in table for m, v in r["values"].items()
                        if v is None]}


def rank(con, metric_id: str, period: str, context: dict | None = None,
         domain: str | None = None, entity_type: str | None = None,
         attributes: dict | None = None, filters: list[dict] | None = None,
         order: str = "auto", limit: int = 20, offset: int = 0) -> dict:
    metric = _one(con, "SELECT higher_is_better FROM metrics WHERE metric_id = ?", [metric_id])
    if metric is None:
        raise QueryError(f"Unknown metric {metric_id!r}. Use list_metrics to browse the catalog.")
    if order == "auto":
        order = "asc" if metric["higher_is_better"] is False else "desc"
    if order not in ("asc", "desc"):
        raise QueryError("order must be 'auto', 'asc' or 'desc'")

    where = ["o.metric_id = ?", "o.period_label = ?", "o.superseded_at IS NULL"]
    params: list = [metric_id, period]
    _context_filter(where, params, context)
    for column, value in (("domain", domain), ("entity_type", entity_type)):
        if value:
            where.append(f"e.{column} = ?")
            params.append(value)
    for key, value in (attributes or {}).items():
        where.append(f"json_extract_string(e.attributes, '$.{_safe_key(key)}') = ?")
        params.append(str(value))
    for f in filters or []:
        if "metric_id" not in f or not ({"min", "max"} & f.keys()):
            raise QueryError("Each filter needs metric_id and min and/or max, e.g. "
                             '{"metric_id": "...", "min": 30}')
        cond = ["x.entity_id = o.entity_id", "x.period_label = o.period_label",
                "x.context::VARCHAR = o.context::VARCHAR", "x.metric_id = ?",
                "x.superseded_at IS NULL"]
        params_f: list = [f["metric_id"]]
        if "min" in f:
            cond.append("x.value >= ?")
            params_f.append(f["min"])
        if "max" in f:
            cond.append("x.value <= ?")
            params_f.append(f["max"])
        where.append(f"EXISTS (SELECT 1 FROM observations x WHERE {' AND '.join(cond)})")
        params += params_f

    contexts = _rows(con, f"""SELECT DISTINCT o.context FROM observations o
                              JOIN entities e USING (entity_id) WHERE {' AND '.join(where)}""",
                     params)
    if len(contexts) > 1:
        raise QueryError(f"{metric_id} has facts for {period} in several contexts "
                         f"({', '.join(c['context'] for c in contexts)}); pass context to pick one")
    limit = _limit(limit)
    rows = _fact_rows(con, " AND ".join(where), params, "concise", limit + 1, offset,
                      order=f"o.value {order.upper()}, e.canonical_name")
    result = _page(rows, limit, offset)
    for i, r in enumerate(result["rows"], start=offset + 1):
        r["rank"] = i
    result["order"] = order
    return result


# ---------------------------------------------------------------- trust

def explain_fact(con, fact_id: int, max_depth: int = 2, max_nodes: int = 100) -> dict:
    fact = _one(con, "SELECT obs_id FROM observations WHERE obs_id = ?", [fact_id])
    if fact is None:
        raise QueryError(f"No fact with fact_id {fact_id}")
    tree = core.lineage(con, fact_id)
    total = len(tree)
    nodes = [n for n in tree if n["depth"] <= max_depth]
    hidden_deeper = total - len(nodes)
    nodes = nodes[:max_nodes]
    out = []
    for n in nodes:
        n = _clean(n)
        n["fact_id"], n["parent_fact_id"] = n.pop("obs_id"), n.pop("parent_obs_id")
        n["context"] = json.loads(n["context"])
        n["params"] = json.loads(n["params"]) if n["params"] else None
        out.append(n)
    sources = core.raw_sources(con, fact_id)
    checks = _rows(con, """
        SELECT c.check_id, c.status, c.independent, c.subject_value, c.reference_value,
               c.difference, o.source_id AS reference_source, o.source_url AS reference_url,
               c.detail, c.recorded_at
        FROM checks c JOIN observations o ON o.obs_id = c.reference_obs_id
        WHERE c.subject_obs_id = ? ORDER BY c.check_id DESC""", [fact_id])
    for c in checks:
        c["detail"] = json.loads(c["detail"] or "{}")
    return {
        "fact": out[0], "inputs": out[1:], "nodes_total": total,
        **verify.assess(con, [fact_id])[fact_id], "checks": checks,
        "coverage_notes": _coverage_notes(con, [{"entity_id": out[0]["entity_id"],
                                                  "context": out[0]["context"],
                                                  "period_label": out[0]["period_label"]}]),
        "nodes_shown": len(out), "deeper_nodes_hidden": hidden_deeper,
        "raw_sources": sources,
        "commercial_use": all(s["commercial_use"] for s in sources),
        "note": ("Raise max_depth to see further down; every branch ends at raw facts whose "
                 "run records the source URL and the SHA-256 of the file they came from."
                 if hidden_deeper or total > len(out) else None),
    }


def get_fact_history(con, entity_id: str, metric_id: str, period: str,
                     context: dict | None = None) -> dict:
    where = ["o.entity_id = ?", "o.metric_id = ?", "o.period_label = ?"]
    params: list = [entity_id, metric_id, period]
    _context_filter(where, params, context)
    versions = _fact_rows(con, " AND ".join(where), params, "detailed", MAX_LIMIT, 0,
                          order="o.context::VARCHAR, o.source_id, o.recorded_at")
    runs = {r["run_id"]: r for r in _rows(con, """
        SELECT run_id, pipeline, pipeline_version, code_version, raw_sha256, params,
               finished_at FROM ingestion_runs""")}
    for v in versions:
        v["produced_by_run"] = runs.get(v["run_id"])
        v["replaced_by_run"] = runs.get(v["superseded_by_run"])
    return {"versions": versions,
            "note": "When a value changed, compare the raw_sha256 (source file changed) and "
                    "code_version (code changed) of produced_by_run and replaced_by_run."}


def list_sources(con) -> list[dict]:
    return _rows(con, """
        SELECT s.source_id, s.name, s.homepage_url, s.license, s.license_url,
               s.commercial_use, s.attribution,
               (SELECT count(*) FROM current_observations o WHERE o.source_id = s.source_id)
                   AS current_facts,
               (SELECT count(*) FROM ingestion_runs r WHERE r.source_id = s.source_id) AS runs,
               (SELECT max(r.finished_at) FROM ingestion_runs r
                WHERE r.source_id = s.source_id AND r.status = 'success') AS last_success_at
        FROM sources s ORDER BY s.source_id""")


def get_coverage(con, domain: str | None = None, dataset: str | None = None,
                 period: str | None = None, only_gaps: bool = False,
                 limit: int = 100, offset: int = 0) -> dict:
    where, params = ["TRUE"], []
    for column, value in (("domain", domain), ("dataset", dataset), ("period_label", period)):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    if only_gaps:
        where.append("(expected IS NULL OR expected > present)")
    limit = _limit(limit)
    rows = _rows(con, f"""
        SELECT source_id, domain, dataset, scope, period_label, expected, present,
               expected - present AS missing_count, missing, method, recorded_at
        FROM current_coverage WHERE {' AND '.join(where)}
        ORDER BY domain, dataset, period_label = 'all' DESC, period_label
        LIMIT ? OFFSET ?""", params + [limit + 1, offset])
    for r in rows:
        r["scope"], r["missing"] = json.loads(r["scope"]), json.loads(r["missing"] or "[]")
    return _page(rows, limit, offset)


def list_issues(con, status: str | None = None, kind: str | None = None,
                domain: str | None = None, entity_id: str | None = None,
                limit: int = 50, offset: int = 0) -> dict:
    where, params = ["TRUE"], []
    for column, value in (("status", status), ("kind", kind), ("domain", domain),
                          ("entity_id", entity_id)):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    limit = _limit(limit)
    rows = _rows(con, f"""
        SELECT issue_id, issue_key, kind, status, source_id, domain, entity_id, metric_id,
               period_label, summary, evidence, created_at, updated_at
        FROM issues WHERE {' AND '.join(where)}
        ORDER BY updated_at DESC, issue_id LIMIT ? OFFSET ?""", params + [limit + 1, offset])
    for r in rows:
        r["evidence"] = json.loads(r["evidence"] or "{}")
        r["reviews"] = _rows(con, """
            SELECT verdict, checked_against, checked_on, reviewer, observed, note
            FROM reviews WHERE issue_key = ? ORDER BY review_id""", [r.pop("issue_key")])
        for review in r["reviews"]:
            review["observed"] = json.loads(review["observed"] or "{}")
    return _page(rows, limit, offset)


# ---------------------------------------------------------------- escape hatch

SQL_TABLES = (
    "entities(entity_id, domain, entity_type, canonical_name, attributes JSON); "
    "entity_names(entity_id, name); entity_identifiers(entity_id, scheme, identifier); "
    "metrics(metric_id, domain, name, description, unit, higher_is_better, definition); "
    "current_observations / observations(obs_id, entity_id, metric_id, period_label, "
    "period_start, period_end, context JSON, value, unit, source_id, license, run_id, "
    "recorded_at, superseded_at, inputs_hash); fact_inputs(obs_id, input_obs_id, role); "
    "sources(source_id, name, license, commercial_use, attribution); "
    "ingestion_runs(run_id, source_id, pipeline, code_version, raw_sha256, params); "
    "checks(check_id, entity_id, metric_id, subject_obs_id, reference_obs_id, status, "
    "independent, detail JSON); coverage / current_coverage(source_id, domain, dataset, "
    "scope JSON, period_label, expected, present, missing JSON); issues(issue_id, kind, "
    "status, source_id, entity_id, metric_id, period_label, summary, evidence JSON); "
    "reviews(issue_key, verdict, checked_against, checked_on, reviewer, observed JSON, note)")


def run_sql(con, sql: str, limit: int = 200, timeout_seconds: float = 10.0) -> dict:
    statement = sql.strip().rstrip(";").strip()
    if ";" in statement:
        raise QueryError("Send one statement at a time")
    if not re.match(r"(?is)^(select|with)\b", statement):
        raise QueryError("Only SELECT (or WITH ... SELECT) queries are allowed")
    limit = _limit(limit)
    timer = threading.Timer(timeout_seconds, con.interrupt)
    timer.start()
    try:
        cur = con.execute(f"SELECT * FROM ({statement}) LIMIT {limit + 1}")
        columns = [d[0] for d in cur.description]
        rows = [[_plain(v) for v in row] for row in cur.fetchall()]
    except duckdb.InterruptException:
        raise QueryError(f"Query took longer than {timeout_seconds}s; narrow it down")
    except duckdb.Error as e:
        raise QueryError(f"{type(e).__name__}: {e}")
    finally:
        timer.cancel()
    return {"columns": columns, "rows": rows[:limit], "truncated": len(rows) > limit,
            "provenance": ("Results carry provenance only through the columns you selected. "
                           "Include obs_id to let explain_fact trace values, or use get_facts.")}


# ---------------------------------------------------------------- helpers

_FACT_COLUMNS = {
    "concise": """o.obs_id AS fact_id, o.entity_id, e.canonical_name AS entity_name,
                  o.metric_id, o.period_label, o.context, o.value, o.value_text, o.unit,
                  o.source_id, o.license, s.commercial_use, o.inputs_hash IS NOT NULL AS derived""",
    "detailed": """o.obs_id AS fact_id, o.entity_id, e.canonical_name AS entity_name,
                   o.metric_id, o.period_label, o.period_start, o.period_end, o.context,
                   o.value, o.value_text, o.unit, o.source_id, s.name AS source_name,
                   o.source_url, o.license, s.commercial_use, s.attribution, o.fetched_at,
                   o.run_id, o.confidence, o.recorded_at, o.superseded_at,
                   o.superseded_by_run, o.inputs_hash IS NOT NULL AS derived""",
}


def _fact_rows(con, where: str, params: list, detail: str, limit: int, offset: int,
               order: str) -> list[dict]:
    if detail not in _FACT_COLUMNS:
        raise QueryError("detail must be 'concise' or 'detailed'")
    rows = _rows(con, f"""
        SELECT {_FACT_COLUMNS[detail]}
        FROM observations o JOIN entities e USING (entity_id) JOIN sources s USING (source_id)
        WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?""", params + [limit, offset])
    assessed = verify.assess(con, [r["fact_id"] for r in rows])
    for r in rows:
        r["context"] = json.loads(r["context"])
        r.update({k: assessed[r["fact_id"]][k] for k in ("confidence", "check_status")})
        if r["derived"]:
            sources = core.raw_sources(con, r["fact_id"])
            r["commercial_use"] = all(s["commercial_use"] for s in sources)
            if detail == "detailed":
                r["attribution"] = "; ".join(s["attribution"] for s in sources if s["attribution"])
    return rows


def _coverage_notes(con, facts: list[dict]) -> list[dict]:
    """Coverage gaps that apply to these facts, so answers can say what is missing."""
    attrs: dict[str, tuple] = {}
    notes, seen = [], set()
    for f in facts:
        if f["entity_id"] not in attrs:
            row = con.execute("SELECT domain, attributes FROM entities WHERE entity_id = ?",
                              [f["entity_id"]]).fetchone()
            attrs[f["entity_id"]] = (row[0], json.loads(row[1] or "{}")) if row else (None, {})
        domain, attributes = attrs[f["entity_id"]]
        for c in verify.coverage_for(con, domain, f["context"], attributes, f["period_label"]):
            key = (c["dataset"], c["period_label"])
            if key in seen or not c["missing_count"]:
                continue
            seen.add(key)
            notes.append({"dataset": c["dataset"], "period_label": c["period_label"],
                          "expected": c["expected"], "present": c["present"],
                          "missing_count": c["missing_count"],
                          "missing_sample": c["missing"][:5]})
    return notes


def _period_filters(where: list, params: list, periods, start, end) -> None:
    if periods:
        where.append("list_contains(?, o.period_label)")
        params.append(list(periods))
    if start:
        where.append("o.period_start >= ?::DATE")
        params.append(start)
    if end:
        where.append("o.period_end <= ?::DATE")
        params.append(end)


def _context_filter(where: list, params: list, context: dict | None) -> None:
    for key, value in (context or {}).items():
        where.append(f"json_extract_string(o.context, '$.{_safe_key(key)}') = ?")
        params.append(str(value))


def _require_single_context(facts: list[dict], context: dict | None) -> None:
    contexts = {json.dumps(f["context"], sort_keys=True) for f in facts}
    if len(contexts) > 1 and not context:
        raise QueryError(f"Facts exist in several contexts ({', '.join(sorted(contexts))}); "
                         "pass context to pick one")


def _safe_key(key: str) -> str:
    if not _KEY.match(key):
        raise QueryError(f"Bad key {key!r}: use letters, digits and underscores")
    return key


def _limit(limit: int, cap: int = MAX_LIMIT) -> int:
    return max(1, min(int(limit), cap))


def _page(rows: list[dict], limit: int, offset: int) -> dict:
    more = len(rows) > limit
    return {"rows": rows[:limit], "has_more": more,
            "next_offset": offset + limit if more else None}


def _rows(con, sql: str, params: list | None = None) -> list[dict]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [{c: _plain(v) for c, v in zip(cols, row)} for row in cur.fetchall()]


def _one(con, sql: str, params: list) -> dict | None:
    rows = _rows(con, sql, params)
    return rows[0] if rows else None


def _clean(row: dict) -> dict:
    return {k: _plain(v) for k, v in row.items()}


def _plain(v):
    """Make a value JSON-friendly."""
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v
