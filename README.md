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

**Coverage caveat:** Cricsheet doesn't have every Test. Coverage is thin before about 2006 and still has gaps after it: Mohammad Yousuf's 2006, for example, shows 16 of his 19 innings. Comparisons with earlier eras will need another source, and coverage tracking is planned with verification (M6).

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

## Adjustment engine

Making values from different contexts comparable is the same problem in every field: era-adjusted stats in sport, inflation-adjusted prices in economics, split-adjusted share prices, per-capita figures. The engine in [`derive.py`](src/agent_fact_store/derive.py) solves it once, with three operations that know nothing about any field:

| Operation | What it does | Examples |
|---|---|---|
| `aggregate` | Sum or average a metric across entities or across periods | League totals, career totals, sector revenue |
| `ratio` | Divide one metric by another for the same entity and period | Batting average, GDP per capita, profit margin |
| `adjust` | `value / reference(period) × reference(base period)` | Era-adjusted runs, inflation-adjusted wages, split-adjusted prices |

Domain packs only compose these into recipes. Two recipes ship today, in two unrelated fields:

```bash
.venv/bin/python -m agent_fact_store.packs.cricket.derived --db data/facts.duckdb
# 22 derivation steps: 37670 inserted, 0 superseded, 0 unchanged

.venv/bin/python -m agent_fact_store.packs.economy.fred --db data/facts.duckdb
# minimum wage in 2025 dollars: 79 inserted, 0 unchanged, 8 skipped
```

**Cricket.** Batting and bowling averages, strike rates and economy rates, plus era-adjusted batting. A player's runs each year are rescaled by how that year's league batting average compares with the 2006–2025 average, so a run in a low-scoring year counts for more. Leaders over Cricsheet's coverage (men, 60+ innings):

| Player | Average | Era-adjusted |
|---|---|---|
| KC Sangakkara | 61.07 | 57.41 |
| HC Brook | 53.09 | 56.91 |
| SPD Smith | 55.70 | 56.22 |
| KS Williamson | 54.06 | 55.13 |
| JH Kallis | 54.05 | 50.96 |

The league baseline currently includes every batter, tail-enders too. A top-order baseline is a planned refinement.

**Economy.** [FRED](https://fred.stlouisfed.org) supplies the US consumer price index, and the same `adjust` operation turns any USD metric in any field into constant dollars. The 1968 federal minimum wage ($1.58 annual average) was worth $14.62 in 2025 dollars, double today's $7.25.

Every derived fact records:

- the exact input facts it was computed from, and their role (numerator, reference, base...)
- the operation, its parameters and the code version, through its run
- a license combined from all its inputs, with commercial use allowed only if every original source allows it

```python
fact = core.get_facts(con, "cricket.player.ba607b88", "cricket.batting.average_era_adjusted")[0]
core.lineage(con, fact["obs_id"])      # every step down to the raw Cricsheet facts
core.raw_sources(con, fact["obs_id"])  # the original sources and their licenses
```

If an input is corrected, rerunning the recipe creates a new version of every affected derived fact, even when the result happens to be the same, because it was computed from different facts.

## MCP server: facts for AI agents

[`mcp_server.py`](src/agent_fact_store/mcp_server.py) gives any MCP client (Claude Code, Claude Desktop and others) read-only access to the store. Its 14 tools work for every field: they know the template, not any domain, so a new domain pack needs no new tools. A test fails if a tool's name, description or parameters mention a field.

| Purpose | Tools |
|---|---|
| Discover | `list_domains`, `search_entities`, `get_entity` (with coverage), `list_metrics`, `describe_metric` (contexts, periods, how it's derived, what's derived from it) |
| Query | `get_facts` (period, date and context filters, point-in-time `as_of`, history, concise or detailed), `compare`, `rank` (with minimum-sample filters) |
| Trust | `explain_fact` (lineage down to raw files, plus checks against other sources), `get_fact_history`, `list_sources` (licenses, attribution, freshness), `get_coverage` (what's missing), `list_issues` (known errors and conflicts) |
| Escape hatch | `run_sql`: one read-only SELECT, no file or network access, row cap, 10-second limit |

Every value comes back with its source, license, whether commercial use is allowed, a confidence score and check status, and a `fact_id` that `explain_fact` traces to the raw files. Results also carry coverage notes when the data behind them is incomplete.

Add it to Claude Code:

```bash
claude mcp add agent-fact-store -- /path/to/agent-fact-store/.venv/bin/agent-fact-store-mcp \
  --db /path/to/agent-fact-store/data/facts.duckdb
```

Example answers from the real store:

| Question | Tool | Answer |
|---|---|---|
| Kohli, Smith, Root over all covered Tests | `compare` | Kohli 9,230 runs at 46.85 (era-adjusted 47.98); Smith 55.70 (56.22); Root 50.45 (52.67) |
| Best era-adjusted Test averages of 2018, min 15 innings | `rank` | Kohli 64.5, AD Mathews 57.6, BKG Mendis 54.4 |
| The 1968 US minimum wage in 2025 dollars | `get_facts` | $1.58 then, $14.62 in 2025 dollars, public domain |

The server opens the store read-only. Stop it before running pipelines, which need write access.

## Verification, confidence and coverage

[`verify.py`](src/agent_fact_store/verify.py) records evidence about facts without deciding what is true. Like the adjustment engine, it knows nothing about any field.

**Checks against other sources.** `verify.compare` compares the store's facts with another source's. Each pair is kept with one of three outcomes, and neither value is ever overwritten:

- `agree`: within tolerance
- `sample_differs`: the two sources count a different number of underlying events (matches, filings...), so the values aren't comparable. The likely cause is recorded: a coverage gap on our side, or an out-of-date reference
- `conflict`: same sample, different value. Registered as an issue

**Independence.** Every source records its upstream, the source it copies from. Wikipedia's cricket infoboxes mostly cite ESPNcricinfo, so Wikipedia is registered with that upstream. Agreement only counts between sources that share no upstream.

**Confidence** means "independent sources agree", never "this is true":

| Status | Confidence | When |
|---|---|---|
| `unchecked` | 0.5 | No check, or only against dependent sources |
| `corroborated` | 1 − 0.5^(k+1) | k independent sources agree (0.75 for one, 0.875 for two) |
| `not_comparable` | 0.5 | The sources count different samples |
| `disputed` | 0.25 | An independent source conflicts |

**Coverage.** Cricsheet records each match's official number, which is sequential, so missing numbers are missing matches. For men's Tests the store holds 896 of the 2,637 played: none of #1–#1577, and 164 gaps after that. Results carry these gaps as coverage notes.

**Issues register.** Known errors, gaps, conflicts and ambiguous identities, each with evidence and a triage status (open, reported, resolved, wontfix).

The second cricket source is Wikipedia's player infoboxes, matched to our players through Wikidata's ESPNcricinfo IDs (never by name). Each fact links to the exact article revision:

```bash
.venv/bin/python -m agent_fact_store.packs.cricket.wikipedia --db data/facts.duckdb
# run 29: 10031 players mapped, 1168 infoboxes loaded, 52 without Test stats, 2 ID conflicts
#   cricket.batting.runs: 639 agree, 501 sample differs, 18 conflict
```

What it found on real data:

| Player | Store (Cricsheet) | Wikipedia | Status |
|---|---|---|---|
| Virat Kohli | 123 Tests, 9,230 runs | 123 Tests, 9,230 runs | corroborated, 0.75 |
| Sachin Tendulkar | 82 Tests, 6,416 runs | 200 Tests, 15,921 runs | not comparable: Cricsheet starts in 2001 |
| Mithali Raj | 4 Tests | 12 Tests | not comparable: 129 women's Tests missing |
| Nazmul Hossain Shanto | 43 Tests, 2,424 runs | 43 Tests, 2,663 runs | disputed, 0.25 |

Settling disputes like these needs a third independent source. ESPNcricinfo would be the natural one, but its terms don't allow scraping.

## Lineage

Every value can be traced back to where it came from and through every change:

- **Origin:** each fact links to its ingestion run, which records the source URL, the untouched raw file kept in `data/raw/` and its SHA-256 fingerprint.
- **Code:** each run records the git commit that produced it (marked `-dirty` if there were uncommitted changes).
- **Changes:** a corrected value never overwrites the old one. The old version is kept, marked with when it was replaced and by which run, so you can compare the two runs' source files and code to see why it changed.
- **Mappings:** every ID and name in the registry records the run that first added it.
- **Calculations:** every derived fact links to the facts it was computed from, so `core.lineage()` walks any number back to the raw files. See [Adjustment engine](#adjustment-engine).

## Known source issues

These are also in the issues register (`list_issues`), with evidence.

- Cricsheet's `names.csv` lists "Anderson Cummins" as an alias of Pat Cummins (`ded9240e`), although Anderson Cummins is a separate player (`ad75edc3`). The alias is kept as published, with its provenance.
- Cricsheet's register has two entries for Margaret Ngoche (`a5ff11c2` and `f91abcd6`), which Wikidata maps to one person. The Wikidata IDs are left unlinked until this is resolved.
- Wikipedia infoboxes contain errors. Usman Salahuddin's lists 1 Test, 37 runs and an average of 27.29, which is impossible with at most two innings. 119 batting averages conflict with Cricsheet even where matches and runs agree, so these are kept as disputed rather than trusted either way.

## Roadmap

| Module | Scope | Status |
|---|---|---|
| M1 | Universal core schema with provenance, licensing and versioning | ✅ Done |
| M2 | Cricket connector (Cricsheet ball-by-ball data) | ✅ Done |
| M3 | Entity registry: cross-source ID matching, name search, change lineage | ✅ Done |
| M4 | Adjustment engine: generic aggregate, ratio and adjust, with lineage for derived facts; cricket and inflation recipes | ✅ Done |
| M5 | MCP server: 12 field-agnostic, read-only tools for agents | ✅ Done |
| M6 | Cross-source verification, confidence scores, coverage tracking and an issues register; Wikipedia as a second cricket source | ✅ Done |
| M7 | Second field (finance: SEC EDGAR + FRED) to prove the template generalizes | Planned |
| M8 | Agent pipeline builder | Planned |

## Data and licensing

Agent Fact Store is built on open data and licensed APIs, and does not scrape sites whose terms prohibit it. Every source's license is recorded, and every fact inherits it, so downstream users can filter for commercially usable data and give correct attribution.
