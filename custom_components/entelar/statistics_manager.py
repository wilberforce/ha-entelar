"""External long-term statistics, mixed daily + hourly granularity.

Uses HA's external statistics API (statistic_ids prefixed with `entelar:`)
so the imported historical data is fully separate from any entity's live
state-derived statistics. Avoids the seed/live contamination that bit us
when we tried entity-bound statistics.

For each metric, the cumulative timeseries we push has two granularities
glued together with a continuous running sum:

  * Daily entries (at local midnight) for the period from solar
    commissioning up to the day BEFORE the hourly window begins.
  * Hourly entries (on the hour) for the last ~30 days, where Univers
    still serves 5m samples we can integrate.

The Energy dashboard reads only the statistic_id, so the granularity-mix
is transparent: the "Year" view bars roll up from daily entries, the
"Day" view bars roll up from hourly entries.

`async_add_external_statistics` is idempotent at matching `start`
timestamps -- re-importing the full timeseries every poll is cheap and
self-healing.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

# HA is migrating statistics metadata from `has_mean` to `mean_type`. Prefer the
# new key when this HA version supports it, and fall back to `has_mean` on older
# versions. Our metrics are pure cumulative sums, so there is no mean component.
try:
    from homeassistant.components.recorder.models import StatisticMeanType

    _MEAN_META: dict = {"mean_type": StatisticMeanType.NONE}
except ImportError:  # pragma: no cover -- older HA without mean_type
    _MEAN_META = {"has_mean": False}

# The source prefix that must match the first segment of every statistic_id.
SOURCE = "entelar"

# (daily/hourly field, external statistic_id, friendly name)
EXTERNAL_STATS = [
    ("ActiveProduction",     f"{SOURCE}:pv_lifetime",                 "Entelar PV Production Lifetime"),
    ("OffGridEnergy",        f"{SOURCE}:grid_import_lifetime",        "Entelar Grid Import Lifetime"),
    ("OnGridEnergy",         f"{SOURCE}:grid_export_lifetime",        "Entelar Grid Export Lifetime"),
    ("ChargeProduction",     f"{SOURCE}:battery_charged_lifetime",    "Entelar Battery Charged Lifetime"),
    ("DischargeProduction",  f"{SOURCE}:battery_discharged_lifetime", "Entelar Battery Discharged Lifetime"),
]

# Convenient list of just the statistic_ids -- used by the dashboard wiring
# and by any future clear-and-resync helper.
EXTERNAL_STATISTIC_IDS = [stat_id for _, stat_id, _ in EXTERNAL_STATS]

# Whole-house grid meter (Res_Meter) external statistics. (meter_daily field,
# statistic_id, name). Anchored to the meter's true lifetime registers.
METER_STATS = [
    ("import", f"{SOURCE}:meter_grid_import", "Entelar Meter Grid Import"),
    ("export", f"{SOURCE}:meter_grid_export", "Entelar Meter Grid Export"),
]
METER_STATISTIC_IDS = [stat_id for _, stat_id, _ in METER_STATS]

# Only anchor to the portal's lifetime (BOL) figure when it differs from our
# summed window by more than this, to avoid churn from rounding/interpolation.
_BOL_ANCHOR_EPSILON = 0.5  # kWh


def _midnight(date_str: str) -> datetime:
    """'2026-06-16' -> local midnight datetime (HA's configured timezone)."""
    d = date.fromisoformat(date_str)
    return datetime(d.year, d.month, d.day, 0, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE)


def _hour_start(hour_key: str) -> datetime:
    """'2026-06-16 14:00' -> local hour-start datetime (HA's timezone)."""
    return datetime.strptime(hour_key, "%Y-%m-%d %H:%M").replace(
        tzinfo=dt_util.DEFAULT_TIME_ZONE
    )


def _build_stats(
    daily: dict[str, float],
    hourly: dict[str, float] | None,
) -> list[dict]:
    """Build HA statistic rows with continuous running cumulative sum.

    Daily entries from the beginning of the daily series up to (but not
    including) the date the hourly series starts; hourly entries from that
    date forward. If no hourly series, falls back to pure-daily.
    """
    if not hourly:
        return _daily_only(daily)

    sorted_hours = sorted(hourly.keys())
    if not sorted_hours:
        return _daily_only(daily)

    cutover_date = _hour_start(sorted_hours[0]).date()

    out: list[dict] = []
    cumulative = 0.0

    # 1) Daily entries strictly before cutover_date
    for d_str in sorted(daily.keys()):
        d = date.fromisoformat(d_str)
        if d >= cutover_date:
            break
        kwh = daily[d_str]
        if kwh is None:
            continue
        cumulative += kwh
        out.append({
            "start": _midnight(d_str),
            "sum":   round(cumulative, 3),
            "state": round(cumulative, 3),
        })

    # 2) Hourly entries from cutover_date forward
    for hour_key in sorted_hours:
        kwh = hourly[hour_key]
        if kwh is None:
            continue
        cumulative += kwh
        out.append({
            "start": _hour_start(hour_key),
            "sum":   round(cumulative, 4),
            "state": round(cumulative, 4),
        })

    return out


def _daily_only(daily: dict[str, float]) -> list[dict]:
    out: list[dict] = []
    cumulative = 0.0
    for d_str in sorted(daily.keys()):
        kwh = daily[d_str]
        if kwh is None:
            continue
        cumulative += kwh
        out.append({
            "start": _midnight(d_str),
            "sum":   round(cumulative, 3),
            "state": round(cumulative, 3),
        })
    return out


def push_meter_statistics(
    hass: HomeAssistant,
    meter_daily: dict[str, dict[str, float]],
    lifetime_totals: dict[str, float] | None = None,
) -> int:
    """Push the whole-house grid meter's daily import/export as external stats.

    `meter_daily` is {'import': {date: kwh}, 'export': {date: kwh}} (daily
    totals). `lifetime_totals` maps 'import'/'export' to the meter's true
    lifetime register (kWh), used to anchor each series so the tail is correct.
    Idempotent per timestamp. Returns the number of metrics pushed.
    """
    lifetime_totals = lifetime_totals or {}
    count = 0
    for field, stat_id, name in METER_STATS:
        daily = meter_daily.get(field) or {}
        if not daily:
            continue
        stats = _daily_only(daily)
        if not stats:
            continue
        _anchor_to_lifetime(stats, stat_id, lifetime_totals.get(field))
        metadata = {
            "statistic_id":        stat_id,
            "source":              SOURCE,
            "name":                name,
            "unit_of_measurement": "kWh",
            "has_sum":             True,
            **_MEAN_META,
        }
        async_add_external_statistics(hass, metadata, stats)
        count += 1
        _LOGGER.debug(
            "Pushed %d meter entries for %s (final %.2f kWh)",
            len(stats), stat_id, stats[-1]["sum"],
        )
    return count


def _anchor_to_lifetime(stats: list[dict], stat_id: str, bol: float | None) -> None:
    """Shift `stats` in place so the final cumulative equals `bol` (BOL).

    No-op when bol is absent, when the gap is within the epsilon (rounding), or
    when our window sum already exceeds bol (which would mean over-counting --
    left alone and logged rather than silently subtracting).
    """
    if bol is None or not stats:
        return
    offset = bol - stats[-1]["sum"]
    if offset > _BOL_ANCHOR_EPSILON:
        for row in stats:
            row["sum"] = round(row["sum"] + offset, 3)
            row["state"] = row["sum"]
        _LOGGER.debug("Anchored %s to BOL (+%.3f kWh)", stat_id, offset)
    elif offset < -_BOL_ANCHOR_EPSILON:
        _LOGGER.warning(
            "Not anchoring %s: window sum %.1f exceeds portal lifetime %.1f",
            stat_id, stats[-1]["sum"], bol,
        )


def push_external_statistics(
    hass: HomeAssistant,
    daily_history: dict[str, dict[str, float]],
    hourly_history: dict[str, dict[str, float]] | None = None,
    lifetime_totals: dict[str, float] | None = None,
) -> int:
    """Re-push the full timeseries for each lifetime metric. Returns count.

    `lifetime_totals` maps a daily/hourly field name to the portal's true
    lifetime (BOL) kWh. When supplied, the whole cumulative series is offset so
    its final point equals BOL -- correcting the absolute lifetime for sites
    older than the API's ~2yr daily-history window. Period deltas (the Energy
    Dashboard bars) are unaffected by a constant offset.
    """
    hourly_history = hourly_history or {}
    lifetime_totals = lifetime_totals or {}
    count = 0
    for field, stat_id, name in EXTERNAL_STATS:
        daily = daily_history.get(field) or {}
        hourly = hourly_history.get(field) or {}
        if not daily and not hourly:
            continue
        stats = _build_stats(daily, hourly)
        if not stats:
            continue
        _anchor_to_lifetime(stats, stat_id, lifetime_totals.get(field))
        metadata = {
            "statistic_id":        stat_id,
            "source":              SOURCE,
            "name":                name,
            "unit_of_measurement": "kWh",
            "has_sum":             True,
            **_MEAN_META,
        }
        async_add_external_statistics(hass, metadata, stats)
        count += 1
        _LOGGER.debug(
            "Pushed %d entries for %s (final cumulative = %.2f kWh)",
            len(stats), stat_id, stats[-1]["sum"],
        )
    if count:
        _LOGGER.info(
            "Pushed external statistics for %d metric(s) "
            "(daily fields: %d, hourly fields: %d)",
            count,
            sum(1 for v in daily_history.values() if v),
            sum(1 for v in hourly_history.values() if v),
        )
    return count
