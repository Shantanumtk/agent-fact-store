# factlayer

[![tests](https://github.com/Shantanumtk/factlayer/actions/workflows/tests.yml/badge.svg)](https://github.com/Shantanumtk/factlayer/actions/workflows/tests.yml)

**Verified, comparable facts for AI agents, in one template that works for any field.**

AI agents are bad at numbers. They pull statistics from scattered sources, can't say where a figure came from, and compare values from different eras or contexts as if they were the same thing. factlayer is a data layer that fixes this at the source. Every fact is stored in one universal template, with its provenance and license attached and a slot for cross-source verification, ready for any agent to query.

Sports is the first field. The same template covers finance, health, film and anything else that can be described as *an entity, a metric, a time and a value*.

> **Status: early development.** Module 1 (the universal core schema) is complete and tested. See the [roadmap](#roadmap).

## What makes it different

Most of the plumbing (ingestion, storage, MCP serving) is standard, and factlayer uses standard tools for it. The value is in four things that no existing platform combines for public data:

| | Capability | What it means |
|---|---|---|
| 1 | **Provenance + license on every value** | Each number carries its source, URL, fetch time, license and pipeline run, so an agent can cite it and know whether it can be used commercially |
| 2 | **Cross-source verification** | The same fact is checked against independent sources and given a confidence score |
| 3 | **Agent-built pipelines** | Adding a new field means an AI agent writes the connector, mapping and checks, and a human approves them |
| 4 | **Adjustment engine** | Fair comparison is built in, with documented methods: era-adjusted stats, inflation-adjusted prices, split-adjusted share prices |

## One template, any field

| Template piece | Sports | Finance | Film |
|---|---|---|---|
| Entity | Player, team | Company, security | Film, actor |
| ID crosswalk | Cricsheet ID ↔ ESPNcricinfo ID | ISIN ↔ CUSIP ↔ ticker | IMDb ↔ TMDB |
| Observation | Kohli · Test runs · 2018 · 1,322 | Apple · revenue · FY2023 · $383.3B | Titanic · worldwide gross · 1997 |
| Adjustment | Era-adjusted average | Split-adjusted price | Inflation-adjusted gross |

## Architecture

```
SOURCES ──► CONNECTORS ──► RAW STORE ──► CORE TEMPLATE ──► VERIFICATION + ADJUSTMENT ──► MCP / API ──► agents, apps, dashboards
                                          entities
                                          identifiers (crosswalk)
                                          metrics (catalog)
                                          observations (+ provenance, license, versions)
```

The core schema lives in [`src/factlayer/schema.sql`](src/factlayer/schema.sql):

- **sources**: where data comes from, its license and attribution requirements
- **entities**: the real-world things being described
- **entity_identifiers**: maps every outside ID to one canonical ID
- **metrics**: every metric defined once, with its unit and method
- **ingestion_runs**: one row per pipeline execution
- **observations**: the facts, each with full provenance. Corrections are versioned rather than overwritten, so you can always see what a value was and when it changed.

## Quickstart

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

```python
from factlayer import core

con = core.connect("facts.duckdb")
core.register_source(con, "cricsheet", "Cricsheet", "ODC-By-1.0", commercial_use=True,
                     attribution="Data from Cricsheet (cricsheet.org)")
core.define_metric(con, "cricket.batting.runs", "cricket", "Runs scored",
                   "Total runs scored as a batter", unit="runs")
core.upsert_entity(con, "player:kohli", "cricket", "player", "Virat Kohli")
core.record_observation(con, "player:kohli", "cricket.batting.runs", 1322,
                        source_id="cricsheet", period_label="2018", context={"format": "Test"})

core.get_facts(con, "player:kohli")
# -> value 1322, with source, license, attribution, fetch time and run ID attached
```

## Roadmap

| Module | Scope | Status |
|---|---|---|
| M1 | Universal core schema with provenance, licensing and versioning | ✅ Done |
| M2 | Cricket connector (Cricsheet ball-by-ball data) | Next |
| M3 | Entity registry: cross-source ID matching | Planned |
| M4 | Adjustment engine v1: era-adjusted batting | Planned |
| M5 | MCP server so agents can query facts | Planned |
| M6 | Cross-source verification and confidence scores | Planned |
| M7 | Second field (finance: SEC EDGAR + FRED) to prove the template generalizes | Planned |
| M8 | Agent pipeline builder | Planned |

## Data and licensing

factlayer is built on open data and licensed APIs, and does not scrape sites whose terms prohibit it. Every source's license is recorded, and every fact inherits it, so downstream users can filter for commercially usable data and give correct attribution.
