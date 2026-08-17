"""Daily import/export history for the whole-house grid meter (Res_Meter).

The `measurement-point/time-series` endpoint serves METER.APConsumed (import)
and METER.APProduction (export) as *daily* cumulative registers that reset at
local midnight. We fetch one day at a time (5-minute granularity, matching the
portal) and take each day's end-of-day cumulative as that day's total.

One request per day is deliberate: the endpoint is happiest with short ranges,
and the daily total is all we need for statistics. Callers keep the window
modest (a week by default) since each day is a separate round-trip.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from .client import call

_LOGGER = logging.getLogger(__name__)

MP_TIMESERIES = "/hossain-bff/monitor/v1.0/measurement-point/time-series"
_POINTS = "METER.APConsumed,METER.APProduction"  # daily cumulative (import, export)


def _day_total(rows: list[dict], key: str) -> float | None:
    """End-of-day cumulative for a daily register = max over the day."""
    vals = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
    return float(max(vals)) if vals else None


def fetch_meter_daily(
    session: dict, start: date, end: date
) -> dict[str, dict[str, float]]:
    """Return {'import': {YYYY-MM-DD: kwh}, 'export': {...}} for [start, end].

    Empty dicts if the site has no meter. Tolerates per-day failures.
    """
    out: dict[str, dict[str, float]] = {"import": {}, "export": {}}
    meter_id = session.get("meter_id")
    if not meter_id:
        return out

    d = start
    while d <= end:
        body = {
            "mdmTypes": "Res_Meter",
            "mdmIds": meter_id,
            "startTime": f"{d.isoformat()} 00:00:00",
            "endTime": f"{d.isoformat()} 23:59:59",
            "interval": "5m",
            "measurementPoints": _POINTS,
            "autoInterpolate": True,
        }
        try:
            j = call(session, MP_TIMESERIES, body, timeout=60)
            rows = j.get("data") or []
            imp = _day_total(rows, "METER.APConsumed")
            exp = _day_total(rows, "METER.APProduction")
            if imp is not None:
                out["import"][d.isoformat()] = imp
            if exp is not None:
                out["export"][d.isoformat()] = exp
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("Meter daily fetch for %s failed: %s", d, e)
        d += timedelta(days=1)

    _LOGGER.debug(
        "fetch_meter_daily %s..%s -> import=%d export=%d days",
        start, end, len(out["import"]), len(out["export"]),
    )
    return out
