"""Verification, confidence and coverage: generic trust operations on the fact store.

Nothing here decides what is true. It records evidence:

- coverage: what a dataset should contain versus what the store holds, so answers can
  say what is missing.
- compare: checks facts against another source's facts. Each pair is kept with its
  outcome: agree, sample_differs (the two count a different number of underlying events,
  so they aren't comparable) or conflict. Neither value is ever overwritten.
- assess: turns checks into a confidence score with a published meaning.
- issues: a register of known errors, gaps, conflicts and ambiguities.

Confidence means "independent sources agree", never "this is true":

    no usable check, or only checks against dependent sources   0.5   unchecked
    agreement from k independent sources                        1 - 0.5^(k+1)
                                                                  (k=1: 0.75, k=2: 0.875)
    conflict with any independent source                        0.25  disputed

Two sources are independent unless they share an upstream (sources.upstream), or are
the same source. A derived fact's sources are the raw sources behind it.
"""

from __future__ import annotations

import json

from agent_fact_store import core
from agent_fact_store.derive import _current

SOURCE_ID = "checks"
VERSION = "0.1.0"


def register(con) -> None:
    core.register_source(con, SOURCE_ID, "Agent Fact Store checks", "Not applicable",
                         commercial_use=None)


# ---------------------------------------------------------------- coverage

def record_coverage(con, *, source_id: str, domain: str, dataset: str, scope: dict,
                    periods: list[dict], method: str) -> int:
    """Store a coverage assessment. Each period dict has period_label, expected (or None
    if unknown), present and optionally missing (a list of item descriptions)."""
    run_id = core.start_run(con, source_id, f"coverage.{domain}", VERSION,
                            params={"dataset": dataset, "scope": scope, "method": method})
    if periods:
        con.executemany(
            """INSERT INTO coverage (source_id, domain, dataset, scope, period_label,
                                     expected, present, missing, method, run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [[source_id, domain, dataset, json.dumps(scope, sort_keys=True), p["period_label"],
              p.get("expected"), p["present"], json.dumps(p.get("missing") or []), method,
              run_id]
             for p in periods])
    core.finish_run(con, run_id, rows_written=len(periods))
    return run_id


def coverage_for(con, domain: str, context: dict, attributes: dict,
                 period_label: str | None = None) -> list[dict]:
    """Coverage assessments that apply to facts with this context and entity attributes."""
    rows = con.execute(
        """SELECT source_id, dataset, scope, period_label, expected, present, missing, method
           FROM current_coverage WHERE domain = ?
             AND (? IS NULL OR period_label = ? OR period_label = 'all')""",
        [domain, period_label, period_label]).fetchall()
    out = []
    for source_id, dataset, scope, label, expected, present, missing, method in rows:
        scope = json.loads(scope)
        if _subset(scope.get("context", {}), context) and _subset(scope.get("attributes", {}), attributes):
            out.append({"source_id": source_id, "dataset": dataset, "period_label": label,
                        "expected": expected, "present": present,
                        "missing_count": None if expected is None else expected - present,
                        "missing": json.loads(missing), "method": method})
    return out


def _subset(small: dict, big: dict) -> bool:
    return all(str(big.get(k)) == str(v) for k, v in small.items())


# ---------------------------------------------------------------- checks

def compare(con, metric_id: str, *, reference_source: str, period: str,
            reference_period: str | None = None, reference_metric: str | None = None,
            members: dict | None = None, abs_tolerance: float = 0.0,
            rel_tolerance: float = 0.0, sample_metric: str | None = None) -> dict:
    """Check every current fact of metric_id for period against reference_source's fact
    for the same entity and context (optionally a different period label or metric, for
    sources that name things differently). sample_metric, if given, is a count of the
    underlying events (matches, filings...) used to tell coverage differences apart from
    real conflicts."""
    register(con)
    reference_period = reference_period or period
    reference_metric = reference_metric or metric_id
    refs = {(f["entity_id"], f["context"]): f
            for f in _current(con, reference_metric, members, reference_source)
            if f["period_label"] == reference_period}
    subjects = [f for f in _current_any_source(con, metric_id, members)
                if f["period_label"] == period and f["source_id"] != reference_source]
    samples = _samples(con, sample_metric, members, period, reference_period,
                       reference_source) if sample_metric else {}
    ref_group = _group(con, reference_source)

    run_id = core.start_run(con, SOURCE_ID, "verify.compare", VERSION, params={
        "metric": metric_id, "reference_source": reference_source, "period": period,
        "reference_period": reference_period, "reference_metric": reference_metric,
        "members": members, "abs_tolerance": abs_tolerance, "rel_tolerance": rel_tolerance,
        "sample_metric": sample_metric})
    counts = {"agree": 0, "sample_differs": 0, "conflict": 0, "no_reference": 0}
    rows = []
    for s in subjects:
        r = refs.get((s["entity_id"], s["context"]))
        if r is None:
            counts["no_reference"] += 1
            continue
        diff = s["value"] - r["value"]
        tolerance = max(abs_tolerance, rel_tolerance * abs(r["value"]))
        subject_groups = sorted({_group(con, src["source_id"])
                                 for src in core.raw_sources(con, s["obs_id"])})
        detail = {"subject_sources": subject_groups, "reference_group": ref_group,
                  "tolerance": tolerance, "context": json.loads(s["context"])}
        if abs(diff) <= tolerance:
            status = "agree"
        else:
            ours, theirs = samples.get((s["entity_id"], s["context"]), (None, None))
            if ours is not None and theirs is not None and ours != theirs:
                status = "sample_differs"
                detail.update({"sample_metric": sample_metric, "subject_sample": ours,
                               "reference_sample": theirs,
                               "likely_cause": "subject is missing events (coverage gap)"
                               if ours < theirs else "reference is out of date or "
                                                     "counts events the subject doesn't"})
            else:
                status = "conflict"
        counts[status] += 1
        rows.append([run_id, s["entity_id"], metric_id, s["obs_id"], r["obs_id"], s["value"],
                     r["value"], diff, status, ref_group not in subject_groups,
                     json.dumps(detail, sort_keys=True)])
        if status == "conflict":
            upsert_issue(con, key=f"conflict:{metric_id}:{s['entity_id']}:{period}:"
                                  f"{reference_source}:{s['context']}",
                         kind="conflict", source_id=reference_source,
                         domain=_domain(con, s["entity_id"]), entity_id=s["entity_id"],
                         metric_id=metric_id, period_label=period, run_id=run_id,
                         summary=f"{metric_id} {period}: store has {s['value']:g}, "
                                 f"{reference_source} has {r['value']:g}",
                         evidence={"subject_fact": s["obs_id"], "reference_fact": r["obs_id"],
                                   "difference": diff})
    if rows:
        con.executemany(
            """INSERT INTO checks (run_id, entity_id, metric_id, subject_obs_id,
                                   reference_obs_id, subject_value, reference_value,
                                   difference, status, independent, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
    core.finish_run(con, run_id, rows_written=len(rows))
    return {"run_id": run_id, **counts}


def assess(con, obs_ids: list[int]) -> dict[int, dict]:
    """Confidence and check status for facts, from the latest check against each reference
    source. See the module docstring for what the numbers mean."""
    if not obs_ids:
        return {}
    rows = con.execute(
        """SELECT subject_obs_id, status, independent, detail->>'reference_group' AS ref,
                  check_id
           FROM (SELECT *, row_number() OVER (
                     PARTITION BY subject_obs_id, detail->>'reference_group'
                     ORDER BY check_id DESC) AS rn
                 FROM checks WHERE list_contains(?, subject_obs_id))
           WHERE rn = 1""", [list(obs_ids)]).fetchall()
    by_fact: dict[int, list] = {}
    for obs_id, status, independent, ref, check_id in rows:
        by_fact.setdefault(obs_id, []).append((status, independent, ref, check_id))
    out = {}
    for obs_id in obs_ids:
        checks = by_fact.get(obs_id, [])
        independent = [c for c in checks if c[1]]
        agreeing = {c[2] for c in independent if c[0] == "agree"}
        if any(c[0] == "conflict" for c in independent):
            status, confidence = "disputed", 0.25
        elif agreeing:
            status, confidence = "corroborated", 1 - 0.5 ** (len(agreeing) + 1)
        elif any(c[0] == "sample_differs" for c in independent):
            status, confidence = "not_comparable", 0.5
        else:
            status, confidence = "unchecked", 0.5
        out[obs_id] = {"confidence": confidence, "check_status": status,
                       "check_ids": sorted(c[3] for c in checks)}
    return out


# ---------------------------------------------------------------- issues

def upsert_issue(con, *, key: str, kind: str, summary: str, source_id: str | None = None,
                 domain: str | None = None, entity_id: str | None = None,
                 metric_id: str | None = None, period_label: str | None = None,
                 evidence: dict | None = None, run_id: int | None = None,
                 status: str | None = None) -> None:
    """Record a known problem. Re-recording the same key refreshes its summary and
    evidence but keeps its status unless one is given, so triage isn't lost on reruns."""
    con.execute(
        """INSERT INTO issues (issue_key, kind, status, source_id, domain, entity_id,
                               metric_id, period_label, summary, evidence, run_id)
           VALUES (?, ?, coalesce(?, 'open'), ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (issue_key) DO UPDATE SET
               summary = excluded.summary, evidence = excluded.evidence,
               run_id = excluded.run_id, updated_at = now(),
               status = coalesce(?, issues.status)""",
        [key, kind, status, source_id, domain, entity_id, metric_id, period_label, summary,
         json.dumps(evidence or {}, sort_keys=True), run_id, status])


# ---------------------------------------------------------------- helpers

def _current_any_source(con, metric_id: str, members: dict | None) -> list[dict]:
    """Current facts for a metric from every source (derive._current insists on one)."""
    facts = []
    sources = [r[0] for r in con.execute(
        "SELECT DISTINCT source_id FROM current_observations WHERE metric_id = ?",
        [metric_id]).fetchall()]
    for source in sources:
        facts += _current(con, metric_id, members, source)
    return facts


def _samples(con, sample_metric: str, members: dict | None, period: str,
             reference_period: str, reference_source: str) -> dict:
    ours = {(f["entity_id"], f["context"]): f["value"]
            for f in _current_any_source(con, sample_metric, members)
            if f["period_label"] == period and f["source_id"] != reference_source}
    theirs = {(f["entity_id"], f["context"]): f["value"]
              for f in _current(con, sample_metric, members, reference_source)
              if f["period_label"] == reference_period}
    return {k: (ours.get(k), theirs.get(k)) for k in ours.keys() | theirs.keys()}


def _group(con, source_id: str) -> str:
    row = con.execute("SELECT coalesce(upstream, source_id) FROM sources WHERE source_id = ?",
                      [source_id]).fetchone()
    return row[0] if row else source_id


def _domain(con, entity_id: str) -> str | None:
    row = con.execute("SELECT domain FROM entities WHERE entity_id = ?", [entity_id]).fetchone()
    return row[0] if row else None
