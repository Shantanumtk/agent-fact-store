"""Generic verification tests, in two unrelated fields."""

import pytest

from agent_fact_store import core, derive, verify


@pytest.fixture
def con():
    con = core.connect()
    core.register_source(con, "official", "Official record", "Public domain", True)
    core.register_source(con, "encyclopedia", "Encyclopedia", "CC-BY-SA-4.0", True,
                         upstream="statsite")
    core.register_source(con, "statsite", "Stats site", "Proprietary", False)
    core.register_source(con, "agency", "Statistics agency", "Public domain", True)
    core.register_source(con, "mirror", "Agency mirror", "Public domain", True, upstream="agency")
    for metric in ("score", "events", "gdp"):
        core.define_metric(con, metric, "any", metric, metric)
    for e in ("p1", "p2", "p3", "country"):
        core.upsert_entity(con, e, "any", "thing", e)
    return con


def put(con, entity, metric, period, value, source):
    return core.record_observation(con, entity, metric, value, source_id=source,
                                   period_label=period)


def test_agree_conflict_and_sample_differs(con):
    for e, ours, theirs in [("p1", 100, 100), ("p2", 90, 80), ("p3", 70, 75)]:
        put(con, e, "score", "career", ours, "official")
        put(con, e, "score", "career", theirs, "encyclopedia")
    # p3 has fewer events in our data: a coverage gap, not a conflict
    for e, ours, theirs in [("p1", 10, 10), ("p2", 9, 9), ("p3", 8, 9)]:
        put(con, e, "events", "career", ours, "official")
        put(con, e, "events", "career", theirs, "encyclopedia")

    result = verify.compare(con, "score", reference_source="encyclopedia", period="career",
                            sample_metric="events")
    assert (result["agree"], result["conflict"], result["sample_differs"]) == (1, 1, 1)
    p3 = con.execute("SELECT detail->>'likely_cause' FROM checks WHERE entity_id = 'p3'").fetchone()
    assert "coverage gap" in p3[0]
    # the conflict is registered as an issue, and both values are still there
    issue = con.execute("SELECT kind, entity_id FROM issues").fetchall()
    assert issue == [("conflict", "p2")]
    assert {f["value"] for f in core.get_facts(con, "p2", "score")} == {90, 80}


def test_tolerances(con):
    put(con, "p1", "score", "career", 55.08, "official")
    put(con, "p1", "score", "career", 55.1, "encyclopedia")
    assert verify.compare(con, "score", reference_source="encyclopedia", period="career",
                          abs_tolerance=0.01)["conflict"] == 1
    assert verify.compare(con, "score", reference_source="encyclopedia", period="career",
                          abs_tolerance=0.05)["agree"] == 1


def test_confidence_counts_only_independent_agreement(con):
    a = put(con, "country", "gdp", "2023", 100.0, "agency")
    put(con, "country", "gdp", "2023", 100.0, "mirror")        # copies the agency
    verify.compare(con, "gdp", reference_source="mirror", period="2023")
    assert verify.assess(con, [a])[a]["check_status"] == "unchecked"   # not independent

    put(con, "country", "gdp", "2023", 100.0, "official")
    verify.compare(con, "gdp", reference_source="official", period="2023")
    result = verify.assess(con, [a])[a]
    assert (result["check_status"], result["confidence"]) == ("corroborated", 0.75)


def test_conflict_with_independent_source_disputes_the_fact(con):
    a = put(con, "p1", "score", "career", 100, "official")
    put(con, "p1", "score", "career", 120, "encyclopedia")
    verify.compare(con, "score", reference_source="encyclopedia", period="career")
    assert verify.assess(con, [a])[a] == {"confidence": 0.25, "check_status": "disputed",
                                          "check_ids": [1]}


def test_derived_facts_are_checked_through_their_raw_sources(con):
    put(con, "p1", "score", "2018", 40, "official")
    put(con, "p1", "score", "2019", 60, "official")
    derive.aggregate(con, "score", over="periods", label="career", members={"entity_ids": ["p1"]})
    put(con, "p1", "score", "career", 100, "encyclopedia")
    verify.compare(con, "score", reference_source="encyclopedia", period="career")
    detail = con.execute("SELECT independent, detail->>'subject_sources' FROM checks").fetchone()
    assert detail == (True, '["official"]')


def test_different_period_labels_between_sources(con):
    put(con, "p1", "score", "covered", 100, "official")
    put(con, "p1", "score", "career", 100, "encyclopedia")
    result = verify.compare(con, "score", reference_source="encyclopedia", period="covered",
                            reference_period="career")
    assert result["agree"] == 1


def test_coverage_applies_by_scope(con):
    verify.record_coverage(con, source_id="official", domain="any", dataset="Matches",
                           scope={"context": {"format": "A"}, "attributes": {"group": "x"}},
                           periods=[{"period_label": "2006", "expected": 10, "present": 8,
                                     "missing": ["#3", "#7"]}],
                           method="sequential numbering")
    hit = verify.coverage_for(con, "any", {"format": "A"}, {"group": "x"}, "2006")
    assert hit[0]["missing_count"] == 2 and hit[0]["missing"] == ["#3", "#7"]
    assert verify.coverage_for(con, "any", {"format": "B"}, {"group": "x"}, "2006") == []
    assert verify.coverage_for(con, "any", {"format": "A"}, {"group": "x"}, "2007") == []


def test_issue_status_survives_reruns(con):
    verify.upsert_issue(con, key="k", kind="error", summary="first")
    con.execute("UPDATE issues SET status = 'reported'")
    verify.upsert_issue(con, key="k", kind="error", summary="second")
    assert con.execute("SELECT status, summary FROM issues").fetchone() == ("reported", "second")


def disputed(con):
    a = put(con, "p1", "score", "career", 100, "official")
    put(con, "p1", "score", "career", 120, "encyclopedia")
    verify.compare(con, "score", reference_source="encyclopedia", period="career")
    return a, verify.conflict_key("score", "p1", "career", "encyclopedia", {})


@pytest.mark.parametrize("verdict, status, confidence, issue_status", [
    ("subject_correct", "reviewed", 0.75, "resolved"),
    ("reference_correct", "refuted", 0.1, "resolved"),
    ("undetermined", "disputed", 0.25, "open"),
])
def test_human_review_settles_a_dispute(con, verdict, status, confidence, issue_status):
    fact, key = disputed(con)
    verify.review_issue(con, key, verdict=verdict, checked_against="the official book",
                        checked_on="2026-10-24", reviewer="tester", observed={"score": 100})
    result = verify.assess(con, [fact])[fact]
    assert (result["check_status"], result["confidence"]) == (status, confidence)
    assert con.execute("SELECT status FROM issues").fetchone()[0] == issue_status


def test_review_survives_rerunning_the_checks(con):
    fact, key = disputed(con)
    verify.review_issue(con, key, verdict="subject_correct", checked_against="book",
                        checked_on="2026-10-24", reviewer="tester")
    verify.compare(con, "score", reference_source="encyclopedia", period="career")
    verify.review_issue(con, key, verdict="subject_correct", checked_against="book",
                        checked_on="2026-10-24", reviewer="tester")   # re-applied: no duplicate
    assert verify.assess(con, [fact])[fact]["check_status"] == "reviewed"
    assert con.execute("SELECT count(*) FROM reviews").fetchone()[0] == 1
    assert con.execute("SELECT status FROM issues").fetchone()[0] == "resolved"


def test_review_needs_an_existing_issue(con):
    with pytest.raises(ValueError, match="no issue"):
        verify.review_issue(con, "conflict:nothing", verdict="subject_correct",
                            checked_against="x", checked_on="2026-10-24", reviewer="t")
