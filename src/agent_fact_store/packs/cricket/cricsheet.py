"""Cricsheet connector: ball-by-ball match files -> player batting and bowling facts per year.

Cricsheet (https://cricsheet.org) publishes every match it covers as one JSON file,
bundled into zip archives per format. This connector keeps the downloaded zip untouched
in the raw store, reads it without extracting, and writes one fact per
player x calendar year x format x metric. A match counts towards the year it started in.

Canonical player IDs are derived from Cricsheet's IDs for now, since Cricsheet's people
register already maps them to other sites. The entity registry module will own this later.

    python -m agent_fact_store.packs.cricket.cricsheet --dataset tests --db data/facts.duckdb
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from agent_fact_store import core

SOURCE_ID = "cricsheet"
PIPELINE = "cricket.cricsheet"
PIPELINE_VERSION = "0.1.0"
BASE_URL = "https://cricsheet.org/downloads/"
USER_AGENT = "agent-fact-store/0.1 (+https://github.com/Shantanumtk/agent-fact-store)"

DATASETS = {
    "tests": "tests_json.zip",
    "odis": "odis_json.zip",
    "t20is": "t20s_json.zip",
}

# Dismissals credited to the bowler. Run outs and obstructing the field are not.
BOWLER_WICKETS = {"bowled", "caught", "caught and bowled", "lbw", "stumped", "hit wicket"}
# Leaving the field without being out.
NOT_DISMISSED = {"retired hurt", "retired not out"}

METRICS = [
    # (metric_id, name, description, unit, higher_is_better)
    ("cricket.matches", "Matches", "Matches in which the player was in the playing XI",
     "matches", None),
    ("cricket.batting.innings", "Innings batted",
     "Innings in which the player came to the crease", "innings", None),
    ("cricket.batting.runs", "Runs scored", "Runs scored off the bat", "runs", True),
    ("cricket.batting.balls_faced", "Balls faced",
     "Deliveries faced, counting no-balls but not wides", "balls", None),
    ("cricket.batting.dismissals", "Dismissals",
     "Times out; retired hurt and retired not out do not count", "dismissals", False),
    ("cricket.batting.fours", "Fours", "Boundaries scoring four", "fours", True),
    ("cricket.batting.sixes", "Sixes", "Boundaries scoring six", "sixes", True),
    ("cricket.bowling.balls", "Balls bowled", "Legal deliveries bowled (no wides or no-balls)",
     "balls", None),
    ("cricket.bowling.runs_conceded", "Runs conceded",
     "Runs off the bat plus wides and no-balls; byes and leg byes are not charged",
     "runs", False),
    ("cricket.bowling.wickets", "Wickets",
     "Dismissals credited to the bowler (not run outs)", "wickets", True),
]

BATTING = ["innings", "runs", "balls_faced", "dismissals", "fours", "sixes"]
BOWLING = ["balls", "runs_conceded", "wickets"]


@dataclass
class Aggregate:
    stats: dict[tuple[str, int, str], Counter] = field(
        default_factory=lambda: defaultdict(Counter))  # (player_id, year, format)
    names: dict[str, str] = field(default_factory=dict)
    genders: dict[str, str] = field(default_factory=dict)
    coverage: Counter = field(default_factory=Counter)  # (year, format) -> matches
    unregistered: Counter = field(default_factory=Counter)  # names missing an ID


def download(dataset: str, raw_dir: Path) -> tuple[Path, str, str]:
    """Fetch a dataset zip into the raw store. Returns (path, sha256, url)."""
    url = BASE_URL + DATASETS[dataset]
    raw_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = raw_dir / f"{dataset}_{stamp}.zip"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp, path.open("wb") as out:
        while chunk := resp.read(1 << 20):
            out.write(chunk)
    return path, sha256(path), url


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def iter_matches(zip_path: Path):
    """Yield (filename, match) for every match JSON in the archive, without extracting."""
    with zipfile.ZipFile(zip_path) as zf:
        for name in sorted(zf.namelist()):
            if name.endswith(".json"):
                yield name, json.loads(zf.read(name))


def add_match(agg: Aggregate, match: dict) -> None:
    info = match["info"]
    year = int(info["dates"][0][:4])
    fmt = info["match_type"]
    registry = info["registry"]["people"]
    agg.coverage[(year, fmt)] += 1

    def pid(name: str) -> str | None:
        player_id = registry.get(name)
        if player_id is None:
            agg.unregistered[name] += 1
            return None
        agg.names.setdefault(player_id, name)
        agg.genders[player_id] = info["gender"]
        return player_id

    def bump(name: str, stat: str, n: int = 1) -> None:
        if n and (player_id := pid(name)):
            agg.stats[(player_id, year, fmt)][stat] += n

    for team_players in info["players"].values():
        for name in team_players:
            bump(name, "matches")

    for innings in match["innings"]:
        batted: set[str] = set()
        for over in innings.get("overs", []):
            for d in over["deliveries"]:
                batter, bowler = d["batter"], d["bowler"]
                batted.update((batter, d["non_striker"]))
                runs, extras = d["runs"], d.get("extras", {})
                is_wide, is_noball = "wides" in extras, "noballs" in extras

                bump(batter, "runs", runs["batter"])
                if not is_wide:
                    bump(batter, "balls_faced")
                if not runs.get("non_boundary"):
                    bump(batter, "fours", runs["batter"] == 4)
                    bump(batter, "sixes", runs["batter"] == 6)

                if not (is_wide or is_noball):
                    bump(bowler, "balls")
                bump(bowler, "runs_conceded",
                     runs["batter"] + extras.get("wides", 0) + extras.get("noballs", 0))

                for w in d.get("wickets", []):
                    batted.add(w["player_out"])
                    if w["kind"] not in NOT_DISMISSED:
                        bump(w["player_out"], "dismissals")
                    if w["kind"] in BOWLER_WICKETS:
                        bump(bowler, "wickets")
        for name in batted:
            bump(name, "innings")


def aggregate(zip_path: Path) -> Aggregate:
    agg = Aggregate()
    for _, match in iter_matches(zip_path):
        add_match(agg, match)
    return agg


def to_observations(agg: Aggregate) -> list[dict]:
    rows = []
    for (player_id, year, fmt), s in agg.stats.items():
        metrics = ["matches"]
        if s["innings"]:
            metrics += [f"batting.{m}" for m in BATTING]
        if s["balls"] or s["runs_conceded"]:
            metrics += [f"bowling.{m}" for m in BOWLING]
        for m in metrics:
            short = m.split(".")[-1]
            rows.append({
                "entity_id": entity_id(player_id), "metric_id": f"cricket.{m}",
                "period_label": str(year), "period_start": date(year, 1, 1),
                "period_end": date(year, 12, 31), "context": {"format": fmt},
                "value": s[short], "unit": next(u for mid, _, _, u, _ in METRICS
                                                  if mid == f"cricket.{m}"),
            })
    return rows


def entity_id(cricsheet_id: str) -> str:
    return f"cricket.player.{cricsheet_id}"


def register(con) -> None:
    core.register_source(
        con, SOURCE_ID, "Cricsheet", "ODC-By-1.0", commercial_use=True,
        homepage_url="https://cricsheet.org",
        license_url="https://opendatacommons.org/licenses/by/1-0/",
        attribution="Ball-by-ball data from Cricsheet (cricsheet.org), ODC-By 1.0",
    )
    for metric_id, name, description, unit, higher in METRICS:
        core.define_metric(con, metric_id, "cricket", name, description, unit=unit,
                           higher_is_better=higher)


def load(con, zip_path: Path, *, source_url: str, raw_sha256: str | None = None) -> dict:
    """Aggregate an archive and write it into the store as one ingestion run."""
    register(con)
    run_id = core.start_run(con, SOURCE_ID, PIPELINE, PIPELINE_VERSION, source_url=source_url,
                            raw_path=str(zip_path), raw_sha256=raw_sha256 or sha256(zip_path))
    try:
        agg = aggregate(zip_path)
        core.upsert_entities(con, [
            {"entity_id": entity_id(pid), "domain": "cricket", "entity_type": "person",
             "canonical_name": name, "attributes": {"gender": agg.genders[pid]}}
            for pid, name in agg.names.items()
        ], update_names=False)  # the people register owns canonical names
        core.add_identifiers(con, [
            {"entity_id": entity_id(pid), "scheme": "cricsheet", "identifier": pid,
             "source_id": SOURCE_ID}
            for pid in agg.names
        ], run_id=run_id)
        core.add_names(con, [
            {"entity_id": entity_id(pid), "name": name, "source_id": SOURCE_ID}
            for pid, name in agg.names.items()
        ], run_id=run_id)
        counts = core.record_observations(con, to_observations(agg), source_id=SOURCE_ID,
                                          run_id=run_id, source_url=source_url)
    except Exception:
        core.finish_run(con, run_id, rows_written=0, status="failed")
        raise
    core.finish_run(con, run_id, rows_written=counts["inserted"])
    return {"run_id": run_id, "players": len(agg.names),
            "matches": sum(agg.coverage.values()), "coverage": dict(agg.coverage),
            "unregistered_names": dict(agg.unregistered), **counts}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", choices=DATASETS, default="tests")
    parser.add_argument("--db", default="data/facts.duckdb")
    parser.add_argument("--raw-dir", default="data/raw/cricsheet")
    parser.add_argument("--zip", help="use an already downloaded archive instead of fetching")
    args = parser.parse_args()

    if args.zip:
        zip_path, url, digest = Path(args.zip), BASE_URL + DATASETS[args.dataset], None
    else:
        zip_path, digest, url = download(args.dataset, Path(args.raw_dir))
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    con = core.connect(args.db)
    result = load(con, zip_path, source_url=url, raw_sha256=digest)

    years = sorted({y for y, _ in result["coverage"]})
    print(f"run {result['run_id']}: {result['matches']} matches, {result['players']} players, "
          f"{years[0]}-{years[-1]}")
    print(f"facts: {result['inserted']} inserted, {result['superseded']} superseded, "
          f"{result['unchanged']} unchanged")
    if result["unregistered_names"]:
        print(f"skipped {len(result['unregistered_names'])} names with no Cricsheet ID")


if __name__ == "__main__":
    main()
