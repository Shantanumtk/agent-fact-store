"""Cricket recipes on top of the generic derivation engine.

Builds rates (averages, strike rates, economy) and era-adjusted batting from the counting
facts the Cricsheet connector loads. Every step is a generic derive operation; only the
choice of metrics, groups and base period is cricket-specific.

Era adjustment: a player's runs in a year are rescaled by how that year's league batting
average compares with the base period's:

    adjusted runs = runs / league average(year) * league average(base period)

The adjusted average is adjusted runs / dismissals, per year and over all covered years.
The league is every player of the same gender in the same format, so tail-enders count
too. Pooling only the top order would be a sharper baseline and is a planned refinement.

    python -m agent_fact_store.packs.cricket.derived --db data/facts.duckdb
"""

from __future__ import annotations

import argparse

from agent_fact_store import core, derive

# Cricsheet's Test coverage is close to complete from 2006. 2026 is still in progress.
BASE_PERIODS = [str(y) for y in range(2006, 2026)]
BASE_LABEL = "2006-2025"
ALL_LABEL = "covered"  # every period the sources cover, not necessarily a whole career

GROUPS = {
    "male": ("cricket.group.men", "Men's cricket, all players"),
    "female": ("cricket.group.women", "Women's cricket, all players"),
}

METRICS = [
    ("cricket.batting.average", "Batting average", "Runs per dismissal", "runs", True,
     "cricket.batting.runs / cricket.batting.dismissals"),
    ("cricket.batting.strike_rate", "Batting strike rate", "Runs per 100 balls faced",
     "runs per 100 balls", True, "cricket.batting.runs / cricket.batting.balls_faced * 100"),
    ("cricket.bowling.average", "Bowling average", "Runs conceded per wicket", "runs", False,
     "cricket.bowling.runs_conceded / cricket.bowling.wickets"),
    ("cricket.bowling.economy", "Economy rate", "Runs conceded per six legal balls",
     "runs per over", False, "cricket.bowling.runs_conceded / cricket.bowling.balls * 6"),
    ("cricket.batting.runs_era_adjusted", "Era-adjusted runs",
     "Runs rescaled to the base period's league batting average", "runs", True,
     f"cricket.batting.runs / league batting average (same year, gender, format) "
     f"* league batting average ({BASE_LABEL})"),
    ("cricket.batting.average_era_adjusted", "Era-adjusted batting average",
     f"Batting average expressed in {BASE_LABEL} terms", "runs", True,
     "cricket.batting.runs_era_adjusted / cricket.batting.dismissals"),
]

PERSONS = {"domain": "cricket", "entity_type": "person"}
RAW = "cricsheet"  # per-year counting facts as loaded, not totals derived from them


def build(con) -> dict:
    """Recompute every cricket derived fact. Unchanged results are not rewritten."""
    for metric_id, name, description, unit, higher, definition in METRICS:
        core.define_metric(con, metric_id, "cricket", name, description, unit=unit,
                           higher_is_better=higher, definition=definition)
    steps = {}

    # 1. league totals per year, and pooled over the base period
    for gender, (group_id, group_name) in GROUPS.items():
        core.upsert_entity(con, group_id, "cricket", "group", group_name,
                           {"members": {**PERSONS, "gender": gender}})
        members = {**PERSONS, "attributes": {"gender": gender}}
        for metric in ("cricket.batting.runs", "cricket.batting.dismissals"):
            steps[f"league {gender} {metric}"] = derive.aggregate(
                con, metric, over="entities", group_entity=group_id, members=members,
                source=RAW)
            steps[f"base {gender} {metric}"] = derive.aggregate(
                con, metric, over="periods", label=BASE_LABEL, periods=BASE_PERIODS,
                members={"entity_ids": [group_id]})

    # 2. player totals over everything covered
    for metric in ("cricket.matches", "cricket.batting.innings", "cricket.batting.runs",
                   "cricket.batting.dismissals", "cricket.batting.balls_faced",
                   "cricket.bowling.runs_conceded", "cricket.bowling.wickets",
                   "cricket.bowling.balls"):
        steps[f"covered {metric}"] = derive.aggregate(
            con, metric, over="periods", label=ALL_LABEL, members=PERSONS, source=RAW)

    # 3. rates, for players and leagues alike
    steps["batting average"] = derive.ratio(
        con, "cricket.batting.runs", "cricket.batting.dismissals", "cricket.batting.average",
        unit="runs")
    steps["strike rate"] = derive.ratio(
        con, "cricket.batting.runs", "cricket.batting.balls_faced",
        "cricket.batting.strike_rate", scale=100, unit="runs per 100 balls")
    steps["bowling average"] = derive.ratio(
        con, "cricket.bowling.runs_conceded", "cricket.bowling.wickets",
        "cricket.bowling.average", unit="runs")
    steps["economy"] = derive.ratio(
        con, "cricket.bowling.runs_conceded", "cricket.bowling.balls",
        "cricket.bowling.economy", scale=6, unit="runs per over")

    # 4. era adjustment against each player's own league
    for gender, (group_id, _) in GROUPS.items():
        steps[f"era-adjusted runs {gender}"] = derive.adjust(
            con, "cricket.batting.runs", "cricket.batting.runs_era_adjusted",
            reference_entity=group_id, reference_metric="cricket.batting.average",
            base_period=BASE_LABEL, members={**PERSONS, "attributes": {"gender": gender}},
            source=RAW)
    steps["covered era-adjusted runs"] = derive.aggregate(
        con, "cricket.batting.runs_era_adjusted", over="periods", label=ALL_LABEL,
        members=PERSONS)
    steps["era-adjusted average"] = derive.ratio(
        con, "cricket.batting.runs_era_adjusted", "cricket.batting.dismissals",
        "cricket.batting.average_era_adjusted", unit="runs", members=PERSONS)
    return steps


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="data/facts.duckdb")
    args = parser.parse_args()
    steps = build(core.connect(args.db))
    total = {k: sum(s.get(k, 0) for s in steps.values()) for k in ("inserted", "superseded",
                                                                     "unchanged")}
    print(f"{len(steps)} derivation steps: {total['inserted']} inserted, "
          f"{total['superseded']} superseded, {total['unchanged']} unchanged")


if __name__ == "__main__":
    main()
