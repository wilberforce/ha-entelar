# Entelar Energy (Univers EMS) — Home Assistant integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A Home Assistant integration for residential solar + battery sites monitored
through the **Univers EMS portal** used by **Entelar Energy** (New Zealand),
served from `app.entelarenergy-emsportal.com`.

It logs in with your portal credentials, polls the site overview, and exposes
live power, battery, and cumulative energy figures — plus long-term statistics
backfilled from the portal's own history so the **Energy Dashboard** works from
day one.

> **Unofficial.** This is a community project, not affiliated with or endorsed
> by Entelar Energy or Univers/Envision. It talks to a private portal API that
> may change without notice. Use at your own risk.

## Features

- **Live sensors:** PV power, load, grid power, battery power, battery SoC.
- **Cumulative energy:** PV production (today / month), grid import & export
  (month), battery charged & discharged (month).
- **Long-term statistics** under the `entelar:` source (PV, grid import/export,
  battery charged/discharged lifetime) — ready to drop into the Energy
  Dashboard, backfilled from portal history.
- **Grid meter device:** live meter power and lifetime import/export registers,
  when the site exposes a `Res_Meter`.
- **`entelar.backfill_statistics` service** to (re)import history on demand.
- Config flow + options flow (adjustable polling interval); no YAML required.

> **Scope note:** the grid/meter figures are measured at the **inverter's
> grid connection** (the solar circuit). On multi-phase installs where solar
> only feeds part of the home, they can be well below whole-house consumption —
> that whole-house figure lives only at the utility revenue meter (e.g. your
> retailer's data), not in this portal.

## Installation (HACS custom repository)

1. In HACS → **Integrations** → ⋮ → **Custom repositories**.
2. Add `https://github.com/wilberforce/ha-entelar` with category **Integration**.
3. Install **Entelar Energy (Univers EMS)**, then restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → Entelar Energy**, and
   enter the same account/password you use on the Univers EMS portal.

## Configuration

| Field | Notes |
|-------|-------|
| Account | Email or username for the Univers EMS portal. |
| Password | Portal password. RSA-encrypted client-side before it's sent; never stored in plaintext logs. |
| API base URL | Leave the default unless directed otherwise. |
| Update interval | Poll cadence in seconds (30–3600, default 300). Changeable later via the integration's **Configure** button. |

## Backfilling history

The integration automatically imports available history on setup and keeps the
long-term statistics self-healing on every poll. To force a re-import (e.g. to
fill a gap), call the service:

```yaml
action: entelar.backfill_statistics
data:
  days_to_fetch: 730
```

## Energy Dashboard

Add these statistics under **Settings → Dashboards → Energy**:

- **Solar production** → `entelar:pv_lifetime`
- **Grid consumption** → `entelar:grid_import_lifetime`
- **Return to grid** → `entelar:grid_export_lifetime`
- **Home battery** → `entelar:battery_charged_lifetime` (in) /
  `entelar:battery_discharged_lifetime` (out)

## Known limitations

- **~2 years of detailed history.** The portal's data service retains roughly
  two years of daily aggregates and about 30 days of 5-minute (→ hourly) data.
  The **absolute lifetime totals stay correct** even for older sites: the
  statistics are anchored to the portal's own lifetime (BOL) figures, so only
  the *shape* of history before the two-year window is missing, not the totals.
- **Cloud polling.** Requires the portal to be reachable; there's no local API.
- Reverse-engineered endpoints — a portal update could break a call until the
  integration is updated to match.

## License

[MIT](LICENSE).
