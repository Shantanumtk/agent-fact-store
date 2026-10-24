"""Wikipedia connector: career Test statistics from player infoboxes, as a second source.

Wikidata maps ESPNcricinfo player IDs to English Wikipedia articles, and the entity
registry already holds ESPNcricinfo IDs, so players are matched by ID, never by name.
Each article's "Infobox cricketer" gives career totals per format; the Test column is
loaded as facts for the period 'career'. Every fact links to the exact article revision.

Wikipedia's infoboxes mostly cite ESPNcricinfo, so the source is registered with that
upstream: agreement with Wikipedia is independent of Cricsheet, but not of ESPNcricinfo.

    python -m agent_fact_store.packs.cricket.wikipedia --db data/facts.duckdb
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

from agent_fact_store import core, verify
from agent_fact_store.packs.cricket import cricsheet

SOURCE_ID = "wikipedia"
MAPPING_SOURCE_ID = "wikidata"
PIPELINE = "cricket.wikipedia"
PIPELINE_VERSION = "0.1.0"
USER_AGENT = cricsheet.USER_AGENT
SPARQL_URL = "https://query.wikidata.org/sparql"
API_URL = "https://en.wikipedia.org/w/api.php"
MAPPING_QUERY = """SELECT ?item ?cricinfo ?article WHERE {
  ?item wdt:P2697 ?cricinfo .
  ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> .
}"""

# infobox field -> (metric, parse as)
FIELDS = {
    "matches": "cricket.matches",
    "runs": "cricket.batting.runs",
    "bat avg": "cricket.batting.average",
    "deliveries": "cricket.bowling.balls",
    "wickets": "cricket.bowling.wickets",
}


def register(con) -> None:
    core.register_source(
        con, SOURCE_ID, "Wikipedia (English), cricketer infoboxes", "CC-BY-SA-4.0",
        commercial_use=True, homepage_url="https://en.wikipedia.org",
        license_url="https://creativecommons.org/licenses/by-sa/4.0/",
        attribution="Wikipedia contributors; see the linked article revision",
        upstream="espncricinfo")
    core.register_source(
        con, MAPPING_SOURCE_ID, "Wikidata", "CC0-1.0", commercial_use=True,
        homepage_url="https://www.wikidata.org",
        license_url="https://creativecommons.org/publicdomain/zero/1.0/")


# ---------------------------------------------------------------- fetching

def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read()


def download(con, raw_dir: Path, batch_size: int = 50, pause: float = 1.0) -> Path:
    """Fetch the Wikidata mapping and the infoboxes of players with Test data, into a new
    timestamped raw folder. Returns the folder."""
    folder = raw_dir / f"wikipedia_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    folder.mkdir(parents=True)
    query = urllib.parse.urlencode({"query": MAPPING_QUERY, "format": "json"})
    (folder / "wikidata_mapping.json").write_bytes(_get(f"{SPARQL_URL}?{query}"))

    titles = sorted({m["title"] for m in _mapping(folder, con) if m["entity_id"] in _targets(con)})
    for i in range(0, len(titles), batch_size):
        params = urllib.parse.urlencode({
            "action": "query", "prop": "revisions", "rvprop": "content|ids|timestamp",
            "rvslots": "main", "format": "json", "formatversion": "2", "redirects": "1",
            "maxlag": "5", "titles": "|".join(titles[i:i + batch_size])})
        (folder / f"pages_{i // batch_size:04d}.json").write_bytes(_get(f"{API_URL}?{params}"))
        time.sleep(pause)
    return folder


def fingerprint(folder: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(folder.glob("*.json")):
        h.update(f"{path.name}:{cricsheet.sha256(path)}\n".encode())
    return h.hexdigest()


# ---------------------------------------------------------------- parsing

def _mapping(folder: Path, con) -> list[dict]:
    """Wikidata rows joined to our entities through ESPNcricinfo IDs."""
    data = json.loads((folder / "wikidata_mapping.json").read_text())
    ours = dict(con.execute(
        "SELECT identifier, entity_id FROM entity_identifiers WHERE scheme = 'cricinfo'").fetchall())
    out = []
    for b in data["results"]["bindings"]:
        entity_id = ours.get(b["cricinfo"]["value"])
        if entity_id:
            title = urllib.parse.unquote(b["article"]["value"].rsplit("/wiki/", 1)[1]).replace("_", " ")
            out.append({"entity_id": entity_id, "qid": b["item"]["value"].rsplit("/", 1)[1],
                        "title": title, "cricinfo": b["cricinfo"]["value"]})
    return out


def _targets(con) -> set[str]:
    return {r[0] for r in con.execute(
        """SELECT DISTINCT entity_id FROM current_observations
           WHERE source_id = 'cricsheet' AND metric_id = 'cricket.matches'
             AND context->>'format' = 'Test'""").fetchall()}


def _pages(folder: Path) -> dict[str, dict]:
    """title -> {revid, timestamp, wikitext}, following redirects and normalisation."""
    pages, aliases = {}, {}
    for path in sorted(folder.glob("pages_*.json")):
        q = json.loads(path.read_text())["query"]
        for step in q.get("normalized", []) + q.get("redirects", []):
            aliases[step["from"]] = step["to"]
        for p in q.get("pages", []):
            if "revisions" in p:
                r = p["revisions"][0]
                pages[p["title"]] = {"revid": r["revid"], "timestamp": r["timestamp"],
                                     "wikitext": r["slots"]["main"]["content"]}
    for src in list(aliases):
        dst = src
        while dst in aliases:
            dst = aliases[dst]
        if dst in pages:
            pages[src] = pages[dst]
    return pages


def clean(value: str) -> str:
    value = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>|<!--.*?-->", "", value, flags=re.S)
    value = re.sub(r"\{\{[^{}]*\}\}", "", value)
    value = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", value)
    return value.strip()


def number(value: str) -> float | None:
    m = re.match(r"^-?[\d,]+(?:\.\d+)?", clean(value).replace(" ", ""))
    return float(m.group().replace(",", "")) if m else None


def parse_infobox(wikitext: str) -> dict | None:
    """The Test column of an 'Infobox cricketer', or None if there isn't one."""
    start = wikitext.find("{{Infobox cricketer")
    if start < 0:
        return None
    fields = dict(re.findall(r"\n\s*\|\s*([^=\n|]+?)\s*=\s*([^\n]*)", wikitext[start:start + 20000]))
    column = next((k[len("column"):] for k, v in fields.items()
                   if re.fullmatch(r"column\d+", k) and re.search(r"\bW?Test\b", clean(v))), None)
    if column is None:
        return None
    stats = {metric: number(fields.get(f"{name}{column}", "")) for name, metric in FIELDS.items()}
    stats = {k: v for k, v in stats.items() if v is not None}
    return {"stats": stats, "as_of": clean(fields.get("date", "")),
            "cites": clean(fields.get("source", ""))[:200]}


def _as_of(text: str) -> date | None:
    for fmt in ("%d %B %Y", "%B %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------- loading

def load(con, folder: Path) -> dict:
    register(con)
    run_id = core.start_run(con, SOURCE_ID, PIPELINE, PIPELINE_VERSION, source_url=API_URL,
                            raw_path=str(folder), raw_sha256=fingerprint(folder))
    try:
        mapping = _mapping(folder, con)
        id_rows = [{"entity_id": m["entity_id"], "scheme": scheme, "identifier": value,
                    "source_id": MAPPING_SOURCE_ID}
                   for m in mapping for scheme, value in (("wikidata", m["qid"]),
                                                          ("enwiki", m["title"]))]
        skipped = _add_identifiers_skipping_conflicts(con, id_rows, run_id)

        pages, targets = _pages(folder), _targets(con)
        rows, no_infobox = [], 0
        for m in mapping:
            page = pages.get(m["title"])
            if m["entity_id"] not in targets or page is None:
                continue
            box = parse_infobox(page["wikitext"])
            if box is None or not box["stats"]:
                no_infobox += 1
                continue
            as_of = _as_of(box["as_of"])
            for metric_id, value in box["stats"].items():
                rows.append({"entity_id": m["entity_id"], "metric_id": metric_id,
                             "period_label": "career", "period_end": as_of,
                             "context": {"format": "Test"}, "value": value,
                             "source_url": f"https://en.wikipedia.org/w/index.php?oldid={page['revid']}"})
        counts = core.record_observations(con, rows, source_id=SOURCE_ID, run_id=run_id)
    except Exception:
        core.finish_run(con, run_id, rows_written=0, status="failed")
        raise
    core.finish_run(con, run_id, rows_written=counts["inserted"])
    return {"run_id": run_id, "mapped_players": len({m["entity_id"] for m in mapping}),
            "pages": len({r["source_url"] for r in rows}), "no_test_infobox": no_infobox,
            "identifier_conflicts": skipped, **counts}


def _add_identifiers_skipping_conflicts(con, rows: list[dict], run_id: int) -> int:
    """Add Wikidata/Wikipedia IDs. Rows that would make one ID point at two of our
    entities are left out and registered as issues: they mean either Wikidata or our
    registry has two records for one person, or one record for two people."""
    try:
        core.add_identifiers(con, rows, run_id=run_id)
        return 0
    except core.IdentifierConflict as e:
        bad = {(scheme, identifier) for scheme, identifier, _, _ in e.conflicts}
        for scheme, identifier, a, b in e.conflicts:
            verify.upsert_issue(
                con, key=f"identifier:{scheme}:{identifier}", kind="ambiguity",
                source_id=MAPPING_SOURCE_ID, domain="cricket", run_id=run_id,
                summary=f"{scheme} ID {identifier} maps to two entities: {a} and {b}",
                evidence={"scheme": scheme, "identifier": identifier, "entities": [a, b]})
        core.add_identifiers(con, [r for r in rows if (r["scheme"], r["identifier"]) not in bad],
                             run_id=run_id)
        return len(bad)


# ---------------------------------------------------------------- checks

CHECKS = [
    # (metric, absolute tolerance): Wikipedia rounds averages to two decimals
    ("cricket.matches", 0), ("cricket.batting.runs", 0),
    ("cricket.batting.average", 0.006), ("cricket.bowling.balls", 0),
    ("cricket.bowling.wickets", 0),
]


def check(con) -> dict:
    """Compare the store's covered-period totals with Wikipedia's career totals, using
    matches played to tell coverage gaps and stale infoboxes apart from real conflicts."""
    return {metric: verify.compare(con, metric, reference_source=SOURCE_ID, period="covered",
                                   reference_period="career", abs_tolerance=tol,
                                   members={"domain": "cricket", "entity_type": "person"},
                                   sample_metric="cricket.matches")
            for metric, tol in CHECKS}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="data/facts.duckdb")
    parser.add_argument("--raw-dir", default="data/raw/wikipedia")
    parser.add_argument("--folder", help="use an already downloaded folder")
    args = parser.parse_args()
    con = core.connect(args.db)
    register(con)
    folder = Path(args.folder) if args.folder else download(con, Path(args.raw_dir))
    result = load(con, folder)
    print(f"run {result['run_id']}: {result['mapped_players']} players mapped, "
          f"{result['pages']} infoboxes loaded, {result['no_test_infobox']} without Test stats, "
          f"{result['identifier_conflicts']} ID conflicts; facts {result['inserted']} inserted")
    for metric, c in check(con).items():
        print(f"  {metric}: {c['agree']} agree, {c['sample_differs']} sample differs, "
              f"{c['conflict']} conflict")


if __name__ == "__main__":
    main()
