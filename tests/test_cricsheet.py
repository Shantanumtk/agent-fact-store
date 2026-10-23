"""Cricsheet connector tests on a small synthetic match in Cricsheet's JSON format."""

import json
import zipfile

import pytest

from agent_fact_store import core
from agent_fact_store.packs.cricket import cricsheet


def delivery(batter, bowler, non_striker, bat=0, extras=None, wickets=None, **runs_flags):
    extras = extras or {}
    d = {"batter": batter, "bowler": bowler, "non_striker": non_striker,
         "runs": {"batter": bat, "extras": sum(extras.values()),
                  "total": bat + sum(extras.values()), **runs_flags}}
    if extras:
        d["extras"] = extras
    if wickets:
        d["wickets"] = wickets
    return d


def synthetic_match(a2_six_runs=6):
    team_a, team_b = ["A One", "A Two", "A Three"], ["B One", "B Two", "B Three"]
    registry = {name: f"id-{name.replace(' ', '').lower()}" for name in team_a + team_b}
    innings_a = [
        delivery("A One", "B One", "A Two", bat=4),                          # four
        delivery("A One", "B One", "A Two", extras={"wides": 1}),            # wide
        delivery("A One", "B One", "A Two", bat=2, extras={"noballs": 1}),   # no-ball, 2 off bat
        delivery("A One", "B One", "A Two", extras={"legbyes": 1}),          # leg bye
        delivery("A Two", "B One", "A One", bat=a2_six_runs),                # six
        delivery("A Two", "B One", "A One", bat=4, non_boundary=True),       # four, all run
        delivery("A Two", "B One", "A One",
                 wickets=[{"kind": "caught", "player_out": "A Two"}]),
        delivery("A Three", "B One", "A One",
                 wickets=[{"kind": "run out", "player_out": "A One"}]),      # non-striker run out
        delivery("A Three", "B One", "A One",
                 wickets=[{"kind": "retired hurt", "player_out": "A Three"}]),
    ]
    innings_b = [delivery("B One", "A One", "B Two")]
    return {
        "meta": {"data_version": "1.2.0"},
        "info": {"dates": ["2018-12-26"], "match_type": "Test", "gender": "male",
                 "teams": ["A", "B"], "players": {"A": team_a, "B": team_b},
                 "registry": {"people": registry}},
        "innings": [
            {"team": "A", "overs": [{"over": 0, "deliveries": innings_a}]},
            {"team": "B", "overs": [{"over": 0, "deliveries": innings_b}]},
        ],
    }


def write_zip(path, *matches):
    with zipfile.ZipFile(path, "w") as zf:
        for i, m in enumerate(matches):
            zf.writestr(f"{i}.json", json.dumps(m))
        zf.writestr("README.txt", "not a match")
    return path


@pytest.fixture
def con():
    return core.connect()


def stats(con, name):
    pid = cricsheet.entity_id(f"id-{name.replace(' ', '').lower()}")
    return {f["metric_id"].removeprefix("cricket."): f["value"]
            for f in core.get_facts(con, pid)}


def test_batting_rules(con, tmp_path):
    cricsheet.load(con, write_zip(tmp_path / "m.zip", synthetic_match()), source_url="test")
    a1, a2, a3 = stats(con, "A One"), stats(con, "A Two"), stats(con, "A Three")
    # runs off the bat only; wides are not balls faced, no-balls and leg byes are
    assert (a1["batting.runs"], a1["batting.balls_faced"]) == (6, 3)
    assert (a1["batting.fours"], a1["batting.dismissals"]) == (1, 1)  # run out at non-striker's end
    # an all-run four is not a boundary
    assert (a2["batting.runs"], a2["batting.fours"], a2["batting.sixes"]) == (10, 0, 1)
    # retired hurt is not a dismissal
    assert (a3["batting.innings"], a3["batting.dismissals"]) == (1, 0)


def test_bowling_rules(con, tmp_path):
    cricsheet.load(con, write_zip(tmp_path / "m.zip", synthetic_match()), source_url="test")
    b1 = stats(con, "B One")
    assert b1["bowling.balls"] == 7               # wide and no-ball are not legal balls
    assert b1["bowling.runs_conceded"] == 18      # leg bye not charged; wide and no-ball are
    assert b1["bowling.wickets"] == 1             # run out and retired hurt not credited


def test_players_who_did_not_bat_only_get_matches(con, tmp_path):
    cricsheet.load(con, write_zip(tmp_path / "m.zip", synthetic_match()), source_url="test")
    assert stats(con, "B Three") == {"matches": 1}
    assert stats(con, "B Two")["batting.innings"] == 1  # non-striker counts as batting


def test_facts_carry_provenance(con, tmp_path):
    result = cricsheet.load(con, write_zip(tmp_path / "m.zip", synthetic_match()),
                            source_url="https://cricsheet.org/downloads/tests_json.zip")
    fact = core.get_facts(con, cricsheet.entity_id("id-aone"), "cricket.batting.runs")[0]
    assert fact["period_label"] == "2018"
    assert json.loads(fact["context"]) == {"format": "Test"}
    assert fact["license"] == "ODC-By-1.0"
    assert fact["run_id"] == result["run_id"]
    assert core.resolve(con, "cricsheet", "id-aone") == cricsheet.entity_id("id-aone")
    run = con.execute("SELECT status, raw_sha256 FROM ingestion_runs").fetchone()
    assert run[0] == "success" and len(run[1]) == 64


def test_reloading_same_data_writes_nothing(con, tmp_path):
    z = write_zip(tmp_path / "m.zip", synthetic_match())
    first = cricsheet.load(con, z, source_url="test")
    second = cricsheet.load(con, z, source_url="test")
    assert first["inserted"] > 0
    assert (second["inserted"], second["unchanged"]) == (0, first["inserted"])


def test_source_correction_supersedes_old_value(con, tmp_path):
    cricsheet.load(con, write_zip(tmp_path / "a.zip", synthetic_match()), source_url="test")
    result = cricsheet.load(con, write_zip(tmp_path / "b.zip", synthetic_match(a2_six_runs=4)),
                            source_url="test")
    # A Two's runs and sixes and B One's runs conceded change; the four now counts as a four
    assert result["superseded"] == 4
    history = core.get_facts(con, cricsheet.entity_id("id-atwo"), "cricket.batting.runs",
                             include_history=True)
    assert [f["value"] for f in history] == [10, 8]
    # lineage: the old value points at the run that replaced it, the new one comes from it
    assert history[0]["superseded_by_run"] == result["run_id"] == history[1]["run_id"]
    assert history[1]["superseded_by_run"] is None
