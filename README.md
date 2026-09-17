# Energy Compass

Energy Compass is a read-only Home Assistant advisory integration. It estimates the incremental cost of one more kWh of household use against an optimized battery and grid plan, classifies near-term use as BOOST, CHEAP, NORMAL, or LIMIT, and exposes upcoming windows as native entities. It never controls equipment or sends messages by itself.

Install it as a custom integration, then configure sources and settings in the Home Assistant UI. [Installation and dashboard guide](docs/installation.md) covers the visual setup, optional dashboard example, and opt-in notification blueprint. [Model and limitations](docs/model.md) explains the optimizer and the meaning of its estimates.

Window alerts require **Notify enabled** in integration options and a nonempty action selected in the blueprint. The blueprint uses live integration preferences unless its override toggle is on.

The extra-kWh estimate assumes the optimizer's proposed plan. Until a controller follows that plan, actual consumption savings can differ under existing automation. A forecast-dependent recommendation is not a measurement of savings.

The integration supports Home Assistant 2026.9.1 or later, subject to the tested release and installation requirements. It does not require a custom dashboard card; an optional ApexCharts recipe is available for users who already have that card installed.
