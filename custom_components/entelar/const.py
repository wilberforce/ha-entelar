"""Constants for the Entelar custom integration."""
DOMAIN = "entelar"

# Default API base. Configurable via config_flow if Univers ever changes.
DEFAULT_API_BASE = "https://app.entelarenergy-emsportal.com"

# Update cadence. Conservative default so we don't hammer Univers.
DEFAULT_UPDATE_INTERVAL_SECONDS = 300   # 5 minutes -- polite to Univers's API

# Config-entry keys
CONF_ACCOUNT = "account"
CONF_PASSWORD = "password"
CONF_API_BASE = "api_base"
CONF_UPDATE_INTERVAL = "update_interval"

# Hash-keys we read out of snapshot_site()'s return -> these match the
# field names existing scripts (snapshot.py) use.
ATTR_BATTERY_SOC = "battery_soc_pct"
ATTR_PV_KW = "pv_active_kw"
ATTR_GRID_KW = "grid_active_kw"
ATTR_BATTERY_KW = "battery_active_kw"
ATTR_LOAD_KW = "load_kw"

# Energy counters from snapshot (when Univers exposes them)
ATTR_PV_TODAY_KWH = "pv_today_kwh"
ATTR_PV_MTD_KWH = "pv_month_kwh"
# Note: lifetime cumulative values are NOT exposed as entity attributes.
# They live as external long-term statistics (statistic_id `entelar:*_lifetime`)
# written by statistics_manager.py. See Plan 09 -> "Historical statistics".

# Cumulative grid + battery MTD (computed from daily history fetched via dt-service).
# `_calc` suffix to make it visually obvious these are NOT direct portal fields.
ATTR_GRID_IMPORT_MTD = "grid_import_mtd_kwh_calc"
ATTR_GRID_EXPORT_MTD = "grid_export_mtd_kwh_calc"
ATTR_BATTERY_CHARGED_MTD = "battery_charged_mtd_kwh_calc"
ATTR_BATTERY_DISCHARGED_MTD = "battery_discharged_mtd_kwh_calc"

# Manufacturer / model metadata for the device_info block
MANUFACTURER = "Univers"
MODEL = "Univers EMS"
MODEL_METER = "Electricity Meter (GRID)"

# Meter (Res_Meter) -- the whole-house revenue grid meter, distinct from the
# inverter/site figures. Its lifetime registers are true odometer totals
# (ever-increasing), ideal for HA's Energy Dashboard and reconciling with the
# electricity retailer's billed usage.
ATTR_METER_POWER_KW = "meter_power_kw"        # METER.ActivePW (signed, -=import)
ATTR_METER_IMPORT_KWH = "meter_grid_import_kwh"  # METER.APConsumedKWH (Imported-Total)
ATTR_METER_EXPORT_KWH = "meter_grid_export_kwh"  # METER.APProductionKWH (Exported-Total)

# Meter history: one API round-trip per day, so the window is kept modest.
# Seeded on first setup; the backfill_statistics service can extend it up to
# the cap. (Start at a week; raise DEFAULT_METER_HISTORY_DAYS later if wanted.)
DEFAULT_METER_HISTORY_DAYS = 7
METER_BACKFILL_MAX_DAYS = 90

# How often to refetch daily history (seconds). Daily aggregates roll over
# at midnight on the Univers side, so a once-per-hour refresh is plenty.
# This also keeps the MTD sensor entities reasonably fresh.
DAILY_HISTORY_REFETCH_SECONDS = 60 * 60

# Hourly external statistics, derived from dt-service 5m power samples.
# Three windows with different refresh cadences (see coordinator).
HOURLY_30D_REFETCH_SECONDS = 24 * 60 * 60   # full 30-day window, once a day
HOURLY_3D_REFETCH_SECONDS  = 60 * 60        # last 3 days, every hour
# "today" window refreshes on every coordinator tick (~every 5 minutes).
HOURLY_WINDOW_DAYS = 30                     # 5m retention limit on Univers

# History reach. The Univers dt-service only retains ~2 years of daily
# aggregates, so this doubles as the default backfill depth AND a hard ceiling
# (asking for more just returns everything available).
DEFAULT_HISTORY_DAYS = 730   # ~2 years -- the portal's retention limit

# Config-entry key: the resolved history start date (ISO 'YYYY-MM-DD').
# Written once at first setup (= today - DEFAULT_HISTORY_DAYS) so the cumulative
# external statistics have a STABLE anchor rather than a window that slides
# forward every day (which would make the lifetime totals drift downward).
CONF_HISTORY_START = "history_start"
