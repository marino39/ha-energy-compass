# Configuration Trust Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan. Treat the four coupled fixes below as **one implementation task and one independent review gate**; the ordered steps are checkpoints within that task. Steps use checkbox syntax for tracking.

**Goal:** Make first-time configuration deliberate, preview feasibility truthful, native SOC freshness correct, and household-load provenance visible without breaking existing installations.

**Architecture:** Retain the existing atomic configuration draft and bounded solver. Add setup-only confirmations, a base-solve preview stage, one shared SOC timestamp resolver, and additive load-provenance results that preserve existing tuple APIs.

**Tech Stack:** Python >=3.14.2, Home Assistant 2026.9.1 native config/options flows, SciPy 1.18.1 MILP, existing pytest and Ruff toolchain.

**Spec:** The user-approved four fixes recorded below; audit evidence at `/Users/marcin/Prywatne/ha-recovery/docs/energy-compass-configuration-audit-2026-09-18/README.md` and adjacent `test_ux_audit.py`. Audit tests assert the defects exist; do not use their original assertions as regression acceptance criteria.

## Task 1: Four configuration trust repairs

### Global constraints and approved scope

- Work only in `/Users/marcin/Prywatne/ha-recovery/worktrees/energy-compass-configuration-fixes`, branch `codex/energy-compass-configuration-fixes`, base `d8a580d`.
- User approval covers exactly audit gaps 01–04: deliberate buy/load selection or confirmation; bounded solver feasibility in preview/save; fresh identical native SOC reports; visible history/fallback provenance and sample coverage.
- The design approval gate is satisfied. No additional approval is needed for implementing or testing these changes locally.
- No live Home Assistant edits, deployment, publishing, or merging. Do not implement audit gaps 05–10.
- Existing installations remain usable, including intentional zero prices, the old default daily estimate, existing helper-backed settings, and explicit custom SOC timestamp paths.
- Do not change dependency versions, solver mathematics, recommendation classification, entity identity, or configuration-entry version solely for these additive changes.
- Another branch, `codex/preserve-entities-during-calculation` (`15598f3`), changes coordinator/entity/sensor retention behavior. Do not edit `coordinator.py` or `sensor.py`; the narrowly required additive changes in `entity.py` must preserve its current data-selection and availability behavior.
- Parent verified the untouched baseline in isolated Docker: **225 passed in 5.32s**. The implementation worker repeats targeted checks after changes and the full suite at the final gate.

## 1. Goal

Users can consciously approve the inputs they intend, cannot save a preview whose base plan has failed validation or optimization, and can see when an apparently complete forecast uses estimated household load.

## 2. Approach

### First-time confirmation, without changing stored defaults

Keep `default_configuration()` numeric defaults and source schemas compatible. In `Editor.async_step_preview`, add **two distinct, initially false** confirmation fields for new installations only: `confirm_buy_source` and `confirm_load_source`, alongside the existing final `confirm`. Require all three to be exactly `True` before creating a new entry. Do not persist the confirmation fields in configuration data. Existing options/reconfigure flows, identified by `_existing_installation`, retain their existing final-confirmation contract.

The preview must show the effective buy mode, bound source identifiers/attributes or helper, actual resolved fixed rate or first forecast rates, currency, and current tariff transformations. Household confirmation must identify the configured source and the **actual** method from load quality; show resolved daily estimate or fallback amount where applicable. Explicitly describe the daily value as an estimate and zero as an intentional free import rate. The buy and load checkboxes separately acknowledge those displayed choices; the generic final checkbox is insufficient. Leave the boxes false when rendering a fresh preview; never remember an earlier confirmation across draft edits. Existing source, tariff, helper, and load-setting forms remain the way to change the displayed choices.

This uses the user's approved “select/confirm” option: an unchanged numerical default may be saved only after a separate, clearly labelled acknowledgement of that exact input. Reject replacing defaults with `None`, adding a new mandatory migration, or treating `0` as an absent value: those approaches break valid existing installations and intentional zero rates. Do not add a second source-management wizard.

### Three distinct preview statuses

Keep source assembly through `build_problem`. After it succeeds, run `solve(problem, time_limit_s=values["solve_time_limit_s"])` through `hass.async_add_executor_job`; the settings validator already bounds that limit to 0.1–10 seconds. Run both stages on every preview/save submission using a fresh snapshot/history. Do not cache a successful solve as permission to save later changed inputs.

Show three independent statuses: **source inputs validated/failed**, **base plan feasible/infeasible/timed out/not checked**, and **extra-consumption guidance not checked in preview**. Do not run `compute()` or consumption probes here. A feasible plan is sufficient to save even when additional consumption may later be unavailable. Keep the explanation that guidance is computed after saving; never label it validated by the preview.

Catch `InputError` using the existing source-error form convention. Catch `SolveError` separately and map its stable `reason` to infeasible, timeout, or optimizer-error messages. Preserve the validated input preview and source status on solver failure. Display actionable text: for infeasibility show the configured import/export/inverter limits, curtailment choice, and relevant battery constraints; point to Hardware/Battery/Planning settings. Include specific conditional hints for (a) positive load with no grid import, PV, or battery and (b) positive PV with zero inverter capacity and no curtailment. These are explanations of evident conflicts, not a claim that the solver supplies a minimal conflicting constraint set. For timeout, state that feasibility is unknown and suggest retrying or increasing the existing bounded solve budget. Every failure retains the draft and leaves saved entry data/options/title untouched.

### Native report freshness and explicit timestamp semantics

Add `last_reported` to the immutable HA snapshot. Use a shared `_soc_timestamp` resolver in **both** `_soc` and `freshness_deadline`, for SOC and BMS alike. Add optional per-source `timestamp_policy` / `bms_timestamp_policy` values `auto` and `exact_path` to `soc_options` and a native selector in the SOC/BMS source form.

- `auto` is the default when a policy is absent. For native paths `last_updated` or `last_reported`, use actual `last_reported` when present; fall back to `last_updated` only when the snapshot has no native report timestamp, preserving older synthetic snapshots. An invalid, future, or stale present report timestamp must not trigger fallback.
- With `auto` and any custom path, resolve that exact path. Never replace `attributes.reported_at`, another publication timestamp, or its stale/future value with a recent HA receipt timestamp.
- `exact_path` always uses the specified path, including an explicit `last_updated`. No implicit substitution is permitted.
- New defaults use `auto` and `last_reported`. Missing legacy policies infer `auto`; old saved native `last_updated` therefore benefits from the bug fix without rewriting its path. Existing custom paths retain exact behavior. Show current draft values rather than resetting SOC timestamp fields when revisiting the editor.
- Keep `validate_soc` age, future, plausibility, disagreement and jump checks intact. Never synthesize timestamps from `now`.

No version bump is needed: new keys are optional, runtime handles missing keys, and the existing version-1 default filler can supply `auto` while retaining custom paths. Test this behavior for both entry data and options documents. Reconfigure/save must preserve an explicit `exact_path` choice.

### Load provenance follows the same calculation as the values

Add rich-result siblings to the existing pure tuple-returning functions. The tuple APIs remain wrappers returning `.values`, so existing callers and tests keep their contracts. Record each covered segment's actual method and bucket sample counts in the **same loop** that selects history or fallback. Do not reconstruct provenance later from configuration flags or reimplement the forecasting algorithm.

For recorder sources, compute summary fractions from **elapsed UTC seconds**, not slot counts or predicted kWh. Native intervals can differ in width, cross local-hour boundaries, or have zero predicted energy. Sample counts refer to the eligible historical local-day/hour bucket used for that future segment, after existing lookback/completeness filtering. Do not sum repeated bucket counts into a misleading total.

Explicit daily estimates and supplied forecasts are separate methods; neither is labelled a failed-history fallback. Empty or partial history with allowed fallback remains usable, but gains a warning and precise provenance. Fallback disabled still fails with `InputError`; its message identifies the first insufficient segment and available/required sample counts. Do not reinterpret malformed input, counter errors, or excessive power-sample gaps as harmless missing history.

Publish additive `quality["load"]`, render its summary/sample coverage in preview, and expose the same object as `load_quality` on the existing plan and forecast-valid entities. Add `load_quality` to unrecorded attributes because its interval metadata changes with each forecast. Preserve `missing_sources` as its existing source-identifier contract; use the new coverage fields and warning for insufficient history rather than inserting fake entity IDs.

### Convention lock

Follow `config_flow.Editor.async_step_currency_review` and `source_flow.SourceEditor.async_step_source_mapping` conventions: native selectors; `errors["base"]` translation keys; human-readable detail in description placeholders; a validated candidate replaces the draft only at the successful transaction boundary. Follow `runtime.build_problem` quality conventions: additive JSON-compatible fields and stable warning codes. Follow `coordinator.EnergyCompassCoordinator.async_recalculate` typed `InputError` versus `SolveError.reason` distinction without changing the coordinator. No new logging or polling mechanism is required.

## 3. Steps — one implementation/review task

### A. Make new-installation assumptions explicit

**Modify:** `custom_components/energy_compass/config_flow.py`, `flow_schema.py` if a small shared preview schema helps; `strings.json`, `translations/en.json`, `translations/pl.json`. **Tests:** `tests/test_config_flow.py`, `tests/test_options_flow.py`, `tests/test_final_review.py` where shared flow fixtures require adaptation.

- [ ] Add native-flow regressions proving generic confirm cannot save a new entry, one acknowledgement is insufficient, explicit zero plus daily-estimate acknowledgement can save, and existing entries need no new fields.
- [ ] Add the setup-only acknowledgement schema and defensive handler checks. Display effective source/rate/load assumptions, including helpers and transforms, with separate buy and load labels. Keep flags out of saved configuration.
- [ ] Cover an edited draft reopening preview with false acknowledgement defaults; verify another flow cannot inherit acknowledgement state. Run the targeted flow tests before proceeding.

### B. Validate bounded base-plan feasibility before every save

**Modify:** `custom_components/energy_compass/config_flow.py` and translation files. **Tests:** `tests/test_config_flow.py`, `tests/test_options_flow.py`.

- [ ] Add real-solver regressions for positive load with zero import and no generation/battery, and positive PV with a zero inverter and no curtailment. Attempt final confirmation and assert no entry is created/updated/reloaded.
- [ ] Add the executor base-solve stage with the resolved solve budget; preserve separate source/solver status and map typed failure reasons to actionable form messages.
- [ ] Add a deterministic mocked timeout and solver-failure check, plus a spy proving the solve budget is passed and consumption probes/`compute` are never invoked.
- [ ] Add a feasible base plan whose extra-kWh probe would be infeasible; preview must allow saving and explicitly state guidance is untested. Add a source/helper change between successful preview and submit; save must use fresh data and reject newly infeasible inputs.

### C. Share a correct SOC timestamp policy across validation and expiry

**Modify:** `custom_components/energy_compass/flow_schema.py`, `settings.py`, `runtime.py`, `source_flow.py`, translation files. **Tests:** `tests/test_config_flow.py`, `tests/test_task5_runtime.py`, `tests/test_lifecycle.py`, `tests/test_sources.py` as appropriate. Do not change `sources/battery.py` validation semantics.

- [ ] Reproduce identical real HA `State` reports 11 minutes apart: unchanged `last_updated`, fresh `last_reported`. Verify snapshot, `_soc`, and deadline behavior for both new defaults and a legacy configuration with no policy.
- [ ] Implement the shared resolver and auto/exact selector. Make SOC and BMS read the same effective timestamp for validation and expiry; retain missing-native-timestamp support for historical test snapshots.
- [ ] Cover fresh native report plus stale/future custom measurement, stale/future native reports, malformed timestamps, exact native `last_updated`, and genuine new-report jump validation.
- [ ] Add options/reconfigure persistence and version-1 migration tests for native, custom, and explicit exact-path choices in both stored documents. Run source/runtime/lifecycle tests.

### D. Carry and publish load history/fallback coverage

**Modify:** `custom_components/energy_compass/engine/models.py`, `engine/forecast.py`, `sources/history.py`, `runtime.py`, `config_flow.py`, and the narrow additive attribute branches/constants in `entity.py`. **Tests:** `tests/test_forecast.py`, `tests/test_sources.py`, `tests/test_task5_runtime.py`, `tests/test_entities.py`, `tests/test_config_flow.py`.

- [ ] Add rich-result models and wrappers, then regressions for complete/partial/empty recorder history and explicit estimates/forecasts. Existing tuple-returning tests must remain valid without blanket assertion rewrites.
- [ ] Produce segment metadata during prediction; propagate it through `load_for_slots_with_quality` into `build_problem` and `compute`. Add the summary fields and `load_history_fallback` warning whenever fallback duration is positive, even when fallback energy is zero.
- [ ] Render actual method, fallback hours/fraction, and available/required samples in preview. Expose the same `load_quality` payload on plan and forecast-valid entities; add it to unrecorded attributes. No coordinator/sensor behavior changes.
- [ ] Cover unequal native intervals, a partial hour, a slot crossing an hour, zero load, weekend grouping, and DST transition durations. Compare produced load values to existing tuple APIs and assert provenance sums to the forecast duration.

### E. Document, verify, and review the combined change

**Modify:** `docs/installation.md` and `docs/compatibility.md` if that existing document is the appropriate public-contract reference; otherwise keep the quality/timestamp notes in `docs/installation.md`. Do not introduce unrelated documentation reorganization.

- [ ] Document first-setup acknowledgements, what feasibility does and does not validate, SOC auto/exact timestamp semantics, and the load-quality attribute contract/fraction denominator.
- [ ] Run targeted tests, then the full existing suite and repository lint/format checks in the pinned Docker test environment. Record commands and result summaries; do not fix the unrelated dependency warnings.
- [ ] Independently review all four acceptance areas and atomic-save compatibility as one patch. Inspect the `entity.py` diff against the retention branch's concerns: only additive quality publication/unrecorded attributes are allowed.

## 4. Interfaces and data contracts

### Forecast result

Add immutable dataclasses in `engine/models.py`:

- `LoadCoverageSegment(start: datetime, end: datetime, method: Literal["history", "fallback", "daily_estimate", "forecast"], samples_available: int | None, samples_required: int | None)`.
- `LoadForecastResult(values: tuple[float, ...], coverage: tuple[LoadCoverageSegment, ...])`.

`forecast_load_with_quality(history, slots, timezone, fallback_daily_kwh, settings) -> LoadForecastResult` accepts the exact existing `forecast_load` parameter types. `forecast_load(...) -> tuple[float, ...]` delegates and returns `.values`.

`load_for_slots_with_quality(source, states, now, slots, timezone, settings, *, statistics=(), power_samples=(), fallback_daily_kwh=None) -> LoadForecastResult` retains the exact existing `load_for_slots` parameter types/defaults. `load_for_slots(...) -> tuple[float, ...]` remains the compatibility wrapper.

Coverage segments have aware timestamps, positive elapsed duration, no overlapping coverage within the requested slots, and values consistent with the same forecast selection. Recorder coverage is split at every existing UTC hour boundary and native slot boundary. Non-history modes can return one segment per native slot with both sample fields `None`. Empty slots return empty values/coverage.

### Public quality

`quality["load"]` and public `load_quality` contain:

| Field | Contract |
| --- | --- |
| `source_mode` | `recorder`, `daily_estimate`, or `forecast` |
| `method` | `history`, `history_with_fallback`, `fallback`, `daily_estimate`, or `forecast` |
| `coverage_hours` | Sum of requested forecast segment UTC seconds / 3600 |
| `history_coverage_hours` | Duration served by history / 3600 |
| `fallback_coverage_hours` | Duration served by fallback / 3600 |
| `fallback_fraction` | Fallback UTC seconds / total covered UTC seconds; 0 for empty coverage or non-fallback modes |
| `insufficient_history_hours` | Requested duration whose historical bucket lacks minimum samples; equal to fallback duration for successful recorder results, 0 for complete recorder history, `None` for non-history modes |
| `coverage` | JSON list of segment `start`, `end`, `method`, `samples_available`, `samples_required`; timestamps are ISO strings |

Never call this fraction an energy share. A selected daily estimate is not recorder fallback. For each recorder segment, show the count used for its local-hour/day-group bucket. Do not collapse distinct UTC occurrences of the repeated DST hour. The bounded horizon/native interval limit bounds payload size; no raw recorder history is published.

### SOC and preview

`_soc_timestamp(config: dict, states: dict, binding: EntityBinding, *, prefix: str = "") -> datetime` accepts `prefix=""` or `"bms_"`, reads the matching policy/path, and follows the exact auto/exact rules above. All `_soc` and `freshness_deadline` SOC/BMS calls use it.

Existing `build_problem(...) -> (Problem, values, quality)`, `compute(...) -> dict`, `_soc(...) -> (energy, observation)`, and `freshness_deadline(...) -> datetime` contracts remain intact. Quality is additive. `InputError` still means invalid source/domain data; `SolveError.reason` distinguishes optimizer outcomes. The existing `_finish()` runs only after source validation, a successful bounded base solve, and the applicable confirmations.

## 5. Risks and edge cases

- **Legacy SOC ambiguity:** old `last_updated` may have been an implicit default or an explicit preference. Auto intentionally fixes native report freshness; `exact_path` preserves a selectable strict change-time policy. Custom paths never change semantics.
- **Migration:** adding an unconditional “reported” mode would override old custom paths. Use `auto` inference, including when version-1 defaults are filled. A missing policy must remain supported at runtime.
- **Solver timing:** the solver's existing time limit bounds the base MILP; it is not a promise of instant wall-clock completion of history I/O/model assembly. Do not add cancellable-thread wrappers that return while unowned optimizer jobs continue. Do not run full consumption probes during preview.
- **Feasibility versus guidance:** exhausted import headroom can yield a feasible base plan and unavailable extra consumption. This is a valid save, with guidance explicitly untested.
- **Confirmations versus live inputs:** every save rereads live inputs and reruns the solver. Never preserve old acknowledgement defaults after a relevant draft edit. Fixed values and resolved helpers must be shown accurately rather than echoing stale serialized source defaults.
- **Provenance drift:** metadata must be captured at the history/fallback choice, not guessed from `allow_fallback`. Using a daily estimate intentionally must not produce a “missing history” claim.
- **Fractions:** use UTC elapsed duration, including partial and unequal intervals; zero fallback kWh can still mean 100% fallback. A 23/25-hour DST interval is not a 24-hour denominator.
- **Parallel branch:** publish attributes additively without changing `available`, `native_value`, `_invalidate`, or data retention. Preserve existing attribute schema version because this is an additive contract, and keep bulky metadata unrecorded.

## 6. Verification

Run tests only in the worktree mounted read-only into the existing pinned test image. Example targeted command from the worktree:

```bash
docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/workspace -v "$PWD:/workspace:ro" ha-energy-compass-test:2026.9.1 -m pytest tests/test_config_flow.py tests/test_options_flow.py tests/test_final_review.py tests/test_forecast.py tests/test_sources.py tests/test_task5_runtime.py tests/test_lifecycle.py tests/test_entities.py -q -p no:cacheprovider --tb=short
```

Final checks use the same Docker prefix with `-m pytest -q -p no:cacheprovider --tb=short`, `-m ruff check custom_components tests`, and `-m ruff format --check custom_components tests`. Capture noisy test logs outside the source mount and report only the result summary unless a failure requires detail.

| Acceptance area | Required decisive assertions |
| --- | --- |
| New setup | `confirm=True` alone cannot create; each missing role acknowledgement blocks; shown explicit zero and estimate can create; confirmations are absent from stored config; changed draft requires current acknowledgement |
| Existing entries | Existing options/reconfigure save with the original confirmation; rejected preview/submit preserves exact saved data/options/title and does not reload |
| Source versus feasibility | Source failure reports source status failed and plan unchecked; real infeasible case reports inputs valid and plan infeasible; timeout is unknown feasibility, never success |
| Bound and scope | Actual resolved `solve_time_limit_s` reaches solver; full runtime computation and consumption probes are not called; fresh input change at submit is rechecked |
| Guidance distinction | Feasible plan with no extra-load headroom is saveable; preview says guidance untested rather than available |
| Native SOC | Real identical HA reports preserve old `last_updated` and fresh `last_reported`; both new and legacy auto configurations accept; expiry uses report time for SOC and BMS |
| Timestamp safeguards | Truly stale/future native or custom reports reject; missing/invalid custom timestamp rejects; exact native path retains strict behavior; invalid present native report never falls back |
| Compatibility | Version-1 fill and options/reconfigure preserve custom/exact paths; optional policies do not invalidate old configs; tuple forecast/load APIs retain all old numerical behavior |
| Fallback visibility | Empty history + fallback gives method fallback, fraction 1, zero available samples, required counts and warning even with verified tariff; mixed history reports exact segment and duration coverage; complete history gives zero fallback |
| Other load modes | Intentional daily estimate and provided forecast identify themselves, with no fabricated history samples or history-missing status |
| Time arithmetic | Unequal slots, partial/cross-hour slots, weekend buckets and DST produce elapsed-time fractions; zero energy never suppresses fallback provenance |
| Published contract | `compute().quality.load`, preview, and plan/forecast-valid `load_quality` agree; the attribute is unrecorded; entity IDs and validity/availability behavior are unchanged |

This plan was prepared by source inspection only. No implementation code or tests were executed by the architect; the 225-test baseline is the parent's verified result.
