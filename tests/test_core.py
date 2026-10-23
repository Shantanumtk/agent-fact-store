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
