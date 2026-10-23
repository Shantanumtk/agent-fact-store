# Agent Fact Store

[![tests](https://github.com/Shantanumtk/agent-fact-store/actions/workflows/tests.yml/badge.svg)](https://github.com/Shantanumtk/agent-fact-store/actions/workflows/tests.yml)

**Verified, comparable facts for AI agents, in one template that works for any field.**

AI agents are bad at numbers. They pull statistics from scattered sources, can't say where a figure came from, and compare values from different eras or contexts as if they were the same thing. Agent Fact Store is a data layer that fixes this at the source. Every fact is stored in one universal template, with its provenance and license attached and a slot for cross-source verification, ready for any agent to query.

Sports is the first field. The same template covers finance, health, film and anything else that can be described as *an entity, a metric, a time and a value*.

> **Status: early development.** Module 1 (the universal core schema) is complete and tested. See the [roadmap](#roadmap).

## What makes it different

Most of the plumbing (ingestion, storage, MCP serving) is standard, and Agent Fact Store uses standard tools for it. The value is in four things that no existing platform combines for public data:

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

The core schema lives in [`src/agent_fact_store/schema.sql`](src/agent_fact_store/schema.sql):

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
from agent_fact_store import core

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

## Load real data: cricket

The first domain pack loads [Cricsheet](https://cricsheet.org) ball-by-ball data and turns it into per-player, per-year batting and bowling facts:

```bash
.venv/bin/python -m agent_fact_store.packs.cricket.cricsheet --dataset tests --db data/facts.duckdb
# run 1: 920 matches, 1223 players, 2001-2026
# facts: 43357 inserted, 0 superseded, 0 unchanged
```

The downloaded archive is kept untouched in `data/raw/`, fingerprinted with SHA-256 and linked to every fact it produced. Re-running writes nothing if the data hasn't changed, and a corrected value supersedes the old one instead of overwriting it. `--dataset` also accepts `odis` and `t20is`.

Spot checks against official records:

| Player | Year | Loaded from Cricsheet |
|---|---|---|
| Virat Kohli | 2018 | 13 Tests, 1,322 runs, 24 dismissals |
| Joe Root | 2021 | 15 Tests, 1,708 runs, 28 dismissals |
| Pat Cummins | 2019 | 12 Tests, 59 wickets |
| R Ashwin | 2016 | 12 Tests, 72 wickets |

**Coverage caveat:** Cricsheet's Test coverage is close to complete from about 2006, partial for 2003–2005 and nearly empty before that. Comparisons with earlier eras will need another source.

## Entity registry

Load the people register first and every cricket person gets a full ID crosswalk, including people with no match data yet:

```bash
.venv/bin/python -m agent_fact_store.packs.cricket.register --db data/facts.duckdb
# run 1: 18651 people, 47207 outside IDs (47207 new), 26550 new names
```

```python
core.resolve(con, "cricinfo", "253802")          # -> 'cricket.player.ba607b88'
core.identifiers(con, "cricket.player.ba607b88")
# {'cricinfo': ['253802'], 'cricketarchive': ['101095'], 'cricsheet': ['ba607b88'], 'pulse': ['164']}

core.find_entities(con, "Mithali Raj")           # -> M Raj (matched by initials)
core.find_entities(con, "Virat Kholi")           # -> V Kohli (close spelling)
```

Registry rules:

- One outside ID never points at two entities. A batch that would cause that is rejected whole, with the conflicts listed.
- One entity may hold several IDs on the same site, since sites have duplicate profiles.
- Attributes from different sources are merged, not overwritten.
- Search ranks exact names, then whole words, then initials ("Mahendra Singh Dhoni" → "MS Dhoni"), then partial and fuzzy matches. Ties go to the entity with more facts.

## Lineage

Every value can be traced back to where it came from and through every change:

- **Origin:** each fact links to its ingestion run, which records the source URL, the untouched raw file kept in `data/raw/` and its SHA-256 fingerprint.
- **Code:** each run records the git commit that produced it (marked `-dirty` if there were uncommitted changes).
- **Changes:** a corrected value never overwrites the old one. The old version is kept, marked with when it was replaced and by which run, so you can compare the two runs' source files and code to see why it changed.
- **Mappings:** every ID and name in the registry records the run that first added it.

## Known source issues

- Cricsheet's `names.csv` lists "Anderson Cummins" as an alias of Pat Cummins (`ded9240e`), although Anderson Cummins is a separate player (`ad75edc3`). The alias is kept as published, with its provenance. Cross-source verification (M6) is meant to catch this kind of error.

## Roadmap

| Module | Scope | Status |
|---|---|---|
| M1 | Universal core schema with provenance, licensing and versioning | ✅ Done |
| M2 | Cricket connector (Cricsheet ball-by-ball data) | ✅ Done |
| M3 | Entity registry: cross-source ID matching, name search, change lineage | ✅ Done |
| M4 | Adjustment engine v1: era-adjusted batting, with lineage for calculated facts | Next |
| M5 | MCP server so agents can query facts | Planned |
| M6 | Cross-source verification and confidence scores | Planned |
| M7 | Second field (finance: SEC EDGAR + FRED) to prove the template generalizes | Planned |
| M8 | Agent pipeline builder | Planned |

## Data and licensing

Agent Fact Store is built on open data and licensed APIs, and does not scrape sites whose terms prohibit it. Every source's license is recorded, and every fact inherits it, so downstream users can filter for commercially usable data and give correct attribution.
