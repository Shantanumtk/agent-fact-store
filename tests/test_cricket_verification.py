"""Cricket coverage, the Wikipedia connector and verification against it, on synthetic
files, plus the MCP view of the results."""

import asyncio
import copy
import json

import pytest
from mcp import Client

from agent_fact_store import api, core
from agent_fact_store.mcp_server import build_server
from agent_fact_store.packs.cricket import cricsheet, derived, register, wikipedia
from tests.test_cricsheet import synthetic_match, write_zip
from tests.test_register import NAMES, PEOPLE


def numbered(number, day, six=6):
    m = copy.deepcopy(synthetic_match(six))
    m["info"]["match_type_number"] = number
    m["info"]["dates"] = [day]
    return m


def test_coverage_periods_from_match_numbers():
    periods = cricsheet.coverage_periods(
        {100: "2005-12-01", 102: "2006-01-05", 105: "2006-03-01"}, "Test")
    by = {p["period_label"]: p for p in periods}
    assert (by["2005"]["expected"], by["2005"]["present"]) == (2, 1)   # 101 placed in 2005
    assert (by["2006"]["expected"], by["2006"]["present"]) == (4, 2)   # 103, 104
    assert by["all"]["expected"] == 105 and by["all"]["present"] == 3
    assert by["all"]["missing"][0] == "Test #1 to #99 (before coverage starts)"


def infobox(matches, runs, avg, cites="ESPNcricinfo"):
    return f"""{{{{Infobox cricketer
| name = X
| column1 = [[Test cricket|Test]]
| matches1 = {matches}
| runs1 = {runs}<ref>x</ref>
| bat avg1 = {avg}
| deliveries1 = 0
| wickets1 = –
| column2 = [[One Day International|ODI]]
| matches2 = 99
| source = {cites}
| date = 28 September 2026
}}}}"""


def test_parse_infobox_takes_the_test_column():
    box = wikipedia.parse_infobox(infobox(123, "9,230", "46.85"))
    assert box["stats"] == {"cricket.matches": 123, "cricket.batting.runs": 9230,
                            "cricket.batting.average": 46.85, "cricket.bowling.balls": 0}
    assert box["as_of"] == "28 September 2026"
    women = wikipedia.parse_infobox(infobox(12, 699, "43.68").replace(
        "[[Test cricket|Test]]", "[[Women's Test cricket|WTest]]"))
    assert women["stats"]["cricket.matches"] == 12
    assert wikipedia.parse_infobox("no box here") is None


@pytest.fixture
def store(tmp_path):
    con = core.connect(tmp_path / "facts.duckdb")
    (tmp_path / "people.csv").write_text(PEOPLE)
    (tmp_path / "names.csv").write_text(NAMES)
    register.load(con, tmp_path)
    # matches #1 and #3 are covered; #2 is missing
    cricsheet.load(con, write_zip(tmp_path / "m.zip", numbered(1, "2018-01-01"),
                                  numbered(3, "2018-06-01")), source_url="test")
    derived.build(con)

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "wikidata_mapping.json").write_text(json.dumps({"results": {"bindings": [
        {"item": {"value": f"http://www.wikidata.org/entity/Q{n}"},
         "cricinfo": {"value": cid}, "article": {"value": f"https://en.wikipedia.org/wiki/{t}"}}
        for n, cid, t in [(1, "1001", "A_One"), (2, "2001", "A_Two"), (3, "9999", "Legend")]]}}))
    pages = {
        # A One: Wikipedia counts 3 Tests (we hold 2), so runs differ for a coverage reason
        "A One": infobox(3, 20, "6.66"),
        # A Two: same 2 Tests, different runs: a genuine conflict
        "A Two": infobox(2, 21, "10.50"),
    }
    (wiki / "pages_0000.json").write_text(json.dumps({"query": {"pages": [
        {"title": t, "revisions": [{"revid": 100 + i, "timestamp": "2026-10-01T00:00:00Z",
                                    "slots": {"main": {"content": text}}}]}
        for i, (t, text) in enumerate(pages.items())]}}))
    loaded = wikipedia.load(con, wiki)
    checked = wikipedia.check(con)
    con.close()
    return str(tmp_path / "facts.duckdb"), loaded, checked


def test_wikipedia_facts_link_to_the_article_revision(store):
    path, loaded, _ = store
    assert loaded["pages"] == 2 and loaded["identifier_conflicts"] == 0
    con = api.open_readonly(path)
    url = con.execute("""SELECT source_url FROM current_observations
                         WHERE source_id = 'wikipedia' AND entity_id = 'cricket.player.id-atwo'
                         LIMIT 1""").fetchone()[0]
    assert url == "https://en.wikipedia.org/w/index.php?oldid=101"
    assert core.resolve(con, "wikidata", "Q2") == "cricket.player.id-atwo"


def test_checks_separate_coverage_gaps_from_conflicts(store):
    _, _, checked = store
    runs = checked["cricket.batting.runs"]
    assert (runs["sample_differs"], runs["conflict"]) == (1, 1)
    assert checked["cricket.matches"]["sample_differs"] == 1   # 2 held vs 3 on Wikipedia


def test_coverage_shows_the_missing_match(store):
    path, _, _ = store
    con = api.open_readonly(path)
    cov = api.get_coverage(con, domain="cricket", period="2018")["rows"][0]
    assert (cov["expected"], cov["present"]) == (3, 2)
    assert cov["missing"] == ["Test #2 (between 2018-01-01 and 2018-06-01)"]


def call(path, name, **args):
    async def go():
        async with Client(build_server(path)) as client:
            r = await client.call_tool(name, args)
            return {"error": r.content[0].text} if r.is_error else r.structured_content
    return asyncio.run(go())


def test_mcp_facts_carry_confidence_and_coverage_notes(store):
    path, _, _ = store
    facts = call(path, "get_facts", entity_ids=["cricket.player.id-atwo"],
                 metric_ids=["cricket.batting.runs"], periods=["covered"])
    ours = next(r for r in facts["rows"] if r["source_id"] == "derived")
    assert ours["check_status"] == "disputed" and ours["confidence"] == 0.25
    assert facts["coverage_notes"][0]["missing_count"] == 1

    explained = call(path, "explain_fact", fact_id=ours["fact_id"])
    assert explained["checks"][0]["reference_source"] == "wikipedia"
    assert explained["checks"][0]["reference_url"].endswith("oldid=101")


def test_mcp_lists_issues(store):
    path, _, _ = store
    kinds = {i["kind"] for i in call(path, "list_issues", domain="cricket")["rows"]}
    assert kinds == {"conflict", "error"}  # the runs conflict, and the curated alias error
    gaps = call(path, "get_coverage", only_gaps=True)["rows"]
    assert {g["period_label"] for g in gaps} == {"2018", "all"}
