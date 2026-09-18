# Energy Compass

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="custom_components/energy_compass/brand/dark_icon@2x.png">
  <img src="custom_components/energy_compass/brand/icon@2x.png" alt="Energy Compass logo" width="160" height="160">
</picture>

Energy Compass is an advisory Home Assistant integration. It estimates the incremental cost of one more kWh of household use against an optimized battery and grid plan, classifies near-term use as `BOOST`, `CHEAP`, `NORMAL`, or `LIMIT`, and exposes upcoming windows as native entities. It makes no inverter control writes and sends no messages by itself.

## Install

Requires Home Assistant **2026.9.1 or later**. The integration installs `scipy==1.18.1` through its manifest. Its tested ARM64 Core/Python/solver combination and representative benchmark results are recorded in [runtime validation](docs/runtime-validation.md).

In HACS, open **Custom repositories**, add `https://github.com/marino39/ha-energy-compass` as an **Integration**, then download Energy Compass. Restart Home Assistant, and add **Energy Compass** under **Settings → Devices & services → Add integration**. For a manual installation, copy `custom_components/energy_compass` into the Home Assistant config directory's `custom_components` folder and restart. The optional notification blueprint lives outside the HACS-installed integration folder and must be [copied separately](docs/installation.md#opt-in-notifications).

To try a source-independent synthetic setup, choose `Synthetic`, `EUR`, `UTC`, the `generic` preset, and disable PV and battery. Set fixed buy and sell rates in **Tariffs**, a daily household load in **Forecast**, and a finite grid import limit in **Hardware**. In **Sources**, select fixed buy, fixed sell, and fixed load after setting those values, then open **Preview** and confirm. The [installation guide](docs/installation.md) also shows how to select real entities and how to build a dashboard without a custom card.

## What it shows

Read the [published entity and calculation guide](https://marino39.github.io/ha-energy-compass/) for every entity, state, formula, quality flag and worked example. You can also open [docs/index.html](docs/index.html) from a checkout for offline viewing. The guide includes English and Polish entity names and follows your system's light or dark theme. See [publishing setup](docs/publishing.md) for GitHub Pages maintenance.

The integration creates current consumption level and extra-kWh cost sensors, optimized machine state and cost sensors, next change and next `BOOST`/`CHEAP`/`LIMIT` window timestamps, a plan with a bounded outlook, optimizer status, and a forecast-valid binary sensor. Native entity names, diagnostics, and reasons are translated into English and Polish. Automations should compare level and machine state values using their stable uppercase names.

Window alerts require **Notify enabled** in integration options and a nonempty action selected in the [optional blueprint](docs/installation.md#opt-in-notifications). The blueprint uses live integration preferences unless its override toggle is on. It handles favorable and `LIMIT` windows only. The integration does not execute actions.

The extra-kWh estimate assumes the optimizer's proposed plan. Until a controller follows that plan, actual consumption savings can differ under existing automation. A forecast-dependent recommendation is not a measurement of savings. See [model and limitations](docs/model.md) for energy balance, coverage, battery assumptions, and solver behavior.

Under **Planning**, **Sell only PV** defaults to on and limits total grid export to total PV generation **for each local calendar day**. Choose the `pv_energy_today` and `grid_export_energy_today` counters in **Sources** to include energy already generated and exported since midnight. Switch it off to remove this budget. The battery direction hold defaults to **60 minutes**. A hold allows idle periods and switching between PV and grid charging. The export budget includes direct solar and battery export; battery provenance is not tracked and initial SOC is eligible within the budget. See [policy details and limitations](docs/model.md#battery-direction-duration-and-pv-export-budget).

## License

Energy Compass source and original artwork are provided under [Apache License 2.0](LICENSE). SciPy and NumPy are installed as separate dependencies and retain their own licenses; their source is not included here.
