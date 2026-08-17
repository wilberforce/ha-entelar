"""Hourly energy aggregates derived from dt-service 5m power samples.

Univers exposes 5-minute granularity for instantaneous power fields (PV out,
battery charge/discharge, grid meter), retained for ~30 days. The Energy
dashboard's "Day" view wants hourly bars, and HA's external-statistics API
stores at hourly resolution -- so we fetch 5m here and integrate up to
hourly kWh, returning the same energy-field names that `daily_history.py`
produces.

Why not ask Univers for hourly directly? The dt-service `interval` param
only accepts "5m" | "D" | "M" | "Y" -- no "H". Aggregating 5m to hourly
ourselves is the cleanest path and lets us own the rounding/sign conventions.

Sign conventions observed via probe (probe_5m.py) on the live system,
NOT what one might guess from the field names:
  - PUB_SITE.PVOutputPower   : kW, >= 0 when PV producing (occasional tiny
                                negative noise pre-dawn; treat as 0).
  - PUB_SITE.METERActivePW   : kW, signed. NEGATIVE = import from grid,
                                POSITIVE = export to grid.
  - ChargePower              : kW, POSITIVE when battery charging, 0 otherwise.
  - DischargePower           : kW, NEGATIVE when battery discharging, 0 otherwise.

The hourly totals produced here match Univers's daily aggregate fields almost
exactly (within rounding) when integrated over the same period.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta

from .client import call

_LOGGER = logging.getLogger(__name__)

DT_QUERY = "/dt-service/datasource/v2/data/query"
DT_TIMESERIES_DS = "07b41597-d083-44d3-a3b5-947d1fb8f4b3"

# 5m power fields we sample. The energy field names produced by the
# integration step match daily_history's keys exactly so the two timeseries
# can be glued together by statistics_manager.
FIVE_MIN_FIELDS = [
    "localtime",
    "PUB_SITE.PVOutputPower",     # PV (kW)  -> ActiveProduction
    "PUB_SITE.METERActivePW",     # Grid (kW, signed) -> OffGridEnergy / OnGridEnergy
    "ChargePower",                # Battery charge (kW) -> ChargeProduction
    "DischargePower",             # Battery discharge (kW) -> DischargeProduction
]

# Plan 02 noted that 5m queries beyond ~7 days time out. Chunking keeps
# individual requests fast and lets us partial-fail one chunk without losing
# the rest.
MAX_DAYS_PER_REQUEST = 6

# Each 5m bucket contributes kw * (5/60) kWh to its hour.
KWH_PER_5MIN = 5.0 / 60.0


def _f(x):
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _fetch_5m_chunk(session: dict, start_dt: datetime, end_dt: datetime) -> list[dict]:
    """Fetch raw 5m rows from dt-service. Returns list of dict rows."""
    body = [{
        "dcKey": f"entelar-5m-{start_dt:%Y%m%d}-{end_dt:%Y%m%d}",
        "datasourceId": DT_TIMESERIES_DS,
        "internalKey": "timeseries",
        "category": "Res_Solar_Site",
        "categoryType": None,
        "pagination": {"pageSize": 5000, "pageNum": 1, "enablePagination": False},
        "fields": FIVE_MIN_FIELDS,
        "params": {
            "startTime": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "endTime":   end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "interval":  "5m",
            "aggregation": "first",
            "autoData":    "true",
            "rawAttribute": "true",
            "mdmId":       session["siteId_short"],
            "preserveIndex": "true",
            "autoInterpolate": "true",
            "newType":     "newGeneral",
        },
        "filters": [],
        "sort": [{"field": "localtime", "sorted": "asc"}],
        "aggregation": True,
    }]
    j = call(
        session, DT_QUERY, body, timeout=90,
        extra_headers={
            "X-NS":    "encompass",
            "X-APPID": session.get("appId", ""),
            "X-CK":    "en-US",
        },
        params={"_p": "high"},
    )
    inner = next(iter(j.get("data", {}).values()), {}) or {}
    return inner.get("data", []) or []


def _integrate_to_hourly(rows: list[dict]) -> dict[str, dict[str, float]]:
    """Aggregate 5m power rows into hourly kWh, keyed by local 'YYYY-MM-DD HH:00'.

    Returns {field: {hour_key: kwh}}.

    For every hour that has ANY 5m sample, emit an entry for ALL five fields
    -- using 0.0 if that field had no contribution. This keeps the resulting
    statistic_id series gap-free at hour boundaries; HA needs a continuous
    cumulative timeseries to compute hourly bars correctly.
    """
    # First pass: discover which hours we saw at all, and accumulate per-field.
    hours_seen: set[str] = set()
    by_hour: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for r in rows:
        lt = r.get("localtime")
        if not lt or len(lt) < 13:
            continue
        # "2026-06-16 14:35:00" -> "2026-06-16 14:00"
        hour_key = lt[:13] + ":00"
        hours_seen.add(hour_key)

        pv = _f(r.get("PUB_SITE.PVOutputPower"))
        meter = _f(r.get("PUB_SITE.METERActivePW"))
        charge = _f(r.get("ChargePower"))
        discharge = _f(r.get("DischargePower"))

        # PV: tiny negative noise pre-dawn observed; clamp to 0.
        if pv is not None and pv > 0:
            by_hour[hour_key]["ActiveProduction"] += pv * KWH_PER_5MIN
        # Grid: NEGATIVE = import (OffGridEnergy), POSITIVE = export (OnGridEnergy).
        if meter is not None:
            if meter < 0:
                by_hour[hour_key]["OffGridEnergy"] += (-meter) * KWH_PER_5MIN
            elif meter > 0:
                by_hour[hour_key]["OnGridEnergy"] += meter * KWH_PER_5MIN
        # Battery charge: POSITIVE = charging.
        if charge is not None and charge > 0:
            by_hour[hour_key]["ChargeProduction"] += charge * KWH_PER_5MIN
        # Battery discharge: NEGATIVE = discharging (per probe_5m.py observation).
        if discharge is not None and discharge < 0:
            by_hour[hour_key]["DischargeProduction"] += (-discharge) * KWH_PER_5MIN

    # Second pass: emit an entry for every (hour, field) pair so the resulting
    # cumulative series in statistics_manager has no gaps. Missing fields in
    # a given hour become 0.0.
    out: dict[str, dict[str, float]] = {
        "ActiveProduction":    {},
        "OffGridEnergy":       {},
        "OnGridEnergy":        {},
        "ChargeProduction":    {},
        "DischargeProduction": {},
    }
    for hour_key in hours_seen:
        fields = by_hour.get(hour_key, {})
        for field in out:
            out[field][hour_key] = round(fields.get(field, 0.0), 4)
    return out


def fetch_hourly_history(
    session: dict, start: date, end: date
) -> dict[str, dict[str, float]]:
    """Fetch 5m samples from `start` 00:00 to `end` 23:59 (local), -> hourly kWh.

    Returns {field: {'YYYY-MM-DD HH:00': kwh, ...}} for the five energy fields.
    Splits large windows into <=6-day chunks (5m queries time out beyond that).
    Tolerates per-chunk failures -- a bad chunk just shrinks the result.
    """
    all_rows: list[dict] = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=MAX_DAYS_PER_REQUEST - 1), end)
        sdt = datetime(chunk_start.year, chunk_start.month, chunk_start.day, 0, 0, 0)
        edt = datetime(chunk_end.year, chunk_end.month, chunk_end.day, 23, 59, 59)
        try:
            rows = _fetch_5m_chunk(session, sdt, edt)
            all_rows.extend(rows)
            _LOGGER.debug(
                "5m chunk %s..%s -> %d rows", chunk_start, chunk_end, len(rows),
            )
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning(
                "5m chunk %s..%s failed (%s); continuing",
                chunk_start, chunk_end, e,
            )
        chunk_start = chunk_end + timedelta(days=1)

    integrated = _integrate_to_hourly(all_rows)
    _LOGGER.info(
        "Hourly history %s..%s: %d raw 5m rows -> hourly counts %s",
        start, end, len(all_rows),
        {k: len(v) for k, v in integrated.items()},
    )
    return integrated
