"""Daily aggregate fetch via Univers's dt-service endpoint.

Equivalent to the standalone `backfill.py` but trimmed for live integration
use: one function that returns `{metric: {date: kwh}}` for a date range.

The dt-service has stricter routing requirements than hossain-bff: it needs
`X-NS: encompass`, `X-APPID: <site uuid>`, and `?_p=high`. Without these the
server returns 99400 'can't find datasource'.
"""
from __future__ import annotations

import logging
from datetime import date

from .client import call

_LOGGER = logging.getLogger(__name__)

DT_QUERY = "/dt-service/datasource/v2/data/query"

# Datasource UUID observed in the portal bundle. May change with portal upgrades.
DT_TIMESERIES_DS = "07b41597-d083-44d3-a3b5-947d1fb8f4b3"

# The energy metrics we care about (daily aggregates, kWh per day).
DAILY_FIELDS = [
    "localtime",
    "ActiveProduction",      # PV
    "OnGridEnergy",          # export
    "OffGridEnergy",         # import
    "ChargeProduction",      # battery in
    "DischargeProduction",   # battery out
]


def fetch_daily_history(
    session: dict, start: date, end: date
) -> dict[str, dict[str, float]]:
    """Return {field: {YYYY-MM-DD: kwh, ...}} for the date range.

    Empty inner dicts if the metric isn't returned for that range.
    """
    body = [{
        "dcKey": f"entelar-integration-{start}-{end}",
        "datasourceId": DT_TIMESERIES_DS,
        "internalKey": "timeseries",
        "category": "Res_Solar_Site",
        "categoryType": None,
        "pagination": {"pageSize": 1000, "pageNum": 1, "enablePagination": False},
        "fields": DAILY_FIELDS,
        "params": {
            "startTime": f"{start.isoformat()} 00:00:00",
            "endTime":   f"{end.isoformat()} 23:59:59",
            "interval":  "D",
            "aggregation": "sum",
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

    # Response shape: {"data": {"<datasource_uuid>": {"data": [ {row}, ... ]}}}
    inner = next(iter(j.get("data", {}).values()), {}) or {}
    records = inner.get("data", []) or []

    out: dict[str, dict[str, float]] = {f: {} for f in DAILY_FIELDS[1:]}
    for r in records:
        lt = r.get("localtime") or ""
        date_str = lt[:10]
        if not date_str:
            continue
        for field in DAILY_FIELDS[1:]:
            v = r.get(field)
            if v is None or not isinstance(v, (int, float)):
                continue
            out[field][date_str] = float(v)

    _LOGGER.debug(
        "fetch_daily_history %s..%s -> %s rows; metric counts: %s",
        start, end, len(records),
        {k: len(v) for k, v in out.items()},
    )
    return out
