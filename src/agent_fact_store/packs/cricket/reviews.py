"""Human reviews of cricket disputes, kept in code so they survive rebuilds.

Each review records that a person looked a disputed value up by hand and what they saw.
The figures they saw are evidence about the dispute; they are not loaded as facts.

    python -m agent_fact_store.packs.cricket.reviews --db data/facts.duckdb
"""

from __future__ import annotations

import argparse

from agent_fact_store import core, verify

CONTEXT = {"format": "Test"}
ESPNCRICINFO = "ESPNcricinfo player page, Test career stats, read by hand"
REVIEWER = "Shantanumtk"

# (cricsheet person ID, metric, verdict, what the reviewer saw, note)
REVIEWS = [
    ("4d622fee", "cricket.batting.runs", "subject_correct",
     {"matches": 2, "runs": 2}, "Kranti Gaud. Matches agree; Wikipedia's infobox has 1 run."),
    ("a63775d6", "cricket.batting.runs", "subject_correct",
     {"matches": 21, "runs": 136}, "Rahat Ali. Matches agree; Wikipedia's infobox has 94 runs."),
    ("8012d0b8", "cricket.batting.runs", "subject_correct",
     {"matches": 19, "runs": 145},
     "Kasun Rajitha. Runs match Cricsheet. ESPNcricinfo counts 19 Tests to Cricsheet's 18, "
     "so Cricsheet is missing one Test in which he scored no runs."),
    ("1274d7ab", "cricket.batting.runs", "undetermined",
     {"matches": 45, "runs": 2805},
     "Najmul Hossain Shanto has played 2 Tests more than either source holds, so career "
     "totals can't settle this. Wikipedia's 2,663 runs with 43 Tests looks like a partly "
     "updated infobox. Needs a match-by-match comparison."),
    ("df7ed601", "cricket.batting.runs", "undetermined",
     {"matches": 6, "runs": 258},
     "Screenshot did not show the player's name; taken to be T Kaitano from the order of the "
     "check list. ESPNcricinfo counts 6 Tests to both sources' 4, so career totals can't "
     "settle this."),
]
CHECKED_ON = "2026-10-24"


def apply(con) -> int:
    applied = 0
    for person, metric, verdict, observed, note in REVIEWS:
        key = verify.conflict_key(metric, f"cricket.player.{person}", "covered", "wikipedia",
                                  CONTEXT)
        try:
            verify.review_issue(con, key, verdict=verdict, checked_against=ESPNCRICINFO,
                                checked_on=CHECKED_ON, reviewer=REVIEWER, observed=observed,
                                note=note)
            applied += 1
        except ValueError:
            pass  # the dispute isn't present in this store (e.g. the sources now agree)
    return applied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="data/facts.duckdb")
    args = parser.parse_args()
    print(f"{apply(core.connect(args.db))} of {len(REVIEWS)} reviews applied")


if __name__ == "__main__":
    main()
