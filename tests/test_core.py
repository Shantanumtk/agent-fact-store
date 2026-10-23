import json

import pytest

from agent_fact_store import core


@pytest.fixture
def con():
    con = core.connect()
    core.register_source(con, "cricsheet", "Cricsheet", "ODC-By-1.0", commercial_use=True,
                         homepage_url="https://cricsheet.org",
                         attribution="Data from Cricsheet (cricsheet.org)")
    core.register_source(con, "sec_edgar", "SEC EDGAR", "Public domain (US Gov)",
                         commercial_use=True)
    core.define_metric(con, "cricket.batting.runs", "cricket", "Runs scored",
                       "Total runs scored as a batter", unit="runs", higher_is_better=True)
    core.define_metric(con, "finance.revenue", "finance", "Revenue",
                       "Total reported revenue", unit="USD", higher_is_better=True)
    return con


def test_same_template_holds_sports_and_finance(con):
    core.upsert_entity(con, "player:kohli", "cricket", "player", "Virat Kohli")
    core.upsert_entity(con, "company:apple", "finance", "company", "Apple Inc.")
    core.record_observation(con, "player:kohli", "cricket.batting.runs", 1322,
                            source_id="cricsheet", period_label="2018",
                            context={"format": "Test"}, unit="runs")
    core.record_observation(con, "company:apple", "finance.revenue", 383_285_000_000,
                            source_id="sec_edgar", period_label="FY2023", unit="USD")

    kohli = core.get_facts(con, "player:kohli")
    apple = core.get_facts(con, "company:apple")
    assert kohli[0]["value"] == 1322
    assert apple[0]["value"] == 383_285_000_000


def test_every_value_carries_provenance_and_license(con):
    core.upsert_entity(con, "player:kohli", "cricket", "player", "Virat Kohli")
    core.record_observation(con, "player:kohli", "cricket.batting.runs", 1322,
                            source_id="cricsheet", period_label="2018",
                            source_url="https://cricsheet.org/downloads/")
    fact = core.get_facts(con, "player:kohli")[0]
    assert fact["source_name"] == "Cricsheet"
    assert fact["license"] == "ODC-By-1.0"
    assert fact["commercial_use"] is True
    assert fact["attribution"].startswith("Data from Cricsheet")
    assert fact["fetched_at"] is not None


def test_unknown_source_is_rejected(con):
    core.upsert_entity(con, "player:kohli", "cricket", "player", "Virat Kohli")
    with pytest.raises(ValueError):
        core.record_observation(con, "player:kohli", "cricket.batting.runs", 1,
                                source_id="random_blog")


def test_corrections_keep_history(con):
    core.upsert_entity(con, "company:apple", "finance", "company", "Apple Inc.")
    core.record_observation(con, "company:apple", "finance.revenue", 100.0,
                            source_id="sec_edgar", period_label="FY2023")
    core.record_observation(con, "company:apple", "finance.revenue", 105.0,
                            source_id="sec_edgar", period_label="FY2023")  # restated

    current = core.get_facts(con, "company:apple")
    history = core.get_facts(con, "company:apple", include_history=True)
    assert [f["value"] for f in current] == [105.0]
    assert [f["value"] for f in history] == [100.0, 105.0]
    assert history[0]["superseded_at"] is not None


def test_rewriting_same_value_does_not_duplicate(con):
    core.upsert_entity(con, "player:kohli", "cricket", "player", "Virat Kohli")
    a = core.record_observation(con, "player:kohli", "cricket.batting.runs", 1322,
                                source_id="cricsheet", period_label="2018")
    b = core.record_observation(con, "player:kohli", "cricket.batting.runs", 1322,
                                source_id="cricsheet", period_label="2018")
    assert a == b
    assert len(core.get_facts(con, "player:kohli", include_history=True)) == 1


def test_id_crosswalk_resolves_outside_ids(con):
    core.upsert_entity(con, "player:kohli", "cricket", "player", "Virat Kohli")
    core.add_identifier(con, "player:kohli", "cricsheet", "example-id-001", source_id="cricsheet")
    core.add_identifier(con, "player:kohli", "cricinfo", "253802")
    assert core.resolve(con, "cricsheet", "example-id-001") == "player:kohli"
    assert core.resolve(con, "cricinfo", "253802") == "player:kohli"
    assert core.resolve(con, "cricinfo", "000") is None


def test_ingestion_run_links_to_facts(con):
    core.upsert_entity(con, "player:kohli", "cricket", "player", "Virat Kohli")
    run = core.start_run(con, "cricsheet", "cricket.cricsheet", "0.1.0")
    core.record_observation(con, "player:kohli", "cricket.batting.runs", 1322,
                            source_id="cricsheet", period_label="2018", run_id=run)
    core.finish_run(con, run, rows_written=1)
    assert core.get_facts(con, "player:kohli")[0]["run_id"] == run
    status = con.execute("SELECT status, rows_written FROM ingestion_runs").fetchone()
    assert status == ("success", 1)


def test_conflicting_identifier_is_rejected_and_nothing_written(con):
    core.upsert_entity(con, "player:kohli", "cricket", "player", "Virat Kohli")
    core.upsert_entity(con, "player:other", "cricket", "player", "Someone Else")
    core.add_identifier(con, "player:kohli", "cricinfo", "253802")
    with pytest.raises(core.IdentifierConflict):
        core.add_identifiers(con, [
            {"entity_id": "player:other", "scheme": "cricbuzz", "identifier": "1413"},
            {"entity_id": "player:other", "scheme": "cricinfo", "identifier": "253802"},
        ])
    assert core.resolve(con, "cricinfo", "253802") == "player:kohli"
    assert core.resolve(con, "cricbuzz", "1413") is None  # whole batch rejected


def test_one_id_claimed_by_two_entities_in_one_batch_is_rejected(con):
    for eid in ("a", "b"):
        core.upsert_entity(con, eid, "cricket", "person", eid)
    with pytest.raises(core.IdentifierConflict):
        core.add_identifiers(con, [{"entity_id": e, "scheme": "cricinfo", "identifier": "1"}
                                   for e in ("a", "b")])


def test_entity_can_hold_several_ids_in_one_scheme(con):
    core.upsert_entity(con, "p", "cricket", "person", "P")
    core.add_identifiers(con, [{"entity_id": "p", "scheme": "cricinfo", "identifier": i}
                               for i in ("1", "2")])
    assert core.identifiers(con, "p") == {"cricinfo": ["1", "2"]}


def test_attributes_from_two_sources_are_merged(con):
    core.upsert_entity(con, "p", "cricket", "person", "P", {"gender": "male"})
    core.upsert_entity(con, "p", "cricket", "person", "P", {"born": "1988-11-05"})
    attrs = con.execute("SELECT attributes FROM entities WHERE entity_id = 'p'").fetchone()[0]
    assert json.loads(attrs) == {"gender": "male", "born": "1988-11-05"}


def test_find_entities_by_any_name(con):
    core.upsert_entity(con, "player:kohli", "cricket", "person", "V Kohli")
    core.upsert_entity(con, "player:root", "cricket", "person", "JE Root")
    core.add_names(con, [{"entity_id": "player:kohli", "name": n}
                         for n in ("V Kohli", "Virat Kohli")])
    core.add_names(con, [{"entity_id": "player:root", "name": "JE Root"}])
    assert core.find_entities(con, "virat kohli")[0]["entity_id"] == "player:kohli"
    assert core.find_entities(con, "Kohli")[0]["entity_id"] == "player:kohli"
    assert core.find_entities(con, "Virat Kholi")[0]["entity_id"] == "player:kohli"  # typo
    assert core.find_entities(con, "Kohli", domain="finance") == []


def test_runs_record_code_version(con):
    run = core.start_run(con, "cricsheet", "p", "0.1.0")
    version = con.execute("SELECT code_version FROM ingestion_runs WHERE run_id = ?",
                          [run]).fetchone()[0]
    assert version and version != ""


@pytest.mark.parametrize("query, expected", [
    ("Mithali Raj", "M Raj"),
    ("Mahendra Singh Dhoni", "MS Dhoni"),
    ("Quinton de Kock", "Q de Kock"),
    ("Steve Smith", "SPD Smith"),
])
def test_find_entities_matches_full_names_to_initials(con, query, expected):
    for name in ("M Raj", "MS Dhoni", "Q de Kock", "SPD Smith", "MS Rajput"):
        core.upsert_entity(con, name, "cricket", "person", name)
        core.add_names(con, [{"entity_id": name, "name": name}])
    found = core.find_entities(con, query)
    assert found[0]["canonical_name"] == expected
    assert found[0]["match"] == "initials"
