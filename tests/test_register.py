"""Cricsheet people register tests on small synthetic register files."""

import pytest

from agent_fact_store import core
from agent_fact_store.packs.cricket import cricsheet, register
from tests.test_cricsheet import synthetic_match, write_zip

PEOPLE = """identifier,name,unique_name,key_cricinfo,key_cricinfo_2,key_cricketarchive,key_pulse
id-aone,A One,A One,1001,1002,,
id-atwo,A Two,A Two (2),2001,,ca-2,
id-old,DG Legend,DG Legend,9999,,ca-9,
"""
NAMES = """identifier,name
id-aone,Alpha One
id-old,Donald Legend
id-missing,Not In People
"""


@pytest.fixture
def folder(tmp_path):
    (tmp_path / "people.csv").write_text(PEOPLE)
    (tmp_path / "names.csv").write_text(NAMES)
    return tmp_path


@pytest.fixture
def con():
    return core.connect()


def test_register_builds_id_crosswalk(con, folder):
    register.load(con, folder)
    a1 = cricsheet.entity_id("id-aone")
    assert core.resolve(con, "cricinfo", "1001") == a1
    assert core.resolve(con, "cricinfo", "1002") == a1  # second profile, same scheme
    assert core.resolve(con, "cricketarchive", "ca-2") == cricsheet.entity_id("id-atwo")
    assert core.identifiers(con, a1) == {"cricinfo": ["1001", "1002"], "cricsheet": ["id-aone"]}


def test_people_without_match_data_still_get_entities(con, folder):
    register.load(con, folder)
    found = core.find_entities(con, "Donald Legend")
    assert found[0]["entity_id"] == cricsheet.entity_id("id-old")
    assert core.get_facts(con, found[0]["entity_id"]) == []


def test_names_for_unknown_people_are_skipped(con, folder):
    register.load(con, folder)
    assert core.find_entities(con, "Not In People") == []


def test_register_and_matches_agree_on_entities(con, folder, tmp_path):
    register.load(con, folder)
    cricsheet.load(con, write_zip(tmp_path / "m.zip", synthetic_match()), source_url="test")
    a2 = cricsheet.entity_id("id-atwo")
    name, attrs = con.execute("SELECT canonical_name, attributes FROM entities WHERE entity_id = ?",
                              [a2]).fetchone()
    assert name == "A Two (2)"          # register's name kept, not the scorecard name
    assert '"gender":"male"' in attrs.replace(" ", "")  # match data adds attributes
    assert core.resolve(con, "cricinfo", "2001") == a2
    assert core.get_facts(con, a2, "cricket.batting.runs")[0]["value"] == 10


def test_reloading_register_adds_nothing(con, folder):
    first = register.load(con, folder)
    second = register.load(con, folder)
    assert first["new_identifiers"] > 0
    assert (second["new_identifiers"], second["new_names"]) == (0, 0)


def test_mappings_record_the_run_that_added_them(con, folder):
    result = register.load(con, folder)
    run = con.execute("SELECT run_id FROM entity_identifiers WHERE identifier = '1001'").fetchone()
    assert run[0] == result["run_id"]
    sha = con.execute("SELECT raw_sha256 FROM ingestion_runs WHERE run_id = ?",
                      [result["run_id"]]).fetchone()[0]
    assert len(sha) == 64
