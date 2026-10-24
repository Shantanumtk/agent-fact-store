"""MCP server: read-only access to the fact store for AI agents.

The tools are field-agnostic. They know the universal template (entities, metrics,
periods, contexts, facts and their lineage) and nothing about any one field, so a new
domain pack needs no new tools: agents discover what exists through the catalog.

    python -m agent_fact_store.mcp_server --db data/facts.duckdb
"""

from __future__ import annotations

import argparse
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from agent_fact_store import api

INSTRUCTIONS = """\
A store of facts from many fields, every one with its source, license and lineage.

Typical workflow:
1. Discover: list_domains, then search_entities to turn names into entity IDs, and
   list_metrics / describe_metric to find metric IDs, the contexts they come in and the
   periods they cover. get_entity shows everything held for one entity.
2. Query: get_facts for values (including point-in-time with as_of), compare for several
   entities side by side, rank for leaderboards with minimum-sample filters.
3. Explain: every value has a fact_id. explain_fact shows how a value was computed,
   down to the raw source files, and how it fared against other sources;
   get_fact_history shows every version of a value.
4. Know the limits: each fact has a confidence and check_status. Confidence means
   "independent sources agree", not "true": 0.5 unchecked, 0.75 one independent source
   agrees, 0.875 two, 0.25 disputed. get_coverage shows what datasets are missing and
   list_issues shows known errors and conflicts. coverage_notes in results flag gaps
   that affect the facts returned.

When you present numbers, cite their source (list_sources gives attribution text) and
mention coverage limits that get_entity or describe_metric reveal. If commercial_use is
false for a fact, say so before it is used commercially. run_sql is a last resort: its
results only carry provenance through the columns you select.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                            openWorldHint=False)

EntityIds = Annotated[list[str], Field(description="Entity IDs from search_entities")]
MetricIds = Annotated[list[str], Field(description="Metric IDs from list_metrics")]
Context = Annotated[dict[str, str] | None, Field(
    description="Only facts whose context has these values. describe_metric lists the "
                "contexts a metric comes in")]
Limit = Annotated[int, Field(ge=1, le=api.MAX_LIMIT, description="Page size")]
Offset = Annotated[int, Field(ge=0, description="Rows to skip; use next_offset from the last page")]


def build_server(db_path: str) -> MCPServer:
    root = api.open_readonly(db_path)

    def call(fn, *args, **kwargs) -> dict[str, Any]:
        cur = root.cursor()  # one cursor per call: tool calls may run on several threads
        try:
            result = fn(cur, *args, **kwargs)
        except api.QueryError as e:
            raise ToolError(str(e)) from e
        finally:
            cur.close()
        return result if isinstance(result, dict) else {"rows": result}

    server = MCPServer("agent-fact-store", title="Agent Fact Store", instructions=INSTRUCTIONS)
    tool = lambda: server.tool(annotations=READ_ONLY, structured_output=True)  # noqa: E731

    @tool()
    def list_domains() -> dict[str, Any]:
        """List the fields of knowledge in the store, with how many entities, metrics and
        facts each holds, the entity types, the time span covered and the sources used.
        Start here to see what can be asked."""
        return call(api.list_domains)

    @tool()
    def search_entities(
        query: Annotated[str, Field(description="A name, full or partial, e.g. a person, "
                                                "team, company or country")],
        domain: Annotated[str | None, Field(description="Limit to one domain")] = None,
        entity_type: Annotated[str | None, Field(description="Limit to one entity type")] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        """Find entities by any name they are known by: exact names, words in a name,
        initials ('Jane Smith' finds 'J Smith') and close spellings. Results are ordered
        best match first, then by how much data is held. Returns entity IDs for other tools."""
        return call(api.search_entities, query, domain, entity_type, limit)

    @tool()
    def get_entity(
        entity_id: Annotated[str | None, Field(description="Entity ID")] = None,
        scheme: Annotated[str | None, Field(
            description="Outside ID scheme, used with identifier instead of entity_id")] = None,
        identifier: Annotated[str | None, Field(description="The outside ID itself")] = None,
    ) -> dict[str, Any]:
        """Everything known about one entity: names, IDs on other sites, attributes, and
        coverage, meaning which metrics exist for it, in which contexts and for which
        periods. Check coverage before drawing conclusions from gaps."""
        return call(api.get_entity, entity_id, scheme, identifier)

    @tool()
    def list_metrics(
        domain: Annotated[str | None, Field(description="Limit to one domain")] = None,
        query: Annotated[str | None, Field(
            description="Words to look for in the metric ID, name or description")] = None,
        limit: Limit = 50, offset: Offset = 0,
    ) -> dict[str, Any]:
        """Browse the metric catalog: every metric with its name, description, unit,
        whether higher is better, its formula if it is derived, and how many facts exist."""
        return call(api.list_metrics, domain, query, limit, offset)

    @tool()
    def describe_metric(metric_id: Annotated[str, Field(description="Metric ID")]) -> dict[str, Any]:
        """One metric in depth: the contexts it comes in, the periods it covers, the entity
        types it applies to, how it is derived (operation and input metrics), and which
        other metrics are derived from it, such as adjusted or per-unit variants."""
        return call(api.describe_metric, metric_id)

    @tool()
    def get_facts(
        entity_ids: EntityIds,
        metric_ids: Annotated[list[str] | None, Field(description="Metric IDs; all if omitted")] = None,
        periods: Annotated[list[str] | None, Field(
            description="Period labels, as listed by describe_metric or get_entity")] = None,
        start: Annotated[str | None, Field(description="Only periods starting on or after "
                                                       "this date (YYYY-MM-DD)")] = None,
        end: Annotated[str | None, Field(description="Only periods ending on or before "
                                                     "this date (YYYY-MM-DD)")] = None,
        context: Context = None,
        as_of: Annotated[str | None, Field(
            description="Point in time (ISO timestamp): return values as the store knew "
                        "them then, before any later corrections")] = None,
        include_history: Annotated[bool, Field(
            description="Also return replaced versions of each value")] = False,
        detail: Annotated[str, Field(description="'concise' or 'detailed' (adds fetch time, "
                                                  "run, attribution, version info)")] = "concise",
        limit: Limit = 100, offset: Offset = 0,
    ) -> dict[str, Any]:
        """Get values for entities, optionally narrowed by metric, period, date range and
        context. Every value comes with its source, license, commercial_use flag, whether
        it was derived, and a fact_id for explain_fact."""
        return call(api.get_facts, entity_ids, metric_ids, periods, start, end, context,
                    as_of, include_history, detail, limit, offset)

    @tool()
    def compare(
        entity_ids: EntityIds, metric_ids: MetricIds,
        period: Annotated[str, Field(description="One period label, e.g. a year or a "
                                                 "combined period like 'covered'")],
        context: Context = None,
    ) -> dict[str, Any]:
        """Put several entities side by side on the same metrics for one period. Missing
        values are listed rather than hidden. Each value has its own fact_id and source."""
        return call(api.compare, entity_ids, metric_ids, period, context)

    @tool()
    def rank(
        metric_id: Annotated[str, Field(description="Metric to rank by")],
        period: Annotated[str, Field(description="Period label")],
        context: Context = None,
        domain: Annotated[str | None, Field(description="Limit to one domain")] = None,
        entity_type: Annotated[str | None, Field(description="Limit to one entity type")] = None,
        attributes: Annotated[dict[str, str] | None, Field(
            description="Only entities with these attribute values (see get_entity)")] = None,
        filters: Annotated[list[dict[str, Any]] | None, Field(
            description="Conditions on other metrics for the same entity and period, e.g. "
                        "a minimum sample size: [{\"metric_id\": \"...\", \"min\": 30}]")] = None,
        order: Annotated[str, Field(description="'auto' uses the metric's higher_is_better; "
                                                 "or 'asc' / 'desc'")] = "auto",
        limit: Limit = 20, offset: Offset = 0,
    ) -> dict[str, Any]:
        """Leaderboard of entities by one metric in one period. Use filters to require a
        minimum sample on another metric so small samples don't top the list."""
        return call(api.rank, metric_id, period, context, domain, entity_type, attributes,
                    filters, order, limit, offset)

    @tool()
    def explain_fact(
        fact_id: Annotated[int, Field(description="fact_id from any other tool")],
        max_depth: Annotated[int, Field(ge=0, le=20, description="How many steps down "
                                                                  "the lineage to show")] = 2,
        max_nodes: Annotated[int, Field(ge=1, le=api.MAX_LIMIT)] = 100,
    ) -> dict[str, Any]:
        """Show how a value came to be: the facts it was computed from and their roles,
        the operation and parameters at each step, the code version, and the raw source
        files (with SHA-256 fingerprints) at the bottom, plus the licenses involved."""
        return call(api.explain_fact, fact_id, max_depth, max_nodes)

    @tool()
    def get_fact_history(
        entity_id: Annotated[str, Field(description="Entity ID")],
        metric_id: Annotated[str, Field(description="Metric ID")],
        period: Annotated[str, Field(description="Period label")],
        context: Context = None,
    ) -> dict[str, Any]:
        """Every version a value has had, with the run that produced each one and the run
        that replaced it, so you can tell whether a change came from the source data or
        from the code."""
        return call(api.get_fact_history, entity_id, metric_id, period, context)

    @tool()
    def list_sources() -> dict[str, Any]:
        """The sources behind the store: license, whether commercial use is allowed, the
        attribution to cite, how many facts each supplies and when each was last fetched."""
        return call(api.list_sources)

    @tool()
    def get_coverage(
        domain: Annotated[str | None, Field(description="Limit to one domain")] = None,
        dataset: Annotated[str | None, Field(description="Limit to one dataset")] = None,
        period: Annotated[str | None, Field(description="Period label, or 'all' for "
                                                        "whole-dataset totals")] = None,
        only_gaps: Annotated[bool, Field(description="Only periods with missing items")] = False,
        limit: Limit = 100, offset: Offset = 0,
    ) -> dict[str, Any]:
        """What each dataset should contain versus what the store holds, per period, with
        the missing items listed where they are known and how 'expected' was worked out.
        Use it before concluding that something didn't happen."""
        return call(api.get_coverage, domain, dataset, period, only_gaps, limit, offset)

    @tool()
    def list_issues(
        status: Annotated[str | None, Field(description="open, reported, resolved or wontfix")] = None,
        kind: Annotated[str | None, Field(description="error, gap, conflict or ambiguity")] = None,
        domain: Annotated[str | None, Field(description="Limit to one domain")] = None,
        entity_id: Annotated[str | None, Field(description="Limit to one entity")] = None,
        limit: Limit = 50, offset: Offset = 0,
    ) -> dict[str, Any]:
        """Known problems: errors found in sources, conflicts between sources, and
        ambiguous identities, each with evidence and a triage status."""
        return call(api.list_issues, status, kind, domain, entity_id, limit, offset)

    @tool()
    def run_sql(
        sql: Annotated[str, Field(description="One read-only SELECT or WITH query. Tables: "
                                              + api.SQL_TABLES)],
        limit: Annotated[int, Field(ge=1, le=api.MAX_LIMIT)] = 200,
    ) -> dict[str, Any]:
        """Last resort for questions the other tools can't express. Read-only, with no file
        or network access, a row cap and a time limit. Select obs_id to keep values
        traceable through explain_fact."""
        return call(api.run_sql, sql, limit)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="data/facts.duckdb")
    args = parser.parse_args()
    build_server(args.db).run("stdio")


if __name__ == "__main__":
    main()
