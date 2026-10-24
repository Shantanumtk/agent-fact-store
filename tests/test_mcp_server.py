"""MCP server tests through a real in-process MCP client, on a store holding two unrelated
fields (cricket and economy) so every tool is checked to be field-agnostic."""

import asyncio
import json
import re

import pytest
from mcp import Client

from agent_fact_store import core
from agent_fact_store.mcp_server import build_server
from agent_fact_store.packs.cricket import cricsheet, derived
from agent_fact_store.packs.economy import fred
from tests.test_recipes import CPI, WAGE, match_in
from tests.test_cricsheet import write_zip

A1, A2 = cricsheet.entity_id("id-aone"), cricsheet.entity_id("id-atwo")


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("store")
    path = tmp / "facts.duckdb"
    con = core.connect(path)
    derived.BASE_PERIODS, derived.BASE_LABEL = ["2018"], "2018"
    try:
        cricsheet.load(con, write_zip(tmp / "m.zip", match_in(2018), match_in(2019, 4)),
                       source_url="test")
        derived.build(con)
    finally:
        derived.BASE_PERIODS = [str(y) for y in range(2006, 2026)]
        derived.BASE_LABEL = "2006-2025"
    (tmp / "cpi.csv").write_text(CPI)
    (tmp / "wage.csv").write_text(WAGE)
    fred.load(con, "CPIAUCSL", tmp / "cpi.csv", source_url="test")
    fred.load(con, "FEDMINNFRWG", tmp / "wage.csv", source_url="test")
    fred.inflation_adjust(con, "economy.federal_minimum_wage", "2020")
    con.close()
    return str(path)


def call(db, name, **args):
    async def go():
        async with Client(build_server(db)) as client:
            result = await client.call_tool(name, args)
            if result.is_error:
                return {"error": result.content[0].text}
            return result.structured_content
    return asyncio.run(go())


def tools(db):
    async def go():
        async with Client(build_server(db)) as client:
            return (await client.list_tools()).tools
    return asyncio.run(go())


def test_tools_are_read_only_structured_and_field_agnostic(db):
    listed = tools(db)
    assert len(listed) == 14
    domain_words = re.compile(r"\b(cricket|batting|bowling|players?|wickets?|runs|economy|"
                              r"wages?|inflation|cpi|eras?|salary|prices?)\b", re.I)
    for t in listed:
        assert t.annotations.read_only_hint is True, t.name
        assert t.output_schema is not None, t.name
        text = t.name + " " + (t.description or "") + " " + json.dumps(t.input_schema)
        assert not domain_words.search(text), f"{t.name} mentions a domain: " \
                                              f"{domain_words.search(text).group()}"


def test_list_domains_covers_both_fields(db):
    domains = {d["domain"]: d for d in call(db, "list_domains")["rows"]}
    assert set(domains) == {"cricket", "economy"}
    assert domains["economy"]["sources"] == ["derived", "fred"]


@pytest.mark.parametrize("query, expected", [("A Two", A2), ("USA", "economy.us")])
def test_search_entities_in_any_field(db, query, expected):
    assert call(db, "search_entities", query=query)["rows"][0]["entity_id"] == expected


def test_get_entity_shows_coverage_and_ids(db):
    e = call(db, "get_entity", scheme="cricsheet", identifier="id-atwo")
    assert e["entity_id"] == A2
    runs = next(c for c in e["coverage"] if c["metric_id"] == "cricket.batting.runs")
    assert runs["periods"] == ["2018", "2019", "covered"]
    assert "Unknown entity" in call(db, "get_entity", entity_id="nobody")["error"]


def test_describe_metric_links_variants(db):
    runs = call(db, "describe_metric", metric_id="cricket.batting.runs")
    assert "cricket.batting.runs_era_adjusted" in runs["used_to_derive"]
    wage = call(db, "describe_metric", metric_id="economy.federal_minimum_wage.real_2020")
    assert wage["derived_by"] == [{"operation": "derive.adjust",
                                   "inputs": ["economy.cpi", "economy.federal_minimum_wage"]}]
    assert [p["period_label"] for p in wage["periods"]] == ["1985", "2020"]


def test_list_metrics_search_and_paging(db):
    page = call(db, "list_metrics", query="adjusted", limit=2)
    assert len(page["rows"]) == 2 and page["has_more"] and page["next_offset"] == 2


def test_get_facts_carry_receipts(db):
    rows = call(db, "get_facts", entity_ids=[A2, "economy.us"],
                metric_ids=["cricket.batting.runs", "economy.federal_minimum_wage"],
                periods=["2019", "1985"])["rows"]
    assert {(r["entity_id"], r["value"]) for r in rows} == {(A2, 8.0), ("economy.us", 3.35)}
    for r in rows:
        assert r["license"] and r["source_id"] and r["commercial_use"] is True and r["fact_id"]


def test_get_facts_point_in_time(db):
    facts = call(db, "get_facts", entity_ids=[A2], metric_ids=["cricket.batting.runs"],
                 periods=["2019"], as_of="2000-01-01T00:00:00")
    assert facts["rows"] == [] and "get_entity" in facts["hint"]


def test_compare_entities(db):
    out = call(db, "compare", entity_ids=[A1, A2],
               metric_ids=["cricket.batting.runs", "cricket.bowling.wickets"], period="2018")
    assert out["rows"][1]["values"]["cricket.batting.runs"]["value"] == 10
    assert f"{A2}: cricket.bowling.wickets" in out["missing"]


def test_rank_with_sample_filter(db):
    out = call(db, "rank", metric_id="cricket.batting.runs", period="2018",
               entity_type="person",
               filters=[{"metric_id": "cricket.batting.balls_faced", "min": 3}])
    assert [(r["rank"], r["entity_id"]) for r in out["rows"]] == [(1, A2), (2, A1)]
    bad = call(db, "rank", metric_id="cricket.batting.runs", period="2018",
               filters=[{"metric_id": "x"}])
    assert "min and/or max" in bad["error"]


def test_rank_lower_is_better_sorts_ascending(db):
    out = call(db, "rank", metric_id="cricket.bowling.economy", period="2018")
    assert out["order"] == "asc"


def test_explain_fact_reaches_raw_sources(db):
    fact = call(db, "get_facts", entity_ids=["economy.us"],
                metric_ids=["economy.federal_minimum_wage.real_2020"], periods=["1985"])["rows"][0]
    out = call(db, "explain_fact", fact_id=fact["fact_id"])
    assert out["fact"]["pipeline"] == "derive.adjust"
    assert {i["role"] for i in out["inputs"]} == {"value", "reference", "base_reference"}
    assert all(i["raw_sha256"] for i in out["inputs"])
    assert [s["source_id"] for s in out["raw_sources"]] == ["fred"] and out["commercial_use"]


def test_fact_history(db):
    out = call(db, "get_fact_history", entity_id=A2, metric_id="cricket.batting.runs",
               period="2018")
    assert len(out["versions"]) == 1
    assert out["versions"][0]["produced_by_run"]["pipeline"] == "cricket.cricsheet"


def test_list_sources(db):
    sources = {s["source_id"]: s for s in call(db, "list_sources")["rows"]}
    assert sources["cricsheet"]["license"] == "ODC-By-1.0" and sources["fred"]["runs"] == 2


def test_run_sql_reads(db):
    out = call(db, "run_sql", sql="SELECT entity_id, value FROM current_observations "
                                  "WHERE metric_id = 'economy.cpi' ORDER BY value")
    assert out["rows"] == [["economy.us", 107.6], ["economy.us", 258.8]]


@pytest.mark.parametrize("sql, message", [
    ("DELETE FROM observations", "Only SELECT"),
    ("SELECT 1; SELECT 2", "one statement"),
    ("SELECT * FROM read_csv('/etc/hosts')", "disabled"),
    ("WITH x AS (SELECT 1) UPDATE sources SET name = 'x'", "Error"),
    ("SELECT * FROM nonexistent", "Error"),
])
def test_run_sql_is_locked_down(db, sql, message):
    assert message.lower() in call(db, "run_sql", sql=sql)["error"].lower()


def test_store_is_not_modified_by_queries(db):
    count = "SELECT (SELECT count(*) FROM observations), (SELECT count(*) FROM sources)"
    before = call(db, "run_sql", sql=count)["rows"]
    for sql in ("WITH x AS (SELECT 1) INSERT INTO sources VALUES ('x','x',null,'x',null,true,null)",
                "SELECT * FROM read_csv('/etc/hosts')"):
        assert "error" in call(db, "run_sql", sql=sql)
    assert call(db, "run_sql", sql=count)["rows"] == before
