"""FRED connector: annual US economic series -> facts on the 'economy.us' entity.

FRED (Federal Reserve Bank of St. Louis, https://fred.stlouisfed.org) republishes series
from US government agencies. Only series whose original publisher is a US federal agency
are listed here, since those are public domain. Each series is downloaded as its own
ingestion run, with the CSV kept untouched in the raw store.

Also provides inflation adjustment: the generic derive.adjust operation with the
consumer price index as the reference series, usable on any USD metric in any field.

    python -m agent_fact_store.packs.economy.fred --db data/facts.duckdb
"""

from __future__ import annotations

import argparse
import csv
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

from agent_fact_store import core, derive
from agent_fact_store.packs.cricket.cricsheet import USER_AGENT, sha256

SOURCE_ID = "fred"
PIPELINE = "economy.fred"
PIPELINE_VERSION = "0.1.0"
ENTITY = "economy.us"
CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&fq=Annual&fam=avg"

SERIES = {
    # FRED id: (metric_id, name, description, unit, original publisher)
    "CPIAUCSL": ("economy.cpi", "Consumer price index",
                 "CPI for all urban consumers, all items, annual average",
                 "index 1982-84=100", "U.S. Bureau of Labor Statistics"),
    "FEDMINNFRWG": ("economy.federal_minimum_wage", "Federal minimum wage",
                    "Federal minimum hourly wage for nonfarm workers, annual average",
                    "USD per hour", "U.S. Department of Labor"),
}
CPI_METRIC = "economy.cpi"


def register(con) -> None:
    core.register_source(
        con, SOURCE_ID, "FRED, Federal Reserve Bank of St. Louis",
        "Public domain (U.S. federal government data)", commercial_use=True,
        homepage_url="https://fred.stlouisfed.org", license_url="https://fred.stlouisfed.org/legal/",
        attribution="Retrieved from FRED, Federal Reserve Bank of St. Louis; "
                    "original data from U.S. federal agencies",
    )
    core.upsert_entity(con, ENTITY, "economy", "country", "United States", {"iso3": "USA"})
    core.add_names(con, [{"entity_id": ENTITY, "name": n, "source_id": SOURCE_ID}
                         for n in ("United States", "USA", "US")])
    for series, (metric_id, name, description, unit, publisher) in SERIES.items():
        core.define_metric(con, metric_id, "economy", name,
                           f"{description}. Source: {publisher} (FRED {series})", unit=unit)


def download(series: str, raw_dir: Path) -> tuple[Path, str]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = raw_dir / f"{series}_{stamp}.csv"
    url = CSV_URL.format(series=series)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=90) as resp:
        path.write_bytes(resp.read())
    return path, url


def read_series(path: Path, series: str) -> list[dict]:
    """Annual rows from a FRED CSV. Blank or '.' values (no data yet) are skipped."""
    metric_id, unit = SERIES[series][0], SERIES[series][3]
    rows = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw = (row.get(series) or "").strip()
            if raw in ("", "."):
                continue
            year = int(row["observation_date"][:4])
            rows.append({"entity_id": ENTITY, "metric_id": metric_id, "period_label": str(year),
                         "period_start": date(year, 1, 1), "period_end": date(year, 12, 31),
                         "value": float(raw), "unit": unit})
    return rows


def load(con, series: str, path: Path, *, source_url: str) -> dict:
    register(con)
    run_id = core.start_run(con, SOURCE_ID, PIPELINE, PIPELINE_VERSION, source_url=source_url,
                            raw_path=str(path), raw_sha256=sha256(path),
                            params={"series": series})
    try:
        counts = core.record_observations(con, read_series(path, series), source_id=SOURCE_ID,
                                          run_id=run_id, source_url=source_url)
    except Exception:
        core.finish_run(con, run_id, rows_written=0, status="failed")
        raise
    core.finish_run(con, run_id, rows_written=counts["inserted"])
    return {"run_id": run_id, **counts}


def inflation_adjust(con, metric_id: str, base_year: str, members: dict | None = None) -> dict:
    """Express a USD metric in base_year dollars, for any field: wages, prices, revenues,
    box office. The output metric is '<metric_id>.real_<base_year>'."""
    output = f"{metric_id}.real_{base_year}"
    original = con.execute("SELECT domain, name, unit FROM metrics WHERE metric_id = ?",
                           [metric_id]).fetchone()
    if original is None:
        raise ValueError(f"unknown metric {metric_id!r}")
    core.define_metric(con, output, original[0], f"{original[1]} ({base_year} dollars)",
                       f"{original[1]} adjusted for US consumer price inflation to {base_year}",
                       unit=original[2],
                       definition=f"{metric_id} / economy.cpi(year) * economy.cpi({base_year})")
    return derive.adjust(con, metric_id, output, reference_entity=ENTITY,
                         reference_metric=CPI_METRIC, base_period=base_year,
                         members=members, match_context=False, source=None)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="data/facts.duckdb")
    parser.add_argument("--raw-dir", default="data/raw/fred")
    parser.add_argument("--base-year", default="2025")
    args = parser.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    con = core.connect(args.db)
    for series in SERIES:
        path, url = download(series, Path(args.raw_dir))
        result = load(con, series, path, source_url=url)
        print(f"run {result['run_id']} {series}: {result['inserted']} inserted, "
              f"{result['superseded']} superseded, {result['unchanged']} unchanged")
    adjusted = inflation_adjust(con, "economy.federal_minimum_wage", args.base_year)
    print(f"minimum wage in {args.base_year} dollars: {adjusted['inserted']} inserted, "
          f"{adjusted['unchanged']} unchanged, {adjusted['skipped']} skipped")


if __name__ == "__main__":
    main()
