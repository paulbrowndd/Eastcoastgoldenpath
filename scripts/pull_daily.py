#!/usr/bin/env python3
"""Assemble dashboard JSON from Sigma query results (agent-run daily pull).

When the user says "pull today's data", refresh ALL of the following:

Daily tab (latest Sigma date, usually yesterday):
  - Hub + spoke OTD and routes  → element itgnfDcpNu (Parcel Golden Path)
  - SLA stack rank (top 25)       → element Rm9zSyNl07
  - Pallet inventory              → element 4fEOWBbyUc (Pallet Level Data / Daily # of Pallets - Outbound)
                                    ATL-15, EWR-2, MCI-1 only; starting counts carry forward

OKR tab (weekly metrics — refreshed every pull because in-week numbers change daily):
  - Hub sortation weekly OKR      → element itgnfDcpNu, STEP_TYPE_ORDER = '2.1 Hub Sortation'
  - Pieces per pallet weekly OKR  → element Jg7aT1Ix9W (Truck Utilization / Utilization Table)

Publish: data/{date}.json, data/latest.json, data/index.json, data/okr.json
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

HUBS = ["ATL-15", "DMV-1", "ELZ-1", "EWR-2", "GCO-1", "NYC-1", "SLC-8"]
PALLET_INVENTORY_HUBS = ["ATL-15", "EWR-2", "MCI-1"]
PALLET_HISTORY_WINDOW = 30
SPOKES = [
    "ALX-1", "ATL-11", "ATL-12", "BKN-9", "BLT-3", "BNX-1", "BOS-5", "BOS-6",
    "CIN-5", "CLE-7", "CLT-3", "CNJ-2", "COL-5", "DCA-5", "DET-13", "HBG-2",
    "HFD-3", "HGR-1", "HUD-1", "JAX-2", "LIN-1", "NAS-2", "NNJ-5", "NNJ-6",
    "ORL-3", "PHL-7", "PHL-8", "PIT-2", "QNS-2", "RAL-3", "RIC-2", "TPA-4",
    "VAB-4", "WMA-1",
]

OKR_WEEK_LOOKBACK = "2026-06-01"
DETAIL_RE = re.compile(r"^(.+?): (.+?) > (.+)$")

# Sigma workbook IDs (for agent MCP queries)
GOLDEN_PATH_WB = "69e5a397-7e08-464f-9921-9b3de12b7d4e"
TRUCK_UTIL_WB = "514c6528-d9e0-4b1a-87b6-b9b5063ab84a"
PALLET_LEVEL_WB = "696d7151-1173-49c5-a1b0-d99d20324037"
EL_DAILY = "itgnfDcpNu"
EL_STACK = "Rm9zSyNl07"
EL_UTIL = "Jg7aT1Ix9W"
EL_PALLET_OUTBOUND = "4fEOWBbyUc"


def now_est() -> str:
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M EST")


def round_pct(v):
    if v is None:
        return None
    return round(float(v), 1)


def pct_from_sigma(v):
    if v is None:
        return None
    f = float(v)
    return round(f * 100, 1) if f <= 1 else round(f, 1)


def parse_day(val) -> str:
    return str(val)[:10]


def parse_week(val) -> str:
    return parse_day(val)


def week_end(start: str) -> str:
    d = datetime.strptime(start, "%Y-%m-%d").date()
    return (d + timedelta(days=6)).isoformat()


def parse_stack_rank(rows):
    out = []
    for detail, miss_count in rows:
        if "TT" in detail and detail.split(":")[0].endswith("Arrival"):
            continue
        m = DETAIL_RE.match(detail)
        if not m:
            continue
        step, from_stop, next_stop = m.groups()
        out.append(
            {
                "step": step,
                "facility": from_stop,
                "from_stop": from_stop,
                "next_stop": next_stop,
                "miss_count": int(miss_count),
            }
        )
    out.sort(key=lambda r: r["miss_count"], reverse=True)
    return out[:25]


def build_daily_payload(date, hub_facility, hub_routes, spoke_facility, stack_rows, pallet_inventory):
    facilities = {}

    for hub in HUBS:
        pct = hub_facility.get(hub)
        routes = [
            {"route": route, "otd_pct": round_pct(pct)}
            for route, pct in sorted(hub_routes.get(hub, {}).items())
        ]
        facilities[hub] = {
            "type": "hub",
            "otd_pct": round_pct(pct),
            "routes": routes,
        }

    for spoke in SPOKES:
        pct = spoke_facility.get(spoke)
        if pct in (None, 0):
            facilities[spoke] = {"type": "spoke", "otd_pct": None}
        else:
            facilities[spoke] = {"type": "spoke", "otd_pct": round_pct(pct)}

    return {
        "date": date,
        "pulled_at": now_est(),
        "facilities": facilities,
        "sla_stack_rank": parse_stack_rank(stack_rows),
        "pallet_inventory": pallet_inventory,
    }


def build_hub_sortation(rows):
    """Rows: [facility, week_start, otd_pct, barcodes] from itgnfDcpNu weekly aggregation."""
    hub_weeks = {}
    for facility, week_start, otd_pct, barcodes in rows:
        if facility not in HUBS:
            continue
        wk = parse_week(week_start)
        if wk not in hub_weeks:
            hub_weeks[wk] = {
                "week_start": wk,
                "week_end": week_end(wk),
                "hubs": {h: {"otd_pct": None, "barcodes": 0} for h in HUBS},
            }
        bc = int(barcodes or 0)
        if bc > 0 and otd_pct is not None:
            hub_weeks[wk]["hubs"][facility] = {
                "otd_pct": pct_from_sigma(otd_pct),
                "barcodes": bc,
            }

    weeks_sorted = sorted(hub_weeks.keys(), reverse=True)
    current_week = weeks_sorted[0] if weeks_sorted else None

    for wk, hw in hub_weeks.items():
        hw["in_progress"] = wk == current_week
        total_weighted = 0
        total_bc = 0
        reporting = 0
        for h in HUBS:
            hd = hw["hubs"][h]
            if hd["otd_pct"] is not None and hd["barcodes"] > 0:
                reporting += 1
                total_weighted += hd["otd_pct"] * hd["barcodes"]
                total_bc += hd["barcodes"]
        hw["network_otd_pct"] = round(total_weighted / total_bc, 1) if total_bc else None
        hw["reporting_hubs"] = reporting

    return hub_weeks, weeks_sorted, current_week


def build_pieces_per_pallet(hub_agg_rows, lane_rows):
    """Hub agg: [week_start, origin, lane_segment, ppp, parcels, pallets, lane_count]
    Lanes: [week_start, origin, lane_segment, lane, ppp, parcels, pallets]
    """
    ppp = {}
    for row in hub_agg_rows:
        ws, origin, seg, ppp_val, parcels, pallets, lane_count = row
        wk = parse_week(ws)
        if wk not in ppp:
            ppp[wk] = {
                "week_start": wk,
                "week_end": week_end(wk),
                "in_progress": False,
                "hubs": {h: {"hub_hub": None, "hub_spoke": None} for h in HUBS},
            }
        if origin not in HUBS:
            continue
        ppp[wk]["hubs"][origin][seg] = {
            "ppp": round(float(ppp_val), 1),
            "parcels": int(parcels),
            "pallets": int(pallets),
            "lane_count": int(lane_count),
            "lanes": [],
        }

    for row in lane_rows:
        ws, origin, seg, lane, ppp_val, parcels, pallets = row
        wk = parse_week(ws)
        if wk not in ppp or origin not in HUBS:
            continue
        seg_data = ppp[wk]["hubs"][origin].get(seg)
        if not seg_data:
            continue
        dest = lane.split(" - ", 1)[1] if " - " in lane else lane
        seg_data["lanes"].append(
            {
                "lane": lane,
                "destination": dest,
                "ppp": round(float(ppp_val), 1),
                "parcels": int(parcels),
                "pallets": int(pallets),
            }
        )

    weeks_sorted = sorted(ppp.keys(), reverse=True)
    current_week = weeks_sorted[0] if weeks_sorted else None

    for wk in ppp:
        ppp[wk]["in_progress"] = wk == current_week
        for h in HUBS:
            for seg in ("hub_hub", "hub_spoke"):
                seg_data = ppp[wk]["hubs"][h][seg]
                if seg_data and seg_data["lanes"]:
                    seg_data["lanes"].sort(key=lambda x: x["ppp"], reverse=True)

    return ppp, weeks_sorted, current_week


def build_okr_payload(hub_sort_rows, ppp_hub_agg_rows, ppp_lane_rows):
    hub_sortation, hs_weeks, hs_current = build_hub_sortation(hub_sort_rows)
    pieces_per_pallet, ppp_weeks, ppp_current = build_pieces_per_pallet(
        ppp_hub_agg_rows, ppp_lane_rows
    )

    weeks = sorted(set(hs_weeks) | set(ppp_weeks), reverse=True)
    current_week = weeks[0] if weeks else (hs_current or ppp_current)

    for wk in pieces_per_pallet:
        if wk in hub_sortation:
            pieces_per_pallet[wk]["in_progress"] = hub_sortation[wk]["in_progress"]

    return {
        "pulled_at": now_est(),
        "source": "Sigma · Parcel Golden Path + Truck Utilization",
        "current_week": current_week,
        "weeks": weeks,
        "hub_sortation": hub_sortation,
        "pieces_per_pallet": pieces_per_pallet,
    }


def write_daily(date, payload):
    path = DATA / f"{date}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n")

    index_path = DATA / "index.json"
    index = json.loads(index_path.read_text())
    dates = index.get("dates", [])
    if date not in dates:
        dates.append(date)
        dates.sort()
    index["dates"] = dates
    index["latest"] = date
    index_path.write_text(json.dumps(index, indent=2) + "\n")

    latest_path = DATA / "latest.json"
    latest_path.write_text(json.dumps(payload, indent=2) + "\n")


def write_okr(payload):
    path = DATA / "okr.json"
    path.write_text(json.dumps(payload, indent=2) + "\n")


def previous_inventory(date):
    index = json.loads((DATA / "index.json").read_text())
    dates = sorted(index.get("dates", []))
    prev = [d for d in dates if d < date]
    if not prev:
        return {}
    prev_path = DATA / f"{prev[-1]}.json"
    if not prev_path.exists():
        return {}
    return json.loads(prev_path.read_text()).get("pallet_inventory") or {}


def load_outbound_history(before_date, sigma_history_rows=None):
    """Return {hub: [(date, outbound), ...]} from saved daily files."""
    history = {hub: [] for hub in PALLET_INVENTORY_HUBS}
    for path in sorted(DATA.glob("2026-*.json")):
        day = path.stem
        if day >= before_date:
            continue
        inv = json.loads(path.read_text()).get("pallet_inventory") or {}
        for hub in PALLET_INVENTORY_HUBS:
            hub_data = inv.get(hub)
            if hub_data and hub_data.get("outbound_today") is not None:
                history[hub].append((day, int(hub_data["outbound_today"])))

    if sigma_history_rows:
        existing = {hub: {day for day, _ in rows} for hub, rows in history.items()}
        for hub, day, outbound in sigma_history_rows:
            day = parse_day(day)
            if hub not in PALLET_INVENTORY_HUBS or day >= before_date:
                continue
            if day not in existing.get(hub, set()):
                history[hub].append((day, int(outbound)))
        for hub in PALLET_INVENTORY_HUBS:
            history[hub].sort()

    return history


def build_pallet_inventory(date, outbound_rows, prev_inventory=None, history=None):
    """Build pallet_inventory for tracked hubs.

    outbound_rows: [(hub, day, outbound_pallets), ...] for the pull date.
    Starting pallet/gaylord baselines carry forward from the prior day unless missing.
    Remaining counts use starting - outbound_today (same model as the dashboard).
    """
    outbound_by_hub = {}
    for hub, day, outbound in outbound_rows:
        if parse_day(day) == date and hub in PALLET_INVENTORY_HUBS:
            outbound_by_hub[hub] = int(outbound)

    prev = prev_inventory or {}
    history = history or {hub: [] for hub in PALLET_INVENTORY_HUBS}
    inventory = {}

    for hub in PALLET_INVENTORY_HUBS:
        outbound = outbound_by_hub.get(hub, 0)
        prev_hub = prev.get(hub, {})
        starting_pallets = prev_hub.get("starting_pallets")
        starting_gaylords = prev_hub.get("starting_gaylords")

        if starting_pallets is None:
            starting_pallets = max(outbound, prev_hub.get("remaining_pallets") or 0)
        if starting_gaylords is None:
            starting_gaylords = max(outbound, prev_hub.get("remaining_gaylords") or 0)

        remaining_pallets = max(0, int(starting_pallets) - outbound)
        remaining_gaylords = max(0, int(starting_gaylords) - outbound)

        past_outbound = [value for _, value in history.get(hub, [])]
        window = (past_outbound + [outbound])[-PALLET_HISTORY_WINDOW:]
        avg_daily_outbound = round(sum(window) / len(window)) if window else outbound

        inventory[hub] = {
            "starting_pallets": int(starting_pallets),
            "starting_gaylords": int(starting_gaylords),
            "outbound_today": outbound,
            "remaining_pallets": remaining_pallets,
            "remaining_gaylords": remaining_gaylords,
            "avg_daily_outbound": avg_daily_outbound,
            "days_remaining_pallets": round(remaining_pallets / avg_daily_outbound, 1)
            if avg_daily_outbound
            else None,
            "days_remaining_gaylords": round(remaining_gaylords / avg_daily_outbound, 1)
            if avg_daily_outbound
            else None,
            "history_days": len(window),
        }

    return inventory


# --- Sigma SQL templates for the agent (MCP query tool) ---

SQL_HUB_SORT_OKR = f"""
SELECT "FACILITY" AS facility, "WEEK" AS week_start,
  SUM("MET_SLA")::float / NULLIF(SUM("BARCODES"), 0) AS otd_pct,
  SUM("BARCODES") AS barcodes
FROM "workbook"."{EL_DAILY}"
WHERE "STEP_TYPE_ORDER" = '2.1 Hub Sortation'
  AND "FACILITY" IN ({','.join(repr(h) for h in HUBS)})
  AND "WEEK" >= '{OKR_WEEK_LOOKBACK}'
GROUP BY 1, 2
ORDER BY 2 DESC, 1
""".strip()

SQL_PPP_HUB_AGG = f"""
WITH base AS (
  SELECT
    DATE_TRUNC('week', "DEPARTURE_DATE_LOCAL" - INTERVAL '1 day') + INTERVAL '1 day' AS week_start,
    "ORIGIN" AS origin,
    "LANE_SEGMENT" AS lane_segment,
    "LANE" AS lane,
    "TOTAL_PARCELS" AS parcels,
    "TOTAL_PALLETS" AS pallets
  FROM "workbook"."{EL_UTIL}"
  WHERE "ORIGIN" IN ({','.join(repr(h) for h in HUBS)})
    AND "TOTAL_PALLETS" > 0
    AND DATE_TRUNC('week', "DEPARTURE_DATE_LOCAL" - INTERVAL '1 day') + INTERVAL '1 day' >= '{OKR_WEEK_LOOKBACK}'
)
SELECT week_start, origin, lane_segment,
  SUM(parcels)::float / SUM(pallets) AS ppp,
  SUM(parcels) AS parcels,
  SUM(pallets) AS pallets,
  COUNT(DISTINCT lane) AS lane_count
FROM base
GROUP BY 1, 2, 3
ORDER BY 1 DESC, 2, 3
""".strip()

SQL_PALLET_OUTBOUND = f"""
SELECT "ptE8dToYt3" AS origin, "58BsECGAoi" AS pallet_date,
  SUM("Z3Uuroy1UV") AS outbound_pallets
FROM "workbook"."{EL_PALLET_OUTBOUND}"
WHERE "ptE8dToYt3" IN ({','.join(repr(h) for h in PALLET_INVENTORY_HUBS)})
  AND "58BsECGAoi" = '{{date}}'
GROUP BY 1, 2
ORDER BY 1
""".strip()

SQL_PALLET_OUTBOUND_HISTORY = f"""
SELECT "ptE8dToYt3" AS origin, "58BsECGAoi" AS pallet_date,
  SUM("Z3Uuroy1UV") AS outbound_pallets
FROM "workbook"."{EL_PALLET_OUTBOUND}"
WHERE "ptE8dToYt3" IN ({','.join(repr(h) for h in PALLET_INVENTORY_HUBS)})
  AND "58BsECGAoi" BETWEEN '{{start_date}}' AND '{{date}}'
GROUP BY 1, 2
ORDER BY 2 DESC, 1
""".strip()

SQL_PPP_LANES = f"""
WITH base AS (
  SELECT
    DATE_TRUNC('week', "DEPARTURE_DATE_LOCAL" - INTERVAL '1 day') + INTERVAL '1 day' AS week_start,
    "ORIGIN" AS origin,
    "LANE_SEGMENT" AS lane_segment,
    "LANE" AS lane,
    "TOTAL_PARCELS" AS parcels,
    "TOTAL_PALLETS" AS pallets
  FROM "workbook"."{EL_UTIL}"
  WHERE "ORIGIN" IN ({','.join(repr(h) for h in HUBS)})
    AND "TOTAL_PALLETS" > 0
    AND DATE_TRUNC('week', "DEPARTURE_DATE_LOCAL" - INTERVAL '1 day') + INTERVAL '1 day' >= '{OKR_WEEK_LOOKBACK}'
)
SELECT week_start, origin, lane_segment, lane,
  SUM(parcels)::float / SUM(pallets) AS ppp,
  SUM(parcels) AS parcels,
  SUM(pallets) AS pallets
FROM base
GROUP BY 1, 2, 3, 4
ORDER BY 1 DESC, 2, 3, 5 DESC
""".strip()


if __name__ == "__main__":
    print("Daily pull script — run by agent after Sigma MCP queries.")
    print("Includes: daily OTD/stack rank, pallet inventory, weekly OKR.")
    print(
        "SQL templates: SQL_HUB_SORT_OKR, SQL_PPP_HUB_AGG, SQL_PPP_LANES, "
        "SQL_PALLET_OUTBOUND, SQL_PALLET_OUTBOUND_HISTORY"
    )
