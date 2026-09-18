# Illustrative time-of-use tariff helper

[This YAML example](../examples/tariff-helper.yaml) creates one Home Assistant Template sensor with a current PLN/kWh state and a `forecast` attribute containing 49 consecutive hourly `{start, end, price}` records. It provides an illustrative fixed schedule for G11, G12 or G12w, not your operator's official tariff. Replace the rates and confirm every time band and date against your contract before using its prices for decisions. It does not predict dynamic market prices.

## Install and customize

Copy the example to your Home Assistant configuration directory, for instance as `packages/tariff-helper.yaml`. If `homeassistant.packages` already uses `!include_dir_named packages`, place the file there and keep that configuration. If it uses another package directory or a named package map, follow its existing layout, for example `tariff_helper: !include packages/tariff-helper.yaml` under the existing `packages:` map. If `homeassistant:` exists but has no `packages:` key, add `packages: !include_dir_named packages` under it. Otherwise add:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

Do not replace existing `homeassistant:` or `template:` sections. You can instead merge the example's `template:` list entry into your existing `template:` list. When using `template: !include ...`, merge it into the included list rather than creating a second top-level key. Check the Home Assistant configuration, reload Template entities after editing the rates or dates, and inspect the entity state and attributes. On a fresh configuration the startup trigger updates it; if the Template integration is reloaded without a startup event, the next quarter-hour trigger updates it (at minutes 00, 15, 30 or 45). [Home Assistant packages](https://www.home-assistant.io/docs/configuration/packages/) and [Template integration configuration](https://www.home-assistant.io/integrations/template/) describe these options.

Edit `tariff_group`, `base_rate`, `off_peak_rate` and `off_peak_dates` near the top of the YAML. The example uses 0.90 and 0.55 PLN/kWh solely as placeholders; enter **final** prices in the same currency as your Energy Compass installation. `G11` uses `base_rate` for every hour. For `G12`, local hours 13:00–15:00 and 22:00–06:00 use `off_peak_rate`. `G12w` additionally applies that rate all Saturday and Sunday and on explicitly listed local calendar dates, for example `off_peak_dates: ['2026-12-24']`. This example has no public-holiday lookup. Incorrect or nonfinite settings leave the sensor unavailable instead of publishing a free price.

The forecast begins at the current UTC hour and uses 49 real one-hour intervals, so it covers at least 48 hours after an update even between hour boundaries. Each interval is classified in Home Assistant's configured local time zone. A daylight-saving day can contain 23 or 25 real hours; timestamps retain their UTC offsets, including both occurrences of a repeated local hour. Set your Home Assistant installation time zone correctly (for example, `Europe/Warsaw`).

## Connect Energy Compass

Open **Tariffs → Buy source** (and **Sell source** separately if appropriate), choose **Interval forecast**, then select:

| Field | Value |
| --- | --- |
| Entity | `sensor.energy_compass_tariff_price` |
| Attribute | `forecast` |
| Value path | `price` |
| Start path | `start` |
| End path | `end` |
| Unit | `PLN/kWh` |
| Publication path, optional | `attributes.generated_at` |
| Maximum age, if checking age | `3600` seconds |

Confirm the actual entity ID in Home Assistant before selection, since an existing entity or an earlier registry rename can change it. The entity must have a finite numeric available state, a list-valued `forecast` attribute with contiguous aware intervals, and prices in the installation currency. Select the **forecast**, not the state: a current numeric state alone is treated as constant over the horizon. The generated timestamp and attributes refresh every 15 minutes, making a 3600-second age limit suitable under normal operation; a missed refresh eventually makes the source stale. Verify Preview's coverage, prices and resolved unit.

If these rates already include all taxes and charges, set the existing buy/sell tariff transformations to multiplier `1`, VAT disabled, and addition `0`. Configure buy and sell independently; copying a purchase tariff into sale pricing is only appropriate if your contract actually uses the same rate schedule. The [source selection guide](source-requirements.md) explains the supported mapping and age checks.
