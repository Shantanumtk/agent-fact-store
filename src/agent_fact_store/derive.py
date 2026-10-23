"""Derived facts: generic operations that compute new facts from existing ones.

Three operations cover most of what "making numbers comparable" means, in any field:

- aggregate: combine a metric across entities (a league, a sector, a country's regions)
  or across periods (a career, a decade), by sum or mean.
- ratio: divide one metric by another for the same entity, period and context
  (batting average, GDP per capita, profit margin).
- adjust: rescale a value by a reference series for the same period, relative to a base
  period: value / reference(period) * reference(base). Era-adjusted stats,
  inflation-adjusted prices and split-adjusted share prices are all this one operation.

Nothing here knows about any field. Domain packs compose these operations into recipes.

Every derived fact records its lineage: the exact input facts (fact_inputs), the operation
and parameters it ran with (its run), and the combined license of everything it was
computed from.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict

from agent_fact_store import core

SOURCE_ID = "derived"
VERSION = "0.1.0"
_ATTR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def register(con) -> None:
    core.register_source(
        con, SOURCE_ID, "Agent Fact Store derivations", "Inherited from inputs",
        commercial_use=None,  # worked out per fact from the raw sources behind it
        attribution=None,
    )


def aggregate(con, metric_id: str, *, over: str, how: str = "sum",
              output_metric: str | None = None, members: dict | None = None,
              group_entity: str | None = None, periods: list[str] | None = None,
              label: str | None = None, source: str | None = None) -> dict:
    """Combine a metric across entities or across periods.

    over="entities": one fact per period and context on group_entity, from every entity
    matching members. over="periods": one fact per entity and context, labelled label,
    from the periods listed (or all of them)."""
    if over not in ("entities", "periods") or how not in ("sum", "mean"):
        raise ValueError("over must be 'entities' or 'periods'; how must be 'sum' or 'mean'")
    if over == "entities" and not group_entity:
        raise ValueError("aggregating over entities needs a group_entity")
    if over == "periods" and not label:
        raise ValueError("aggregating over periods needs a label for the combined period")

    facts = _current(con, metric_id, members, source)
    if periods is not None:
        wanted = set(periods)
        facts = [f for f in facts if f["period_label"] in wanted]
    if over == "periods":
        # never fold an earlier result for the combined period back into itself
        facts = [f for f in facts if f["period_label"] != label]

    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for f in facts:
        key = (f["period_label"], f["context"]) if over == "entities" else (f["entity_id"], f["context"])
        buckets[key].append(f)

    out = []
    for key, group in buckets.items():
        values = [f["value"] for f in group]
        starts = [f["period_start"] for f in group if f["period_start"]]
        ends = [f["period_end"] for f in group if f["period_end"]]
        out.append({
            "entity_id": group_entity if over == "entities" else key[0],
            "metric_id": output_metric or metric_id,
            "period_label": key[0] if over == "entities" else label,
            "period_start": min(starts, default=None), "period_end": max(ends, default=None),
            "context": key[1], "unit": group[0]["unit"],
            "value": sum(values) if how == "sum" else sum(values) / len(values),
            "inputs": [(f, "member") for f in group],
        })
    return _write(con, "aggregate", out, {
        "metric": metric_id, "output_metric": output_metric or metric_id, "over": over,
        "how": how, "members": members, "group_entity": group_entity, "periods": periods,
        "label": label, "source": source})


def ratio(con, numerator: str, denominator: str, output_metric: str, *, scale: float = 1.0,
          unit: str | None = None, members: dict | None = None,
          min_denominator: float | None = None, source: str | None = None) -> dict:
    """numerator / denominator * scale, for every entity, period and context with both."""
    dens = {_key(f): f for f in _current(con, denominator, members, source)}
    out, skipped = [], 0
    for n in _current(con, numerator, members, source):
        d = dens.get(_key(n))
        if d is None or not d["value"] or (min_denominator is not None and d["value"] < min_denominator):
            skipped += 1
            continue
        out.append({**_period(n), "metric_id": output_metric, "unit": unit,
                    "value": n["value"] / d["value"] * scale,
                    "inputs": [(n, "numerator"), (d, "denominator")]})
    result = _write(con, "ratio", out, {
        "numerator": numerator, "denominator": denominator, "output_metric": output_metric,
        "scale": scale, "members": members, "min_denominator": min_denominator,
        "source": source})
    return {**result, "skipped": skipped}


def adjust(con, metric_id: str, output_metric: str, *, reference_entity: str,
           reference_metric: str, base_period: str, members: dict | None = None,
           match_context: bool = True, source: str | None = None) -> dict:
    """value / reference(period) * reference(base_period).

    The reference is a series on one entity: a league's average, a price index, a
    cumulative split factor. Values in periods the reference doesn't cover are skipped.
    source limits the values being adjusted, not the reference, which is often derived."""
    refs = {(f["period_label"], f["context"] if match_context else None): f
            for f in _current(con, reference_metric, {"entity_ids": [reference_entity]}, None)}
    out, skipped = [], 0
    for v in _current(con, metric_id, members, source):
        ctx = v["context"] if match_context else None
        ref, base = refs.get((v["period_label"], ctx)), refs.get((base_period, ctx))
        if ref is None or base is None or not ref["value"]:
            skipped += 1
            continue
        out.append({**_period(v), "metric_id": output_metric, "unit": v["unit"],
                    "value": v["value"] / ref["value"] * base["value"],
                    "inputs": [(v, "value"), (ref, "reference"), (base, "base_reference")]})
    result = _write(con, "adjust", out, {
        "metric": metric_id, "output_metric": output_metric,
        "reference_entity": reference_entity, "reference_metric": reference_metric,
        "base_period": base_period, "members": members, "match_context": match_context,
        "source": source})
    return {**result, "skipped": skipped}


def _key(f: dict) -> tuple:
    return (f["entity_id"], f["period_label"], f["context"])


def _period(f: dict) -> dict:
    return {k: f[k] for k in ("entity_id", "period_label", "period_start", "period_end", "context")}


def _current(con, metric_id: str, members: dict | None, source: str | None) -> list[dict]:
    """Current numeric facts for a metric, optionally limited to some entities and one
    source. Members may give entity_ids, domain, entity_type and attributes to match."""
    members = members or {}
    where, params = ["o.metric_id = ?", "o.value IS NOT NULL"], [metric_id]
    if source:
        where.append("o.source_id = ?")
        params.append(source)
    if "entity_ids" in members:
        where.append("list_contains(?, o.entity_id)")
        params.append(list(members["entity_ids"]))
    for column in ("domain", "entity_type"):
        if column in members:
            where.append(f"e.{column} = ?")
            params.append(members[column])
    for attr, value in (members.get("attributes") or {}).items():
        if not _ATTR.match(attr):
            raise ValueError(f"bad attribute name {attr!r}")
        where.append(f"json_extract_string(e.attributes, '$.{attr}') = ?")
        params.append(str(value))

    cur = con.execute(
        f"""SELECT o.obs_id, o.entity_id, o.period_label, o.period_start, o.period_end,
                   o.context::VARCHAR AS context, o.value, o.unit, o.license, o.source_id
            FROM current_observations o JOIN entities e USING (entity_id)
            WHERE {' AND '.join(where)}""", params)
    cols = [d[0] for d in cur.description]
    facts = [dict(zip(cols, row)) for row in cur.fetchall()]

    seen: dict[tuple, str] = {}
    for f in facts:
        if (other := seen.setdefault(_key(f), f["source_id"])) != f["source_id"]:
            raise ValueError(
                f"{metric_id} for {f['entity_id']} {f['period_label']} has current facts from "
                f"both {other!r} and {f['source_id']!r}; pass source= to choose one")
    return facts


def _write(con, operation: str, rows: list[dict], params: dict) -> dict:
    """Write derived facts as one run and link each new fact to its inputs."""
    register(con)
    run_id = core.start_run(con, SOURCE_ID, f"derive.{operation}", VERSION, params=params)
    try:
        for r in rows:
            inputs = r.pop("inputs")
            r["inputs"] = [(f["obs_id"], role) for f, role in inputs]
            r["license"] = " + ".join(sorted({f["license"] for f, _ in inputs}))
            r["inputs_hash"] = hashlib.sha256(
                "\n".join(sorted(f"{i}:{role}" for i, role in r["inputs"])).encode()).hexdigest()
            r["context"] = json.loads(r["context"]) if isinstance(r["context"], str) else r["context"]
        counts = core.record_observations(con, rows, source_id=SOURCE_ID, run_id=run_id)

        core._stage(con, "_links", [
            {"entity_id": r["entity_id"], "metric_id": r["metric_id"],
             "period_label": r["period_label"],
             "context": json.dumps(r["context"] or {}, sort_keys=True),
             "input_obs_id": obs_id, "role": role}
            for r in rows for obs_id, role in r["inputs"]
        ], {"entity_id": "VARCHAR", "metric_id": "VARCHAR", "period_label": "VARCHAR",
            "context": "VARCHAR", "input_obs_id": "BIGINT", "role": "VARCHAR"})
        con.execute(
            """INSERT OR IGNORE INTO fact_inputs (obs_id, input_obs_id, role)
               SELECT o.obs_id, l.input_obs_id, l.role
               FROM _links l JOIN observations o
                 ON o.run_id = ? AND o.entity_id = l.entity_id AND o.metric_id = l.metric_id
                AND o.period_label IS NOT DISTINCT FROM l.period_label
                AND o.context::VARCHAR = l.context""", [run_id])
        con.execute("DROP TABLE _links")
    except Exception:
        core.finish_run(con, run_id, rows_written=0, status="failed")
        raise
    core.finish_run(con, run_id, rows_written=counts["inserted"])
    return {"run_id": run_id, **counts}
