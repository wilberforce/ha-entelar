"""Site snapshot for the Entelar custom integration.

Same logic as snapshot.py in the standalone Solar-Project repo, but rewired
for the integration: no SQLite, no argparse -- just a function that returns
a dict of current values.
"""
from __future__ import annotations

import time
from typing import Any

from .client import call

API_DETAIL = "/hossain-bff/monitor/v1.0/asset/detail"
API_LIST = "/hossain-bff/monitor/v1.0/asset/list"

# Measurement points the Site Overview page asks for.
LIVE_POINTS = ",".join([
    "PUB_SITE.PVOutputPower",   # PV output (kW)
    "PUB_SITE.METERActivePW",   # Grid meter active power (kW); -ve = IMPORT, +ve = export
    "PUB_SITE.BSActivePW",      # Battery active power (kW); -ve = discharge, +ve = charge
    "PUB_SITE.Soc",             # Battery state of charge (%)
    "ConsPower",                # Load / consumption (kW)
    "SITE.GenActivePW",         # Site generation active power (kW)
    "PUB_SITE.EVChargingPW",    # EV charging power (kW) -- may be absent
])

# Metric -> dict-key. Each metric is one of <name>:<TD|MTD|YTD|BOL>
TRACKED_METRICS = {
    "pv_today_kwh":                   "ActiveProduction:TD",
    "pv_month_kwh":                   "ActiveProduction:MTD",
    "pv_year_kwh":                    "ActiveProduction:YTD",
    "pv_lifetime_kwh":                "ActiveProduction:BOL",
    "revenue_today":                  "Revenue:TD",
    "ongrid_income_today":            "OnGridIncome:TD",
    "ongrid_income_lifetime":         "OnGridIncome:BOL",
    "self_cons_production_today":     "SelfConsProduction:TD",
    "grid_export_month_kwh":          "OnGridEnergy:MTD",
    "grid_export_lifetime_kwh":       "OnGridEnergy:BOL",
    "grid_import_month_kwh":          "OffGridEnergy:MTD",
    "grid_import_lifetime_kwh":       "OffGridEnergy:BOL",
    "battery_charged_month_kwh":      "ChargeProduction:MTD",
    "battery_charged_lifetime_kwh":   "ChargeProduction:BOL",
    "battery_discharged_month_kwh":   "DischargeProduction:MTD",
    "battery_discharged_lifetime_kwh":"DischargeProduction:BOL",
}

TRACKED_POINTS = {
    "pv_active_kw":       "PUB_SITE.PVOutputPower",
    "grid_active_kw":     "PUB_SITE.METERActivePW",
    "battery_active_kw":  "PUB_SITE.BSActivePW",
    "battery_soc_pct":    "PUB_SITE.Soc",
    "load_kw":            "ConsPower",
    "site_gen_active_kw": "SITE.GenActivePW",
    "ev_charging_kw":     "PUB_SITE.EVChargingPW",
}


def discover_site(session: dict) -> dict:
    """Look up the user's first solar site via asset/list.

    Populates `siteId_short`, `site_name`, and `capacity_kw` into the session
    dict so subsequent snapshot calls can address the site.

    The Univers API has been observed returning `data` in any of these shapes:
      {"data": [ {...}, {...} ]}              -- a list directly
      {"data": {"items": [ {...} ]}}          -- nested under "items"
      {"data": {"<site_id>": {...}, ...}}     -- dict keyed by mdmId
    We handle all three.
    """
    import logging
    log = logging.getLogger(__name__)

    j = call(session, API_LIST, {
        "pageSize": 50, "pageNo": 1,
        "mdmTypes": "Res_Solar_Site",
    })
    data = j.get("data") if isinstance(j, dict) else None
    if isinstance(data, list):
        sites = data
    elif isinstance(data, dict):
        # Try "items" key first; fall back to values() (id-keyed dict)
        items = data.get("items")
        if isinstance(items, list):
            sites = items
        else:
            sites = [v for v in data.values() if isinstance(v, dict)]
    else:
        sites = []

    if not sites:
        log.warning("Entelar asset/list returned no Res_Solar_Site rows; "
                    "raw response keys: %s, top-level type: %s",
                    list(j.keys()) if isinstance(j, dict) else "(not a dict)",
                    type(j).__name__)
        raise RuntimeError(
            "No solar sites returned for this account. "
            "Raw response shape: " + repr(type(j)) + " / data type: " + repr(type(data))
        )
    first = sites[0]
    site_id_short = first.get("mdmId") or first.get("id") or first.get("siteId")
    # Univers also exposes a longer UUID for the site (varies per record;
    # try several field names). dt-service requires this as X-APPID -- under
    # this tenant's setup appId == siteId_uuid.
    site_id_uuid = (
        first.get("uuid")
        or first.get("siteUuid")
        or first.get("id")
        or first.get("mdmUuid")
        or site_id_short  # last resort -- often still works for hossain-bff calls
    )
    # Enrich from asset/detail. On some tenants asset/list is sparse (no name,
    # capacity, or commissioning date); the detail view carries all three.
    operative_date = None
    site_name = first.get("name") or first.get("siteName")
    capacity = first.get("capacity")
    site_timezone = None
    try:
        detail = call(session, API_DETAIL, {
            "mdmIds": site_id_short,
            "view": "WebSiteDetailMonitorOverview",
        })
        dattrs = ((detail.get("data") or {}).get(site_id_short) or {}).get(
            "attributes", {}
        )
        operative_date = dattrs.get("operativeDate")  # 'YYYY-MM-DD' commissioning
        site_name = dattrs.get("name") or site_name
        capacity = dattrs.get("capacity") or capacity
        site_timezone = dattrs.get("timezone")
    except Exception as e:  # noqa: BLE001
        log.debug("asset/detail enrichment failed (%s); using asset/list only", e)

    return {
        **session,
        "siteId_short": site_id_short,
        "siteId_uuid":  site_id_uuid,
        "appId":        site_id_uuid,   # dt-service routing header
        "site_name":    site_name,
        "capacity_kw":  capacity,
        "operative_date": operative_date,   # commissioning date, if the portal has it
        "site_timezone":  site_timezone,
        "_site_raw":    first,  # kept for diagnostics; not used downstream
    }


def snapshot_site(session: dict) -> dict[str, Any]:
    """Hit both endpoints, flatten into one dict of live + cumulative values.

    Session must already have `siteId_short` populated -- call discover_site()
    once after login if it isn't.
    """
    site_id = session["siteId_short"]

    overview = call(session, API_DETAIL, {
        "mdmIds": site_id,
        "view": "WebSiteDetailMonitorOverview",
    })
    site_overview = overview["data"][site_id]

    live = call(session, API_DETAIL, {
        "mdmIds": site_id,
        "attributes": "gmtAmount,batteryStorageAmount,strInvAmount,powerDirection",
        "measurementPoints": LIVE_POINTS,
    })
    site_live = live["data"][site_id]

    metrics = site_overview.get("metrics", {})
    points = site_live.get("measurementPoints", {})
    attrs = site_overview.get("attributes", {})

    def m(key: str):
        v = metrics.get(key)
        return v.get("value") if v else None

    def p(key: str):
        v = points.get(key)
        return v.get("value") if v else None

    return {
        "ts_ms": int(time.time() * 1000),
        "site_id":   site_id,
        "site_name": attrs.get("name") or session.get("site_name"),
        "currency":  attrs.get("currency"),
        "capacity_kw": attrs.get("capacity") or session.get("capacity_kw"),
        **{k: p(v) for k, v in TRACKED_POINTS.items()},
        **{k: m(v) for k, v in TRACKED_METRICS.items()},
    }
