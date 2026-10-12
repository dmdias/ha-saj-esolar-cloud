# SAJ Elekeeper card

A Lovelace card that recreates the SAJ Elekeeper portal in Home Assistant,
using the sensors of this integration:

- **Overview**: house energy flow (grid, PV, load, battery), plant strategy,
  revenue and the energy generated / consumption / grid / battery cards
- **Device**: inverter and battery packs table, inverter/battery detail panel
  (temperatures, cell readings, remaining SOC, time to full charge)
- **Analysis**: day curves (PV, load, grid, battery power and SOC) and
  week / month / year / total energy bars with self-consumption and
  self-sufficiency summaries
- **Comparison**: PV (or any energy counter) per month, quarter or year,
  year against year

It needs no other frontend cards. The sensors are found automatically
through the entity registry. The charts read the Home Assistant history
and long-term statistics, so they fill in as Home Assistant records data.
Older periods only show once statistics exist for them.

## Install

1. Copy `saj-elekeeper-card.js` to `/config/www/saj-elekeeper-card.js`.
2. Settings > Dashboards > ⋮ > Resources > Add resource:
   `/local/saj-elekeeper-card.js`, type *JavaScript module*.
3. Create a dashboard and paste `elekeeper-dashboard.yaml` in the raw
   configuration editor (or add a *Manual* card with
   `type: custom:saj-elekeeper-card`).

## Options

| Option        | Default | Description                                               |
|---------------|---------|-----------------------------------------------------------|
| `view`        | `all`   | `all` shows tabs; or one of `overview`, `device`, `analysis`, `comparison` |
| `plant_uid`   | first   | Plant to show when the integration monitors several       |
| `pv_capacity` |         | Installed PV in kWp, shown under the PV power             |
| `weather`     |         | Weather entity whose temperature is shown on the overview |
| `title`       | plant   | Overrides the plant name in the header                    |

## Preview

Open `preview/index.html` in a browser to see the card with mocked data
(values from the portal screenshots); `?tab=analysis&mode=week` selects
the tab and period.
