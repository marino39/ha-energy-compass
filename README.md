# Energy Compass

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="custom_components/energy_compass/brand/dark_icon@2x.png">
  <img src="custom_components/energy_compass/brand/icon@2x.png" alt="Energy Compass logo" width="160" height="160">
</picture>

Energy Compass is an advisory Home Assistant integration. It estimates the incremental cost of one more kWh of household use against an optimized battery and grid plan, classifies near-term use as `BOOST`, `CHEAP`, `NORMAL`, or `LIMIT`, and exposes upcoming windows as native entities. It makes no inverter control writes and sends no messages by itself.

## Install

Requires Home Assistant **2026.9.1 or later**. The integration installs `scipy==1.18.1` through its manifest. Its tested ARM64 Core/Python/solver combination and representative benchmark results are recorded in [runtime validation](docs/runtime-validation.md).

In HACS, open **Custom repositories**, add `https://github.com/marino39/ha-energy-compass` as an **Integration**, then download Energy Compass. Restart Home Assistant, and add **Energy Compass** under **Settings → Devices & services → Add integration**. For a manual installation, copy `custom_components/energy_compass` into the Home Assistant config directory's `custom_components` folder and restart. The optional notification blueprint lives outside the HACS-installed integration folder and must be [copied separately](docs/installation.md#opt-in-notifications).

To try a source-independent synthetic setup, choose `Synthetic`, `EUR`, `UTC`, the `generic` preset, and disable PV and battery. Set fixed buy and sell rates in **Tariffs → Fixed rates and transformations**, a daily household load in **Forecast**, and a finite grid import limit in **Hardware**. Then open **Preview** and confirm. The [installation guide](docs/installation.md) shows the full setup and dashboard steps, while [source requirements](docs/source-requirements.md) explain real entity and forecast inputs.

## What it shows

Read the [published entity and calculation guide](https://marino39.github.io/ha-energy-compass/) for every entity, state, formula, quality flag and worked example. You can also open [docs/index.html](docs/index.html) from a checkout for offline viewing. The guide includes English and Polish entity names and follows your system's light or dark theme. See [publishing setup](docs/publishing.md) for GitHub Pages maintenance.

The integration creates current consumption level and extra-kWh cost sensors, optimized machine state and cost sensors, next change and next `BOOST`/`CHEAP`/`LIMIT` window timestamps, a plan with a bounded outlook, optimizer status, a forecast-valid binary sensor, and a diagnostic **Alert** binary sensor. Native entity names, diagnostics, and reasons are translated into English and Polish. Automations should compare level and machine state values using their stable uppercase names.

Input or calculation failures turn **Alert** on with `code`, `reason`, `since`, `plan_retained`, and `last_successful_plan_at`. A covered previous plan continues through its scheduled intervals, including during retries. `plan_retained: true` identifies advice based on that earlier snapshot; **Forecast valid** remains on until the plan coverage ends. A successful replacement clears the alert. SOC corrections can recover after fresh, plausible reports span at least one minute. See [retention and SOC recovery](docs/model.md#plan-retention-and-alerts).

Coordinator updates skip state writes for entities whose value, availability, and attributes are unchanged. Changes to forecast validity, plan attributes, refresh status, or window timing still publish even when the primary sensor value stays the same.

Window alerts require **Notify enabled** in integration options and a nonempty action selected in the [optional blueprint](docs/installation.md#opt-in-notifications). The blueprint uses live integration preferences unless its override toggle is on. It handles favorable and `LIMIT` windows only. The integration does not execute actions.

The extra-kWh estimate assumes the optimizer's proposed plan. Until a controller follows that plan, actual consumption savings can differ under existing automation. A forecast-dependent recommendation is not a measurement of savings. See [model and limitations](docs/model.md) for energy balance, coverage, battery assumptions, and solver behavior.

Under **Planning**, **Sell only PV** defaults to on and limits total grid export to total PV generation **for each local calendar day**. Choose the `pv_energy_today` and `grid_export_energy_today` counters in **Sources** to include energy already generated and exported since midnight. Switch it off to remove this budget. Actual operating modes have a **60-minute** minimum duration and **0.1 kW (100 W)** minimum active power by default. Power may vary above the floor; changing PV/grid source, discharge destination, HOLD or CURTAIL starts a distinct mode. Set duration to zero to disable mode duration and mode power restrictions; the separate export-benefit rule below may still apply its export power floor. CURTAIL excludes battery activity under the strict policy. An observed SoC bound may temporarily require HOLD while preserving an existing commitment. The export budget includes direct solar and battery export; battery provenance is not tracked and initial SOC is eligible within the budget. See [policy details and limitations](docs/model.md#operating-mode-duration-and-pv-export-budget).

**Limit grid-charging price**, under **Planning**, optionally sets a **Maximum grid-charging price** in the installation currency/kWh. It compares the final tariff price, including configured adjustments; equality is allowed. The switch defaults to off, and zero is a valid ceiling for free or negative prices. Above the ceiling, household supply and surplus-PV charging remain available. A new grid-charge run must stay within the ceiling for its full minimum duration. If a carried charge commitment conflicts with the ceiling before its deadline, advice pauses immediately in HOLD until that original deadline, preserving its clock. The ceiling also works when mode duration is disabled.

**Minimum benefit per additional battery-export period** also appears under **Planning**. It defaults to **1 unit of the configured currency** (1 PLN in a PLN installation); finite values from 0 to 1000 are supported and **0 disables it**. The planner requires that each additional period of physical battery discharge with simultaneous grid export improve full-horizon economic cost by at least that amount compared with a feasible plan having fewer periods. Prices, losses, battery wear and optional terminal value already enter that comparison. This is an incremental planning hurdle, not a per-kWh spread, a tracked recharge cycle or a realized-profit guarantee. A currently published export period continues without another reserve; future periods are never paid in advance. Real low-power export can bridge a price valley without recharging. With mode duration **0**, the export rule still requires both discharge and export to meet the minimum power while exporting from the battery; standalone household discharge, PV-only export and simultaneous curtailment/charging remain possible. The plan reports the horizon's `new_export_episodes` and `export_episode_reserve` separately from physical costs and consumption-probe differences. See [the mathematical contract](docs/model.md#minimum-additional-export-benefit).


## License

Energy Compass source and original artwork are provided under [Apache License 2.0](LICENSE). SciPy and NumPy are installed as separate dependencies and retain their own licenses; their source is not included here.

Planning's refresh interval controls full periodic recalculation (up to once per hour). Important source changes still recalculate earlier; native quarter-hour boundaries advance the cached plan without a new solve. Performance allows an overall calculation budget up to five minutes and individual consumption probes up to 30 seconds. Existing defaults stay unchanged.
