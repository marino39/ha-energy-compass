# AGENTS.md — Energy Compass

## Documentation sweep (required for every change)

Any change to behaviour, entities, settings, defaults, strategies, modes, reason codes, blueprints
or the release version ships with a **doc sweep** in the same commit or PR. The docs describe the
code; when they disagree, the code is right and the docs are fixed.

1. **Map the change** to every affected doc using the table below. Search the docs for each
   changed identifier (setting key, state, reason code, strategy, default value) with
   `grep -rn '<identifier>' README*.md docs/*.md docs/index.html` and include every hit.
2. **Update English and Polish together.** `docs/guide.en.md` ↔ `docs/guide.pl.md` and
   `README.md` ↔ `README.pl.md` are mirrors: same sections, same tables, same diagrams, same
   numbers. Polish UI labels come from `custom_components/energy_compass/translations/pl.json`.
3. **Verify each claim against the code** (`engine/`, `runtime.py`, `coordinator.py`,
   `settings.py`, `translations/`), not against other docs.
4. **Update diagrams** whose logic changed, then **rebuild the Pages guides**:
   `pip install -e '.[docs]'` (or `pip install markdown==3.8`), then
   `python tools/build_guides.py`. It renders every Mermaid block to light/dark SVGs in
   `docs/assets/diagrams/` (Node.js/npx required) and regenerates `docs/guide.en.html` and
   `docs/guide.pl.html`. Commit the generated files with the Markdown; edit only the `.md`.
5. **Check links**: every relative link and `#anchor` in `README*.md` and `docs/guide.*.md`
   resolves (GitHub heading slugs).
6. **On release**, bump the version stated in the intro of both guides and in `docs/index.html`
   (meta description, sidebar, intro, footer), then rebuild the guides.

Done when every grep hit is current, both languages match, `tools/build_guides.py` runs clean
and its output is committed, and no link is broken. State in the PR which docs changed, or why
none needed to.

### Change → docs map

| Change | Update |
| --- | --- |
| Entity added/removed/renamed, state value, attribute, availability rule | `guide.*` (entity overview, state tables, availability diagram), `docs/index.html` (entity index + article), `README*.md` entity paragraph, `translations/*.json` + `strings.json` |
| Optimizer status, Alert code, `calculating`/`invalid_input` reason, plan retention | `guide.*` (optimizer status, Alert, retention diagrams + tables), `docs/model.md` § Plan retention, `docs/index.html` |
| Operating mode, minimum duration, safety exception, Sell only PV, grid-charge ceiling, episode hurdles, import penalty, standby loss, terminal rule | `guide.*` § Operating modes + policy table, `docs/model.md`, `README*.md` policy paragraphs |
| Strategy added/changed, strategy flag or weight, strategy default | `guide.*` § Dispatch strategies (summary table, per-strategy section, formula), `docs/model.md` § Dispatch strategies, `README*.md` § Dispatch strategy, `docs/index.html` Strategy article |
| Consumption levels, probes, percentiles, windows, flexible depth | `guide.*` § Consumption levels / Windows, `docs/model.md` § Incremental consumption, `docs/index.html` |
| Recalculation cadence, refresh, rate limit, SOC trigger, time budgets | `guide.*` § Recalculation cadence, `docs/model.md` § Recalculation cadence, `README*.md` last paragraph |
| Any setting key or default value | every doc citing it (grep the key and the old value) |
| `strategy_switch` or notification blueprint | `guide.*` § Automatic strategy switching, `docs/installation.md` |
| Source requirements, units, tariffs | `docs/source-requirements.md`, `docs/source-contracts.md`, `docs/tariff-helper.md` |
| Home Assistant / SciPy / runtime requirement | `README*.md` § Install, `docs/runtime-validation.md` |
