"""Cricket and economy recipes, both built only from the generic derive operations."""

import copy

import pytest

from agent_fact_store import core
from agent_fact_store.packs.cricket import cricsheet, derived
from agent_fact_store.packs.economy import fred
from tests.test_cricsheet import synthetic_match, write_zip


def match_in(year, a2_six_runs=6):
    m = copy.deepcopy(synthetic_match(a2_six_runs))
    m["info"]["dates"] = [f"{year}-06-01"]
    return m


@pytest.fixture
def cricket(tmp_path, monkeypatch):
    # two seasons with different scoring, and the 2018 season as the base period
    monkeypatch.setattr(derived, "BASE_PERIODS", ["2018"])
    monkeypatch.setattr(derived, "BASE_LABEL", "2018")
    con = core.connect()
    cricsheet.load(con, write_zip(tmp_path / "m.zip", match_in(2018), match_in(2019, 4)),
                   source_url="test")
    derived.build(con)
    return con


def fact(con, entity, metric, period):
    return next(f for f in core.get_facts(con, entity, metric) if f["period_label"] == period)


def test_league_average_per_year(cricket):
    # 2018: 16 runs off the bat for 2 dismissals; 2019: the six becomes a four
    assert fact(cricket, "cricket.group.men", "cricket.batting.average", "2018")["value"] == 8.0
    assert fact(cricket, "cricket.group.men", "cricket.batting.average", "2019")["value"] == 7.0


def test_era_adjusted_runs_scale_by_league_average(cricket):
    a2 = cricsheet.entity_id("id-atwo")
    # 8 runs in 2019, when the league averaged 7, is worth 8 / 7 * 8 in 2018 terms
    adj = fact(cricket, a2, "cricket.batting.runs_era_adjusted", "2019")["value"]
    assert adj == pytest.approx(8 / 7 * 8)
    # the base year is unchanged by construction
    assert fact(cricket, a2, "cricket.batting.runs_era_adjusted", "2018")["value"] == 10


def test_covered_totals_and_rates(cricket):
    a2 = cricsheet.entity_id("id-atwo")
    assert fact(cricket, a2, "cricket.batting.runs", "covered")["value"] == 18
    assert fact(cricket, a2, "cricket.batting.average", "covered")["value"] == 9.0
    assert fact(cricket, a2, "cricket.batting.average_era_adjusted", "covered")["value"] == \
        pytest.approx((10 + 8 / 7 * 8) / 2)
    b1 = cricsheet.entity_id("id-bone")
    assert fact(cricket, b1, "cricket.bowling.economy", "2018")["value"] == pytest.approx(18 / 7 * 6)


def test_era_adjusted_average_traces_back_to_the_raw_file(cricket):
    a2 = cricsheet.entity_id("id-atwo")
    f = fact(cricket, a2, "cricket.batting.average_era_adjusted", "2019")
    tree = core.lineage(cricket, f["obs_id"])
    raw = [n for n in tree if not n["derived"]]
    assert raw and all(n["pipeline"] == "cricket.cricsheet" and n["raw_sha256"] for n in raw)
    assert {n["pipeline"] for n in tree} >= {"derive.ratio", "derive.adjust", "derive.aggregate",
                                             "cricket.cricsheet"}
    assert f["license"] == "ODC-By-1.0" and f["commercial_use"] is True
    assert "Cricsheet" in f["attribution"]


def test_rebuilding_writes_nothing(cricket):
    steps = derived.build(cricket)
    assert sum(s["inserted"] for s in steps.values()) == 0
    assert sum(s["superseded"] for s in steps.values()) == 0


CPI = "observation_date,CPIAUCSL\n1985-01-01,107.6\n2020-01-01,258.8\n2026-01-01,\n"
WAGE = "observation_date,FEDMINNFRWG\n1985-01-01,3.35\n2020-01-01,7.25\n"


def test_fred_inflation_adjustment(tmp_path):
    con = core.connect()
    (tmp_path / "cpi.csv").write_text(CPI)
    (tmp_path / "wage.csv").write_text(WAGE)
    assert fred.load(con, "CPIAUCSL", tmp_path / "cpi.csv", source_url="test")["inserted"] == 2
    fred.load(con, "FEDMINNFRWG", tmp_path / "wage.csv", source_url="test")
    fred.inflation_adjust(con, "economy.federal_minimum_wage", "2020")

    real = fact(con, "economy.us", "economy.federal_minimum_wage.real_2020", "1985")
    assert real["value"] == pytest.approx(3.35 / 107.6 * 258.8)
    assert real["license"] == "Public domain (U.S. federal government data)"
    assert core.find_entities(con, "USA")[0]["entity_id"] == "economy.us"


def test_inflation_adjustment_works_on_any_field(tmp_path):
    # the same helper adjusts a sports salary, with no economy-specific code involved
    con = core.connect()
    (tmp_path / "cpi.csv").write_text(CPI)
    fred.load(con, "CPIAUCSL", tmp_path / "cpi.csv", source_url="test")
    core.register_source(con, "contracts", "Contracts", "CC-BY-4.0", commercial_use=True)
    core.define_metric(con, "sport.salary", "sport", "Salary", "Annual salary", unit="USD")
    core.upsert_entity(con, "player", "sport", "person", "Player")
    core.record_observation(con, "player", "sport.salary", 1_000_000, source_id="contracts",
                            period_label="1985")
    fred.inflation_adjust(con, "sport.salary", "2020", members={"domain": "sport"})
    real = fact(con, "player", "sport.salary.real_2020", "1985")
    assert real["value"] == pytest.approx(1_000_000 / 107.6 * 258.8)
    assert real["license"] == "CC-BY-4.0 + Public domain (U.S. federal government data)"
