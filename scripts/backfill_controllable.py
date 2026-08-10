#!/usr/bin/env python3
"""Backfill daily JSON files with controllable hub/spoke sortation metrics.

Usage (after agent runs bulk Sigma MCP queries and saves JSON responses):
  python3 scripts/backfill_controllable.py hub_detail.json spoke_facility.json hub_routes_okr.json

  hub_detail.json     — itgnfDcpNu hub sort rows: day, facility, route, met_sla, barcodes
  spoke_facility.json — itgnfDcpNu spoke sort rows: day, facility, otd_pct
  hub_routes_okr.json — qDt7Dz4HcO hub sort rows: day, facility, route, otd_pct

Preserves sla_stack_rank, pallet_inventory, and eos from each existing daily file.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

sys.path.insert(0, str(ROOT / "scripts"))
from pull_daily import build_daily_payload, parse_day, pct_from_sigma  # noqa: E402


def load_query(path: Path) -> list:
    data = json.loads(path.read_text())
    return data.get("rows", data)


def index_hub_facility(rows):
    """Aggregate facility OTD from itgnfDcpNu route-level rows."""
    totals = defaultdict(lambda: [0, 0])  # met_sla, barcodes
    for day, facility, _route, met_sla, barcodes in rows:
        key = (parse_day(day), facility)
        totals[key][0] += int(met_sla or 0)
        totals[key][1] += int(barcodes or 0)

    out = defaultdict(dict)
    for (day, facility), (met, bc) in totals.items():
        if bc > 0:
            out[day][facility] = pct_from_sigma(met / bc)
    return out


def index_spoke_facility(rows):
    out = defaultdict(dict)
    for day, facility, otd in rows:
        out[parse_day(day)][facility] = pct_from_sigma(otd)
    return out


def index_hub_routes(rows):
    out = defaultdict(lambda: defaultdict(dict))
    for day, facility, route, otd in rows:
        if route:
            out[parse_day(day)][facility][route] = pct_from_sigma(otd)
    return out


def stack_rows_from_existing(existing: dict) -> list:
    rows = []
    for item in existing.get("sla_stack_rank") or []:
        rows.append(
            (f"{item['step']}: {item['from_stop']} > {item['next_stop']}", item["miss_count"])
        )
    return rows


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)

    hub_facility = index_hub_facility(load_query(Path(sys.argv[1])))
    spoke_facility = index_spoke_facility(load_query(Path(sys.argv[2])))
    hub_routes = index_hub_routes(load_query(Path(sys.argv[3])))

    index = json.loads((DATA / "index.json").read_text())
    dates = index.get("dates", [])
    updated = 0
    skipped = []

    for date in dates:
        path = DATA / f"{date}.json"
        if not path.exists():
            skipped.append(date)
            continue

        existing = json.loads(path.read_text())
        payload = build_daily_payload(
            date,
            hub_facility.get(date, {}),
            hub_routes.get(date, {}),
            spoke_facility.get(date, {}),
            stack_rows_from_existing(existing),
            existing.get("pallet_inventory") or {},
        )
        if existing.get("eos"):
            payload["eos"] = existing["eos"]

        path.write_text(json.dumps(payload, indent=2) + "\n")
        updated += 1

    latest = index.get("latest")
    if latest:
        (DATA / "latest.json").write_text((DATA / f"{latest}.json").read_text())

    print(f"Backfilled {updated} daily files.")
    if skipped:
        print(f"Skipped (no file): {', '.join(skipped)}")


if __name__ == "__main__":
    main()
