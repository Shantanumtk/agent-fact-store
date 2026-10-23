"""Generic derivation tests. Each operation is exercised in two unrelated fields to keep
the engine honest about being field-agnostic."""

import pytest

from agent_fact_store import core, derive


@pytest.fixture
def con():
    con = core.connect()
    core.register_source(con, "scores", "Scorebook", "ODC-By-1.0", commercial_use=True,
                         attribution="Scores from Scorebook")
    core.register_source(con, "stats_gov", "Statistics office", "Public domain",
                         commercial_use=True, attribution="Statistics office")
    core.register_source(con, "research", "Research set", "CC-BY-NC-4.0", commercial_use=False,
                         attribution="Research set (non-commercial)")
    for mid, domain in [("runs", "sport"), ("outs", "sport"), ("average", "sport"),
                        ("runs_adj", "sport"), ("average_adj", "sport"),
                        ("gdp", "economy"), ("population", "economy"),
                        ("gdp_per_capita", "economy"), ("price_index", "economy"),
                        ("wage", "economy"), ("real_wage", "economy")]:
        core.define_metric(con, mid, domain, mid, mid)
    return con


def put(con, entity, metric, period, value, source="scores", attrs=None, context=None):
    core.upsert_entity(con, entity, "sport" if source == "scores" else "economy",
                       "person" if source == "scores" else "country", entity, attrs)
    return core.record_observation(con, entity, metric, value, source_id=source,
                                   period_label=period, context=context)


def value(con, entity, metric, period):
    facts = [f for f in core.get_facts(con, entity, metric) if f["period_label"] == period]
    return facts[0]["value"] if facts else None


def test_ratio_in_two_fields(con):
    put(con, "kohli", "runs", "2018", 1322)
    put(con, "kohli", "outs", "2018", 24)
    put(con, "india", "gdp", "2023", 3.5e12, source="stats_gov")
    put(con, "india", "population", "2023", 1.43e9, source="stats_gov")

    derive.ratio(con, "runs", "outs", "average")
    derive.ratio(con, "gdp", "population", "gdp_per_capita", unit="USD")

    assert value(con, "kohli", "average", "2018") == pytest.approx(55.083, abs=1e-3)
    assert value(con, "india", "gdp_per_capita", "2023") == pytest.approx(2447.55, abs=0.01)


def test_ratio_skips_zero_and_small_denominators(con):
    put(con, "a", "runs", "2018", 50)
    put(con, "a", "outs", "2018", 0)
    put(con, "b", "runs", "2018", 50)
    put(con, "b", "outs", "2018", 2)
    result = derive.ratio(con, "runs", "outs", "average", min_denominator=5)
    assert result["skipped"] == 2
    assert value(con, "b", "average", "2018") is None


def test_aggregate_over_entities_and_periods(con):
    for who, gender, runs in [("a", "m", 100), ("b", "m", 300), ("c", "f", 50)]:
        put(con, who, "runs", "2018", runs, attrs={"gender": gender})
        put(con, who, "runs", "2019", runs * 2, attrs={"gender": gender})
    core.upsert_entity(con, "men", "sport", "group", "Men")

    derive.aggregate(con, "runs", over="entities", group_entity="men",
                     members={"entity_type": "person", "attributes": {"gender": "m"}})
    derive.aggregate(con, "runs", over="periods", label="career",
                     members={"entity_ids": ["a"]})

    assert value(con, "men", "runs", "2018") == 400   # a + b, not c
    assert value(con, "men", "runs", "2019") == 800
    career = [f for f in core.get_facts(con, "a", "runs") if f["period_label"] == "career"][0]
    assert career["value"] == 300


def test_aggregate_mean(con):
    for country, wage in [("x", 10.0), ("y", 20.0)]:
        put(con, country, "wage", "2020", wage, source="stats_gov")
    core.upsert_entity(con, "region", "economy", "group", "Region")
    derive.aggregate(con, "wage", over="entities", how="mean", group_entity="region",
                     members={"domain": "economy", "entity_type": "country"})
    assert value(con, "region", "wage", "2020") == 15.0


def test_adjust_is_the_same_operation_for_eras_and_inflation(con):
    # sport: a 60 average when the league averaged 40 equals a 45 average in a base
    # year when the league averaged 30
    core.upsert_entity(con, "league", "sport", "group", "League")
    put(con, "league", "average", "1990", 40.0)
    put(con, "league", "average", "2020", 30.0)
    put(con, "star", "average", "1990", 60.0)
    derive.adjust(con, "average", "average_adj", reference_entity="league",
                  reference_metric="average", base_period="2020",
                  members={"entity_type": "person"})
    assert value(con, "star", "average_adj", "1990") == pytest.approx(45.0)

    # economy: a $3.35 wage in 1985 at CPI 107.6, in 2020 dollars at CPI 258.8
    put(con, "us", "price_index", "1985", 107.6, source="stats_gov")
    put(con, "us", "price_index", "2020", 258.8, source="stats_gov")
    put(con, "us", "wage", "1985", 3.35, source="stats_gov")
    derive.adjust(con, "wage", "real_wage", reference_entity="us",
                  reference_metric="price_index", base_period="2020")
    assert value(con, "us", "real_wage", "1985") == pytest.approx(8.057, abs=1e-3)


def test_adjust_skips_periods_without_a_reference(con):
    core.upsert_entity(con, "league", "sport", "group", "League")
    put(con, "league", "average", "2020", 30.0)
    put(con, "star", "average", "1950", 99.0)
    result = derive.adjust(con, "average", "average_adj", reference_entity="league",
                           reference_metric="average", base_period="2020",
                           members={"entity_type": "person"})
    assert result["skipped"] == 1 and result["inserted"] == 0


def test_lineage_reaches_raw_facts_through_every_step(con):
    put(con, "kohli", "runs", "2018", 1322)
    put(con, "kohli", "outs", "2018", 24)
    core.upsert_entity(con, "league", "sport", "group", "League")
    put(con, "league", "average", "2018", 30.0)
    put(con, "league", "average", "2020", 33.0)
    derive.ratio(con, "runs", "outs", "average", members={"entity_type": "person"})
    derive.adjust(con, "average", "average_adj", reference_entity="league",
                  reference_metric="average", base_period="2020",
                  members={"entity_type": "person"})

    fact = core.get_facts(con, "kohli", "average_adj")[0]
    tree = core.lineage(con, fact["obs_id"])
    by_depth = {}
    for node in tree:
        by_depth.setdefault(node["depth"], []).append((node["metric_id"], node["role"]))
    assert by_depth[0] == [("average_adj", None)]
    assert sorted(by_depth[1]) == [("average", "base_reference"), ("average", "reference"),
                                   ("average", "value")]
    assert sorted(by_depth[2]) == [("outs", "denominator"), ("runs", "numerator")]
    assert tree[0]["pipeline"] == "derive.adjust" and '"base_period": "2020"' in tree[0]["params"]
    assert fact["derived"] and fact["commercial_use"] is True
    assert fact["attribution"] == "Scores from Scorebook"


def test_license_and_commercial_use_follow_the_inputs(con):
    put(con, "x", "gdp", "2023", 100.0, source="stats_gov")
    put(con, "x", "population", "2023", 4.0, source="research")
    derive.ratio(con, "gdp", "population", "gdp_per_capita")
    fact = core.get_facts(con, "x", "gdp_per_capita")[0]
    assert fact["license"] == "CC-BY-NC-4.0 + Public domain"
    assert fact["commercial_use"] is False  # one non-commercial input taints the result
    assert {s["source_id"] for s in core.raw_sources(con, fact["obs_id"])} == {
        "research", "stats_gov"}


def test_rerunning_a_derivation_writes_nothing(con):
    put(con, "kohli", "runs", "2018", 1322)
    put(con, "kohli", "outs", "2018", 24)
    first = derive.ratio(con, "runs", "outs", "average")
    second = derive.ratio(con, "runs", "outs", "average")
    assert (first["inserted"], second["inserted"], second["unchanged"]) == (1, 0, 1)


def test_corrected_input_creates_new_derived_version(con):
    put(con, "kohli", "runs", "2018", 1322)
    put(con, "kohli", "outs", "2018", 24)
    derive.ratio(con, "runs", "outs", "average")
    put(con, "kohli", "runs", "2018", 1320)          # source corrects the input
    result = derive.ratio(con, "runs", "outs", "average")
    history = core.get_facts(con, "kohli", "average", include_history=True)
    assert result["superseded"] == 1
    assert [round(f["value"], 3) for f in history] == [55.083, 55.0]
    assert history[0]["superseded_by_run"] == result["run_id"]


def test_new_input_version_with_same_result_is_still_a_new_version(con):
    put(con, "a", "runs", "2018", 100)
    put(con, "a", "outs", "2018", 4)
    derive.ratio(con, "runs", "outs", "average")
    put(con, "a", "runs", "2018", 100.0, source="research")   # second source, same value
    con.execute("UPDATE observations SET superseded_at = now() WHERE source_id = 'scores' "
                "AND metric_id = 'runs'")                    # first source's fact retired
    result = derive.ratio(con, "runs", "outs", "average")
    assert result["superseded"] == 1  # same 25.0, but computed from different input facts
    fact = core.get_facts(con, "a", "average")[0]
    assert {s["source_id"] for s in core.raw_sources(con, fact["obs_id"])} == {
        "research", "scores"}


def test_same_fact_from_two_sources_must_be_disambiguated(con):
    put(con, "a", "runs", "2018", 100)
    put(con, "a", "runs", "2018", 101, source="research")
    put(con, "a", "outs", "2018", 4)
    with pytest.raises(ValueError, match="pass source="):
        derive.ratio(con, "runs", "outs", "average")
