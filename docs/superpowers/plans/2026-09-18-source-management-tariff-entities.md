# Source management and tariff entities implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this cohesive task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users inspect and edit individual source bindings, safely remove a selected price continuation or PV group, select usable daily-throughput measurements, and choose buy/sell price entities directly from Tariffs, with documented entity requirements.

**Architecture:** Keep config-entry version 2 and existing source/helper serialization. Add a transient source inventory and precise selection layer around the existing shared SourceEditor, preserving saved mappings when editing. Reuse rate helpers for numeric tariff entities and the existing forecast mapping pipeline for interval prices. Share daily-throughput resolution between preview/form validation and runtime.

**Tech Stack:** Python >=3.14.2, Home Assistant 2026.9.1 native config/option/reconfigure flows, voluptuous selectors, existing scipy==1.18.1 solver, pytest-homeassistant-custom-component==0.13.364, Ruff 0.16.8.

**Spec:** User-approved scope from this conversation: audit items 05–06, plus entities supplying current prices or price forecasts through state/attribute and documentation of requirements. User explicitly selected prices/forecasts, not a tariff-group name. Detailed acceptance criteria are inline below; this is an execution record of that approval.

## Global Constraints

- Work only in `/Users/marcin/Prywatne/ha-recovery/worktrees/energy-compass-source-management` on `codex/source-management-tariff-entities`; base `00b28108617057dc2d6c97c20f1209ad9ee13613`.
- Other agents own the primary checkout and dispatch-policy work. Do not edit or reset their files or integrate unfinished changes.
- Preserve config-entry version 2, saved source/helper formats, entity IDs, solver mathematics, and existing working configurations. Do not persist transient row IDs or UI state.
- All edits remain in the flow draft until fresh preview/base-plan validation and final confirmation. Failed submissions and cancellation preserve saved data/options/title.
- Home Assistant >=2026.9.1; scipy==1.18.1; no dependency, version or release change.
- Buy/sell choices are independent. Numeric scalar values apply unchanged across the planning horizon; future tariff switches require interval data. No tariff-group/schedule engine, provider auto-discovery, negative-price clipping, notifications or dashboard changes.
- All newly exposed forms, labels and errors have English and Polish translations for setup and options. Existing precision, currencies, transformation order and disabled-component behavior remain supported.
- Tests use the pinned local Docker image `ha-energy-compass-test:2026.9.1`, matching `.github/workflows/validate.yml`. No live HA writes or deployment in this task.

### Task 1: Precise source editing, usable daily throughput and direct tariff entity selection

**Files:**
- Modify: `custom_components/energy_compass/source_flow.py` for shared source editing and precise commit routing.
- Create: `custom_components/energy_compass/source_management.py` for transient inventory, selection and exact removal/edit support; keep source parsing in existing modules.
- Modify: `custom_components/energy_compass/config_flow.py` for Tariffs entry points, throughput readiness and preview detail.
- Modify: `custom_components/energy_compass/flow_schema.py` only for shared selectors/schema support if needed.
- Create: `custom_components/energy_compass/sources/throughput.py` for one resolver used by form/preview/runtime.
- Modify: `custom_components/energy_compass/runtime.py` to use the shared throughput resolver without changing the budget formula.
- Modify: `custom_components/energy_compass/strings.json`, `translations/en.json`, `translations/pl.json` for new steps and copy.
- Modify: `README.md`, `docs/installation.md`, `docs/index.html`; add a link in `docs/source-contracts.md` if useful; create `docs/source-requirements.md` for the complete data contract and examples.
- Create: `tests/test_source_management.py`, `tests/test_tariff_entities.py`; modify existing source-flow tests if step navigation changes while preserving their assertions; extend `tests/test_task5_runtime.py` / `tests/test_options_flow.py` for shared validation and atomic save behavior.

**Interfaces and responsibilities:**

- `source_management.py` owns a transient inventory of existing role/binding locations and their user-facing identities. Locations identify exactly one price continuation, PV binding/group, load source, SOC/BMS or optional measurement. SourceEditor owns the current edit transaction. Internal representations can be dataclasses or compact immutable tuples; do not add serialized IDs.
- Existing `EntityBinding`, `IntervalBinding`, `NumericSetting`, `PriceSource` and `LoadSource` remain the persisted model. Numeric price mode must write `helpers["buy_rate"]` or `helpers["sell_rate"]` and set the corresponding PriceSource mode to fixed; runtime already resolves those helpers through `validate_configuration`.
- `resolve_daily_throughput(config: dict, values: dict, states: dict, now: datetime) -> float` resolves normalized AC-side charge-plus-discharge kWh matching the existing solver flow basis. It checks a real entity binding, availability, finite nonnegative result, supported Wh/kWh unit, selected age limit and same installation-local date. Runtime computes exactly its existing `2 * capacity_kwh * daily_cycles` cap and `max(0, cap - observed)` remaining today.

**Chosen native flow shape:** Sources menu → source_inventory / source_add; source_inventory selects one transient SourceRef(role, kind, group_index?, binding_index?) → source_actions → edit / append continuation / confirmed remove. source_add selects a role, source_mode then offers role-compatible choices. Reuse source_entity/source_attribute/source_mapping/source_measurement/source_statistic. Tariffs menu → tariff_values (existing numeric form), tariff_buy, tariff_sell. The source selection validates the original object/reference before applying a precise mutation. Existing flow test navigation can change; retain substantive assertions.

**Acceptance criteria — source inventory and edits:**

1. Sources opens a real inventory with roles, entity IDs, selected attributes, PV groups and a concise interval mapping/coverage descriptor. Include fixed and helper-backed prices, load modes, enabled SOC/BMS, optional measurements, and legacy statistic selections; missing/unpublished sources must remain visible and repairable. Resolve registry-backed renames without adopting unrelated replacement entities.
2. Provide distinct add, edit, remove operations. Editing an existing binding starts with its saved entity/attribute, paths, timezone, unit, sign, publication timestamp, interval length and age settings, rather than preset defaults. Preserve unsupported/non-rendered fields unless explicitly replaced. Opening a form is not a mutation. Invalid edits do not replace the old draft source.
3. Editing/removing one today/tomorrow price binding preserves all siblings and price settings. Editing/removing one PV continuation preserves sibling continuations and other groups. A separate explicit remove-group action removes only the selected PV group; group controls appear only for PV.
4. Removal identifies its concrete target and requires native form confirmation. A negative confirmation/cancel changes nothing. Removing the final required buy/sell/load/SOC source or the final PV group while that feature is enabled is rejected with a clear replacement/disable instruction; never silently create a zero price, reset to defaults, disable a component, or leave an empty group.
5. Add/edit mode choices depend on target: prices fixed/entity/forecast; PV forecast; load daily estimate/forecast/recorder statistic/power history; SOC/BMS measurement; throughput measurement only; other optional diagnostics retain existing supported choices. Server-side validation rejects forged incompatible role/mode combinations. Protect removal of throughput while battery and resolved (including helper-backed) daily_cycles are active; an unresolved cycle helper must not silently permit deletion. Existing optional statistics remain visible and editable as diagnostic-only data.
6. Do not let source-list navigation or switching target reuse stale `_source`, `_binding`, `_helper_key`, edit references or PV group from an earlier operation. Exact edits/removals must still target the selected record after previous operations; test remove then edit and group reindex cases.

**Acceptance criteria — tariffs:**

7. Tariffs provides explicit Buy source and Sell source entry points using native entity selectors. Keep fixed rates and multiplier/addition/VAT settings readily accessible. A scalar source can be a sensor, number or input_number state or numeric attribute. An interval source can select an available entity's structured attribute and use existing mapping screens. Price group names such as G12 are not accepted as numeric rates.
8. Recognize existing rate helpers and forecast bindings; simply opening Tariffs does not rewrite them. Each side independently supports fixed → entity → forecast → fixed. Explicitly switching mode clears only that side's inactive rate helper, preventing an old unavailable helper from blocking the new source. Keep unrelated helpers/sources and all tariff transformations unchanged.
9. Scalar unit choices are installation-currency/kWh or installation-currency/MWh; MWh divides by 1000 exactly once, independently of tariff multiplier/VAT/addition. State unit metadata must match the declared unit when present. Attribute units are declared explicitly. Missing/unavailable/nonfinite values, mismatched currencies/units and owned output sources are rejected without replacing the prior selection. Zero and negative prices remain valid within existing bounds.
10. Scalar age checking has a configurable limit and an explicit disable option for deliberately static manual helpers. Existing custom helper multipliers/age settings must survive untouched edits. If exposing a separate source scale, derive its displayed value from stored multiplier divided by the selected unit-conversion factor, then serialize factor times source scale; this preserves legacy stored multipliers while normalizing newly selected MWh sources once. Source selection previews identify buy AND sell origin, selected attribute, declared/source units and normalized/effective rate; explicitly state the constant-horizon assumption for scalar input. Forecast previews identify selected entries and their mappings without implying a predicted rate from a scalar state.

**Acceptance criteria — throughput:**

11. Do not offer `statistic` for `throughput_today`; reject direct/forged submissions. Legacy stored unsupported statistics remain readable for repair; do not silently delete/convert them. If the cycle limit is enabled, require a valid entity-backed daily throughput before saving the battery setting/final preview. Provide actionable error including the source requirement and a way to return to Sources.
12. Show current source entity/attribute, measured total charge-plus-discharge kWh, daily cap and remaining kWh before final save. Use the shared resolver for editor/preview/runtime so stale, previous-day, unavailable, negative or wrong-unit measurements are rejected consistently. Zero cycles disables the constraint; do not impose this measurement when the battery is disabled.

**Acceptance criteria — documentation:**

13. `docs/source-requirements.md` and installation links document the actual final menu paths, scalar-vs-forecast meaning, supported domains, state/attribute selection, availability, finite values, currency/kWh and currency/MWh conversion, age limits/last_updated behavior, publication time, units and transformation ordering.
14. Include synthetic examples of numeric state, numeric attribute and structured forecast records with value/start/end and a today/tomorrow continuation. Explain timestamp-map support, timezone-aware ISO8601, DST ambiguity rejection, positive duration, matching values on duplicate intervals, conflicting overlaps, and shortened coverage when tomorrow is unpublished. HA state strings containing JSON are not silently parsed as forecasts. Do not claim tariff group labels or provider schedules are inferred.
15. Document throughput as daily AC-side charge PLUS discharge matching the existing solver flow basis, reset at local midnight, current local-date timestamp, nonnegative kWh/Wh and not a net/lifetime/grid/SOC/power value; explain cap/remaining formulas and the unsupported recorder-statistic mode. Update the main guide and installation examples that previously required the generic Sources detour.

- [x] **Step 1: Write behavior tests before production changes.** Use native HA config/options flow fixtures and literal synthetic sources. Every test names the actual bug it catches; no tests that grep prose/source or only assert mocks. At minimum cover two PV groups with two continuations, price today/tomorrow edits/removals, cancellation, saved mapping defaults including absent legacy optional fields, invalid modes, supported throughput, Tariffs state/attribute/forecast flows, switching modes, conversion and final saved configuration. Use real runtime `build_problem` for resolved prices/budgets and keep data/options atomic on failures.

  Hand-checked numerical cases:
  ```python
  # scalar MWh conversion plus existing transformations:
  # 750 PLN/MWh / 1000 * 2 * (1 + 20/100) + 0.1 == 1.9 PLN/kWh
  assert problem.slots[0].buy_per_kwh == pytest.approx(1.9)
  # battery cap 20 kWh, one cycle, 4 kWh observed => 36 kWh remains
  assert dict(problem.remaining_daily_throughput_kwh)["2026-09-18"] == 36
  # negative price retained: -50 PLN/MWh => -0.05 PLN/kWh
  assert problem.slots[0].sell_per_kwh == pytest.approx(-0.05)
  ```
  Assert the public resulting budget, not a duplicated calculation.

- [x] **Step 2: Observe RED on the pinned HA image before implementation.** Ask the controller to run focused commands if Docker approval in the child is unavailable. Record failures caused by absent behavior, not syntax/fixture errors. Do not change tests to hide established valid semantics.
  ```bash
  docker run --rm -v /Users/marcin/Prywatne/ha-recovery/worktrees/energy-compass-source-management:/workspace:ro ha-energy-compass-test:2026.9.1 -m pytest -p no:cacheprovider -q tests/test_source_management.py tests/test_tariff_entities.py --tb=short --show-capture=no
  ```

- [x] **Step 3: Implement the smallest coherent source transaction layer, shared throughput resolver and tariff entry points.** Preserve the existing serializer and solver. Extract the inventory/management concern into the planned module rather than growing SourceEditor with a second copy of parsing logic. Reuse existing entity/mapping/numeric resolution. Copy saved mappings before applying user changes; replace a selected list index rather than rebuilding its entire collection.
  ```python
  candidate = deepcopy(self._draft)
  # Validate the selected source and edited data first.
  # Replace only its exact target location in candidate.
  self._draft = candidate  # after this sub-step's validation succeeds
  ```
  For numeric tariffs, store the binding in the existing rate helper and preserve the chosen rate's source unit and conversion multiplier. Keep the forecast and fixed-price representations valid when mode changes. Validation failures must return a form with useful translated error/detail, not throw a traceback.

- [x] **Step 4: Observe GREEN, add translations and documentation, then self-review.** Verify complete source transactions through final preview/save for new setup and existing options/reconfigure. Test registry renames and own-output refusal using native HA registry. Include loop cases edit→back/change role→edit and remove group→edit remaining group. Add human docs; do not write tests for prose. The controller has supplementary audited contracts and draft doc text under `.superpowers/source-management-notes/`.

- [x] **Step 5: Verify regression tests discriminate, then the exact final CI checks.** Revert only relevant production behavior in an isolated temporary copy or restore with try/finally; keep tests unchanged. Each new bug repro must fail for the intended reason. Cover the original group-wide deletion bug and unsupported-throughput-statistic acceptance, plus stale mapping/price-helper regressions introduced by this task. Coordinate with controller so no source mutation overlaps tests or review. Final production must be byte-identical to the green snapshot afterward.
  ```bash
  docker run --rm -v /Users/marcin/Prywatne/ha-recovery/worktrees/energy-compass-source-management:/workspace:ro ha-energy-compass-test:2026.9.1 -m pytest -p no:cacheprovider -q
  docker run --rm -v /Users/marcin/Prywatne/ha-recovery/worktrees/energy-compass-source-management:/workspace:ro ha-energy-compass-test:2026.9.1 -m ruff check --no-cache .
  docker run --rm -v /Users/marcin/Prywatne/ha-recovery/worktrees/energy-compass-source-management:/workspace:ro ha-energy-compass-test:2026.9.1 -m ruff format --check --no-cache .
  docker run --rm ha-energy-compass-test:2026.9.1 -m pip check
  ```
  Run hassfest on the final translations/manifest; controller handles publishing a feature branch/PR and remote CI after independent review. No merge/release/live deployment from this task.

- [x] **Step 6: Commit and report.** Commit only this task's files. Report RED/GREEN commands and evidence, changed files, all validation results, remaining concerns and exact commit. The controller performs independent task and whole-branch review; do not spawn reviewers or other agents.

Throughput basis clarification: the existing solver budgets charge + discharge on its AC-side flow basis; SOC efficiency is accounted for separately. The selected measurement must use the same basis. DC counters require upstream conversion accounting for the respective charging/discharging losses; Energy Compass does not infer or apply that conversion. Solver mathematics remain unchanged.

Execution record: Task 1 was implemented and reviewed in commits `b5a859e` and `8270198`, with focused RED/GREEN and narrow regression-discrimination evidence in the local SDD report. The original product base remains `00b28108617057dc2d6c97c20f1209ad9ee13613`. The subsequent feature-branch integration base is published `origin/main` at `f88d9d4e9f914a279f8be4be9cf9026d39ea45b4` (v0.1.3); its dispatch policy, daily export counters, dependencies and version are preserved during the branch merge. This is not a release or a merge of the feature branch into main.
