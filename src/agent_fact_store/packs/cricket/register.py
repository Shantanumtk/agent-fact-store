"""Cricsheet people register -> cricket entity registry.

Cricsheet's register (https://cricsheet.org/register/) lists every person in its data
with their IDs on other sites (ESPNcricinfo, CricketArchive, BCCI, Opta...) and the
other names they are known by. Loading it gives every cricket entity a full ID crosswalk,
including people with no ball-by-ball data yet, such as players from earlier eras.

    python -m agent_fact_store.packs.cricket.register --db data/facts.duckdb
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from agent_fact_store import core
from agent_fact_store.packs.cricket import cricsheet

PIPELINE = "cricket.cricsheet_register"
PIPELINE_VERSION = "0.1.0"
REGISTER_URL = "https://cricsheet.org/register/"
FILES = ("people.csv", "names.csv")


def download(raw_dir: Path) -> Path:
    """Fetch both register files into a new timestamped folder in the raw store."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = raw_dir / f"register_{stamp}"
    folder.mkdir(parents=True)
    for name in FILES:
        req = urllib.request.Request(REGISTER_URL + name,
                                     headers={"User-Agent": cricsheet.USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as resp:
            (folder / name).write_bytes(resp.read())
    return folder


def fingerprint(folder: Path) -> str:
    """One SHA-256 over both files, so the run pins the exact register snapshot."""
    h = hashlib.sha256()
    for name in FILES:
        h.update(f"{name}:{cricsheet.sha256(folder / name)}\n".encode())
    return h.hexdigest()


def scheme(column: str) -> str:
    """'key_cricinfo_2' -> 'cricinfo'. A second ID on the same site is a duplicate
    profile there, so it belongs to the same scheme."""
    return re.sub(r"_\d+$", "", column.removeprefix("key_"))


def read_register(folder: Path) -> tuple[list[dict], list[dict], list[dict]]:
    entities, ids, names = [], [], []
    with (folder / "people.csv").open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pid = row["identifier"]
            eid = cricsheet.entity_id(pid)
            entities.append({"entity_id": eid, "domain": "cricket", "entity_type": "person",
                             "canonical_name": row["unique_name"]})
            ids.append({"entity_id": eid, "scheme": "cricsheet", "identifier": pid,
                        "source_id": cricsheet.SOURCE_ID})
            for column, value in row.items():
                if column.startswith("key_") and value:
                    ids.append({"entity_id": eid, "scheme": scheme(column), "identifier": value,
                                "source_id": cricsheet.SOURCE_ID})
            for name in {row["name"], row["unique_name"]}:
                names.append({"entity_id": eid, "name": name, "source_id": cricsheet.SOURCE_ID})
    known = {e["entity_id"] for e in entities}
    with (folder / "names.csv").open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            eid = cricsheet.entity_id(row["identifier"])
            if eid in known:
                names.append({"entity_id": eid, "name": row["name"],
                              "source_id": cricsheet.SOURCE_ID})
    return entities, ids, names


def load(con, folder: Path) -> dict:
    """Load a register snapshot as one ingestion run."""
    cricsheet.register(con)
    run_id = core.start_run(con, cricsheet.SOURCE_ID, PIPELINE, PIPELINE_VERSION,
                            source_url=REGISTER_URL, raw_path=str(folder),
                            raw_sha256=fingerprint(folder))
    try:
        entities, ids, names = read_register(folder)
        core.upsert_entities(con, entities)
        new_ids = core.add_identifiers(con, ids, run_id=run_id)
        new_names = core.add_names(con, names, run_id=run_id)
    except Exception:
        core.finish_run(con, run_id, rows_written=0, status="failed")
        raise
    core.finish_run(con, run_id, rows_written=new_ids + new_names)
    return {"run_id": run_id, "people": len(entities), "identifiers": len(ids),
            "new_identifiers": new_ids, "new_names": new_names}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="data/facts.duckdb")
    parser.add_argument("--raw-dir", default="data/raw/cricsheet")
    parser.add_argument("--folder", help="use an already downloaded register folder")
    args = parser.parse_args()

    folder = Path(args.folder) if args.folder else download(Path(args.raw_dir))
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    result = load(core.connect(args.db), folder)
    print(f"run {result['run_id']}: {result['people']} people, "
          f"{result['identifiers']} outside IDs ({result['new_identifiers']} new), "
          f"{result['new_names']} new names")


if __name__ == "__main__":
    main()
