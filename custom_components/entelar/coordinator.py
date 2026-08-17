"""DataUpdateCoordinator for the Entelar custom integration.

Owns the login session and the daily-history cache, refreshes both, and
polls `snapshot_site` on the configured interval.

The snapshot has live values + PV cumulative (TD/MTD/BOL). Grid and battery
MTD/lifetime kWh are NOT in the snapshot -- Univers doesn't expose those
cumulative fields. We compute them by summing the daily-history table
(fetched separately from dt-service) since solar commissioning.

Daily history is fetched once on first refresh, then re-fetched every
`DAILY_HISTORY_REFETCH_SECONDS` (default 1h).
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator, UpdateFailed,
)

from .const import (
    CONF_HISTORY_START,
    DAILY_HISTORY_REFETCH_SECONDS,
    DEFAULT_HISTORY_DAYS,
    HOURLY_30D_REFETCH_SECONDS,
    HOURLY_3D_REFETCH_SECONDS,
    HOURLY_WINDOW_DAYS,
    DEFAULT_API_BASE,
    DEFAULT_UPDATE_INTERVAL_SECONDS,
    ATTR_GRID_IMPORT_MTD,
    ATTR_GRID_EXPORT_MTD,
    ATTR_BATTERY_CHARGED_MTD,
    ATTR_BATTERY_DISCHARGED_MTD,
)
from .daily_history import fetch_daily_history
from .errors import EntelarError, EntelarLoginError
from .hourly_history import fetch_hourly_history
from .login import login, is_expired
from .snapshot import discover_site, snapshot_site
from .statistics_manager import push_external_statistics

_LOGGER = logging.getLogger(__name__)


# Map (daily_history field, MTD attr) -- lifetime cumulative values are NOT
# computed here. They flow into HA's long-term statistics under the external
# statistic_ids `entelar:*_lifetime` (see statistics_manager.py).
_CUMULATIVE_FIELD_MAP = [
    ("OffGridEnergy",       ATTR_GRID_IMPORT_MTD),
    ("OnGridEnergy",        ATTR_GRID_EXPORT_MTD),
    ("ChargeProduction",    ATTR_BATTERY_CHARGED_MTD),
    ("DischargeProduction", ATTR_BATTERY_DISCHARGED_MTD),
]


class EntelarCoordinator(DataUpdateCoordinator):
    """Polls Entelar's site overview and exposes the parsed dict."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        config_entry: ConfigEntry,
        account: str,
        password: str,
        history_start: date | None,
        api_base: str = DEFAULT_API_BASE,
        update_interval_seconds: int = DEFAULT_UPDATE_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="entelar",
            update_interval=timedelta(seconds=update_interval_seconds),
        )
        self._entry = config_entry
        self._account = account
        self._password = password
        self._api_base = api_base
        # Stable start bound for daily-history fetches. If None, it's resolved
        # once from the portal's operativeDate on first login and persisted.
        self._history_start = history_start
        self._session: dict | None = None
        # Daily history cache: {field: {YYYY-MM-DD: kwh}} from commissioning.
        self._daily_history: dict[str, dict[str, float]] = {}
        self._daily_history_fetched_at: float = 0.0
        # Hourly history cache: {field: {'YYYY-MM-DD HH:00': kwh}} for the
        # last HOURLY_WINDOW_DAYS days. Derived from dt-service 5m samples.
        self._hourly_history: dict[str, dict[str, float]] = {}
        self._hourly_30d_fetched_at: float = 0.0
        self._hourly_3d_fetched_at: float = 0.0

    @property
    def daily_history(self) -> dict[str, dict[str, float]]:
        """Read-only view of the cached daily history. Empty until first fetch."""
        return self._daily_history

    async def _ensure_session(self) -> dict:
        """Login if we don't have a session or the token is near-expiry."""
        if self._session and not is_expired(self._session):
            return self._session
        try:
            session = await self.hass.async_add_executor_job(
                login, self._api_base, self._account, self._password
            )
        except EntelarLoginError as e:
            raise ConfigEntryAuthFailed(f"Entelar login failed: {e}") from e
        session = await self.hass.async_add_executor_job(discover_site, session)
        self._resolve_history_start(session)
        self._session = session
        return session

    def _resolve_history_start(self, session: dict) -> None:
        """Pin the daily-history start date once and persist it.

        Prefers the portal's `operativeDate` (site commissioning), falling back
        to `today - DEFAULT_HISTORY_DAYS`. Either way it's floored at the ~2yr
        API retention limit, since the portal won't serve older data anyway.
        """
        if self._history_start is not None:
            return
        default = date.today() - timedelta(days=DEFAULT_HISTORY_DAYS)
        operative = session.get("operative_date")
        anchor = default
        if operative:
            try:
                anchor = max(date.fromisoformat(operative), default)
            except ValueError:
                _LOGGER.debug("Unparseable operativeDate %r; using default", operative)
        self._history_start = anchor
        self.hass.config_entries.async_update_entry(
            self._entry,
            data={**self._entry.data, CONF_HISTORY_START: anchor.isoformat()},
        )
        _LOGGER.info(
            "Entelar history anchor set to %s (portal operativeDate=%s)",
            anchor, operative,
        )

    async def _refresh_daily_history(self, session: dict) -> None:
        """Fetch daily aggregates from commissioning to today."""
        try:
            history = await self.hass.async_add_executor_job(
                fetch_daily_history,
                session,
                self._history_start,
                date.today(),
            )
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning(
                "Daily-history fetch failed (%s); cumulative sensors will use "
                "the previous cache or be unavailable", e
            )
            return
        if not any(history.values()):
            _LOGGER.warning(
                "Daily-history fetch returned no rows; cumulative sensors "
                "will be unavailable until dt-service responds"
            )
            return
        self._daily_history = history
        self._daily_history_fetched_at = time.time()
        _LOGGER.info(
            "Daily history refreshed: %s",
            {k: len(v) for k, v in history.items()},
        )

    async def _refresh_hourly_window(self, session: dict, days_back: int) -> None:
        """Fetch 5m data for the last `days_back` days, integrate to hourly.

        Merges the result into `self._hourly_history` (newer values override).
        `days_back=0` means today only.
        """
        end = date.today()
        start = end - timedelta(days=days_back)
        try:
            window = await self.hass.async_add_executor_job(
                fetch_hourly_history, session, start, end,
            )
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning(
                "Hourly history fetch (%dd back) failed: %s", days_back, e,
            )
            return
        if not any(window.values()):
            _LOGGER.debug(
                "Hourly history fetch (%dd back) returned no rows", days_back,
            )
            return
        # Merge per-field; newer hour keys overwrite older
        for field, hours in window.items():
            self._hourly_history.setdefault(field, {})
            self._hourly_history[field].update(hours)
        # Prune anything older than the window we maintain
        cutoff = (end - timedelta(days=HOURLY_WINDOW_DAYS + 1)).isoformat()
        for field, hours in self._hourly_history.items():
            self._hourly_history[field] = {
                k: v for k, v in hours.items() if k[:10] >= cutoff
            }
        _LOGGER.debug(
            "Hourly history merged (%dd back): cache counts now %s",
            days_back,
            {k: len(v) for k, v in self._hourly_history.items()},
        )

    def _compute_cumulative(self, snap: dict) -> None:
        """Add grid/battery MTD computed from daily history.

        Mutates snap in place. If we don't have history yet, the keys stay
        absent and the corresponding entities show 'unavailable'.
        Lifetime cumulative values are NOT written here -- they go to HA's
        long-term statistics directly (see statistics_manager.py).
        """
        if not self._daily_history:
            return
        today = date.today()
        month_start_iso = today.replace(day=1).isoformat()

        for daily_field, mtd_key in _CUMULATIVE_FIELD_MAP:
            daily = self._daily_history.get(daily_field) or {}
            if not daily:
                continue
            snap[mtd_key] = round(
                sum(v for d, v in daily.items() if d >= month_start_iso), 3
            )

    async def async_backfill(self, days: int) -> None:
        """On-demand re-fetch of `days` of history + full statistics rewrite.

        Backs the `entelar.backfill_statistics` service. Fetches daily
        aggregates for the last `days` days (capped at the portal's retention
        limit) plus the hourly window, then re-pushes the external statistics.
        Idempotent -- re-pushing overwrites matching timestamps in place.
        """
        days = max(1, min(days, DEFAULT_HISTORY_DAYS))
        start = date.today() - timedelta(days=days)
        _LOGGER.info("Entelar backfill requested: %s days (from %s)", days, start)
        session = await self._ensure_session()
        try:
            history = await self.hass.async_add_executor_job(
                fetch_daily_history, session, start, date.today(),
            )
        except Exception as e:  # noqa: BLE001
            raise HomeAssistantError(f"Entelar backfill failed: {e}") from e
        if any(history.values()):
            self._daily_history = history
            self._daily_history_fetched_at = time.time()
        # Refresh the hourly window too (bounded by the portal's 5m retention).
        await self._refresh_hourly_window(
            session, days_back=min(days, HOURLY_WINDOW_DAYS)
        )
        self._hourly_30d_fetched_at = time.time()
        self._hourly_3d_fetched_at = time.time()
        push_external_statistics(
            self.hass, self._daily_history, self._hourly_history,
        )
        await self.async_request_refresh()

    async def _async_update_data(self) -> dict:
        try:
            session = await self._ensure_session()

            now = time.time()

            # --- Daily history (commissioning -> today) ---
            # Hourly refresh keeps the MTD sensors fresh and feeds the older
            # portion of the external statistics timeseries.
            daily_due = (
                not self._daily_history
                or (now - self._daily_history_fetched_at)
                > DAILY_HISTORY_REFETCH_SECONDS
            )
            if daily_due:
                await self._refresh_daily_history(session)

            # --- Hourly window (last 30 days) ---
            # Three cadences: full window on startup & once a day, last 3 days
            # every hour, today on every poll.
            hourly_30d_due = (
                not self._hourly_history
                or (now - self._hourly_30d_fetched_at) > HOURLY_30D_REFETCH_SECONDS
            )
            hourly_3d_due = (
                (now - self._hourly_3d_fetched_at) > HOURLY_3D_REFETCH_SECONDS
            )
            if hourly_30d_due:
                await self._refresh_hourly_window(session, days_back=HOURLY_WINDOW_DAYS)
                self._hourly_30d_fetched_at = now
                self._hourly_3d_fetched_at = now      # subsumes 3d
            elif hourly_3d_due:
                await self._refresh_hourly_window(session, days_back=3)
                self._hourly_3d_fetched_at = now
            else:
                # Every tick: refresh today's bars only
                await self._refresh_hourly_window(session, days_back=0)

            # --- Push combined external statistics ---
            # Idempotent; safe to call every tick.
            if self._daily_history or self._hourly_history:
                try:
                    push_external_statistics(
                        self.hass,
                        self._daily_history,
                        self._hourly_history,
                    )
                except Exception as e:  # noqa: BLE001
                    _LOGGER.warning("Failed to push external statistics: %s", e)

            # --- Live snapshot for live + MTD sensor entities ---
            snap = await self.hass.async_add_executor_job(snapshot_site, session)
            self._compute_cumulative(snap)
            return snap
        except ConfigEntryAuthFailed:
            raise
        except EntelarError as e:
            self._session = None
            raise UpdateFailed(f"Entelar API error: {e}") from e
        except Exception as e:  # noqa: BLE001
            self._session = None
            raise UpdateFailed(f"Unexpected error: {e}") from e
